"""Background ticker — drives forecast emission and resolver cycles.

ONE thread, ONE responsibility: every N minutes (configurable; default
10), for each enabled symbol and kind, build a forecast row from the
deterministic engine, attach the cited attribution paragraph, persist
the prediction, then run a resolver cycle to grade any predictions
that have aged out since last tick.

The thread is started by the FastAPI lifespan on import, gated by an
env var so tests don't accidentally fire predictions during a pytest
session. The runtime config is reloaded on every cycle so an admin
can change cadence / symbols without a server restart.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from sca.movement import config as sim_config
from sca.movement import peg_tick_runner
from sca.movement.attribute import compose_attribution, gather_event_candidates
from sca.movement.brave_context import fetch_context_for as fetch_brave_context
from sca.movement.judge import compose as compose_judge
from sca.movement.predict import (
    forecast_net_flow_direction,
    forecast_peg_deviation,
)
from sca.movement.resolve import run_resolver_cycle
from sca.observability import log_event


# IPCC confidence ladder with EXPLICIT calm-rank integers.
# Calm rank: 0 = most calm (skip web context), 6 = least calm (fetch
# context even at the cost of a quota call). Pinned as tuples so an
# editor "tidying" the order alphabetically cannot silently invert
# the semantics — the rank lives next to the word.
_CALM_RANK: dict[str, int] = {
    "virtually_certain": 0,
    "very_likely": 1,
    "likely": 2,
    "about_as_likely_as_not": 3,
    "unlikely": 4,
    "very_unlikely": 5,
    "exceptionally_unlikely": 6,
}


def _pick_least_calm(words: list[str | None]) -> str | None:
    """Return the highest-calm-rank (least-calm) word from a list —
    that's the one the Brave interest gate uses to decide whether
    to fetch fresh web context. None entries are ignored; an
    all-None list returns None and the gate treats that as
    'not calm' (don't skip)."""
    best_rank = -1
    best_word: str | None = None
    for w in words:
        if not w:
            continue
        rank = _CALM_RANK.get(w)
        if rank is None:
            continue
        if rank > best_rank:
            best_rank = rank
            best_word = w
    return best_word


_TICKER_DISABLED_ENV = "SCA_MOVEMENT_TICKER_DISABLED"
_TICKER_LOCK = threading.Lock()
_TICKER_STATE: dict = {
    "running": False,
    "last_tick_at": None,
    "last_summary": None,
    "started_at": None,
}


def is_running() -> bool:
    with _TICKER_LOCK:
        return _TICKER_STATE["running"]


def state() -> dict:
    """Snapshot of the ticker's current runtime state. Read by the
    /api/simulator/state endpoint."""
    with _TICKER_LOCK:
        return dict(_TICKER_STATE)


def _peg_history_bps(symbol: str, limit: int = 60) -> list[float]:
    """Pull recent peg ticks for `symbol`, oldest-last so the
    forecast model sees a forward-moving series."""
    from sca.store import get_store
    try:
        rows = get_store().list_peg_ticks(symbol, limit=limit) or []
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.ticker.peg_history_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return []
    # list_peg_ticks returns newest-first; flip for the model.
    rows = list(reversed(rows))
    return [float(r.get("deviation_bps") or 0.0) for r in rows]


def _supply_series(symbol: str, limit: int = 60) -> list[float]:
    """Pull recent total-supply snapshots for `symbol`, oldest-last."""
    from sca.store import get_store
    try:
        rows = get_store().list_snapshots(symbol, limit=limit) or []
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.ticker.supply_history_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return []
    rows = list(reversed(rows))
    return [float(r.get("total_supply") or 0.0) for r in rows]


def _emit_for_symbol(symbol: str, kinds: list[str],
                     horizon_minutes: int,
                     *,
                     max_in_flight: int | None = None,
                     run_judge: bool = True) -> dict:
    """Build + persist one prediction per enabled kind for `symbol`.
    Returns a per-kind summary dict for observability.

    Audit #9: `max_in_flight` caps unresolved-prediction count per
    symbol so a misconfigured tiny-tick / huge-horizon never floods
    the archive. When the cap is hit, we skip emit (and log) instead
    of writing yet another row that can't be resolved. The resolver
    drains the backlog naturally on the next cycle."""
    out = {"symbol": symbol, "emitted": {}, "skipped": {}, "errors": {}}

    # Check the in-flight cap BEFORE doing any LLM work. The cap is
    # cheap to enforce (one count query) and prevents the largest
    # cost layers (judge + brave) from running on a row we'd then
    # drop. None = uncapped.
    if max_in_flight is not None and max_in_flight > 0:
        try:
            from sca.store import get_store
            unresolved = get_store().list_predictions(
                symbol=symbol, limit=max_in_flight + 10,
            ) or []
            # "in-flight" = prediction without a resolution. We have no
            # join here but the resolver writes a resolutions row 1:1
            # with predictions; the cheap proxy is "predictions whose
            # resolves_at hasn't passed yet OR which lack a resolution
            # field on the joined view". For the FileStore the joined
            # view isn't applied here; we approximate with future
            # resolves_at.
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            future_unresolved = sum(
                1 for p in unresolved
                if (p.get("resolves_at") or "") > now_iso
            )
            if future_unresolved >= max_in_flight:
                log_event(
                    "movement.ticker.in_flight_cap_hit", level="info",
                    symbol=symbol,
                    in_flight=future_unresolved,
                    cap=max_in_flight,
                )
                out["skipped"]["in_flight_cap"] = future_unresolved
                return out
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.ticker.in_flight_check_failed", level="info",
                symbol=symbol, error_class=type(exc).__name__,
            )

    candidates = gather_event_candidates(symbol)
    forecast_summary_parts = []
    forecasts = []

    if "peg_deviation" in kinds:
        history = _peg_history_bps(symbol)
        f = forecast_peg_deviation(
            symbol, history, horizon_minutes, drivers=[],
        )
        forecasts.append(f)
        forecast_summary_parts.append(
            f"{symbol} peg deviation {f.point:+.2f}bp at {horizon_minutes}m"
        )

    if "net_flow_direction" in kinds:
        series = _supply_series(symbol)
        f = forecast_net_flow_direction(
            symbol, series, horizon_minutes, drivers=[],
        )
        forecasts.append(f)
        forecast_summary_parts.append(
            f"{symbol} net flow {f.point:+.0f} at {horizon_minutes}m "
            f"(p+={f.prob_positive or 0:.2f})"
        )

    # ONE attribution paragraph for both kinds — drivers operate at
    # the symbol level, not the kind level. Saves LLM budget and
    # keeps the narrative coherent.
    if forecasts:
        forecast_summary = " · ".join(forecast_summary_parts)
        try:
            sentence, used = compose_attribution(
                forecast_summary, candidates,
            )
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.ticker.attribution_failed", level="warn",
                symbol=symbol, error_class=type(exc).__name__,
            )
            sentence, used = "No driver cited.", []
        for f in forecasts:
            f.drivers = used
            if sentence != "No driver cited.":
                f.notes = (f.notes + " " if f.notes else "") + f"attr: {sentence}"

    from sca.store import get_store
    store = get_store()
    # Pull a calibration snapshot ONCE per symbol so the judge can
    # temper its own confidence based on actual track record. Cheap
    # in-process aggregation — fine to call inside the ticker.
    try:
        calibration = store.calibration_summary(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        # Audit #15: a store exception is NOT 'no resolutions yet';
        # the judge needs to tell them apart so it can say 'no track
        # record on this target' vs 'calibration backend unavailable
        # — treat this call with extra caution'. Mark explicitly.
        log_event(
            "movement.ticker.calibration_unavailable", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        calibration = {"count": 0, "unavailable": True,
                        "error_class": type(exc).__name__}

    # Coalesce Brave context: ONE call per symbol, shared across all
    # kinds. The interest gate uses the most-uncertain confidence
    # word AND the largest-magnitude point — if any forecast for the
    # symbol has either a wide cone or a far-from-zero point, we
    # fetch fresh context.
    confidence_words = [getattr(f, "confidence_word", None) for f in forecasts]
    least_calm_word = _pick_least_calm(confidence_words)
    # Use the peg-deviation point for the magnitude check when
    # available (it's the only kind in bps; net-flow magnitude is in
    # tokens and isn't comparable to the 5bp threshold).
    peg_forecast = next(
        (f for f in forecasts if getattr(f, "kind", "") == "peg_deviation"),
        None,
    )
    point_for_gate = peg_forecast.point if peg_forecast is not None else None
    try:
        web_ctx = fetch_brave_context(
            symbol, kind=forecasts[0].kind if forecasts else "peg_deviation",
            confidence_word=least_calm_word,
            point_value=point_for_gate,
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.ticker.brave_context_failed", level="info",
            symbol=symbol, error_class=type(exc).__name__,
        )
        web_ctx = []

    for f in forecasts:
        # Run the LLM judge per forecast row. Empty drivers + thin
        # history are honest inputs; the judge handles them by saying
        # so. Output is best-effort — None fields leave the UI to
        # fall back to the engine prose.
        judge_summary = {
            "symbol": f.symbol,
            "kind": f.kind,
            "horizon_minutes": f.horizon_minutes,
            "point": f.point,
            "p50_low": f.p50_low, "p50_high": f.p50_high,
            "p80_low": f.p80_low, "p80_high": f.p80_high,
            "p95_low": f.p95_low, "p95_high": f.p95_high,
            "prob_positive": f.prob_positive,
            "confidence_word": f.confidence_word,
            "engine_notes": f.notes,
        }
        from sca.movement.judge import JudgeOutput
        if not run_judge:
            # Skip the LLM call for non-mover symbols to keep cycle
            # cost bounded. The deterministic forecast row is still
            # written; the UI falls back to engine prose for tokens
            # without judge output.
            j = JudgeOutput(synthesis=None, insight=None, pitch=None)
        else:
            try:
                # web_ctx was fetched ONCE for the symbol above
                # (coalesced across kinds — see cost-discipline notes
                # in brave_context.py).
                j = compose_judge(
                    judge_summary, sentence if forecasts else "",
                    used, calibration, web_context=web_ctx,
                )
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "movement.ticker.judge_failed", level="warn",
                    symbol=symbol, kind=f.kind,
                    error_class=type(exc).__name__,
                )
                j = JudgeOutput(synthesis=None, insight=None, pitch=None)

        try:
            row = {
                "symbol": f.symbol,
                "kind": f.kind,
                "horizon_minutes": f.horizon_minutes,
                "resolves_at": f.resolves_at,
                "point": f.point,
                "p50_low": f.p50_low, "p50_high": f.p50_high,
                "p80_low": f.p80_low, "p80_high": f.p80_high,
                "p95_low": f.p95_low, "p95_high": f.p95_high,
                "prob_positive": f.prob_positive,
                "confidence_word": f.confidence_word,
                "drivers": f.drivers,
                "model": f.model,
                "notes": f.notes,
                "judge_synthesis": j.synthesis,
                "judge_insight": j.insight,
                "judge_pitch": j.pitch,
                "judge_model": j.model if (
                    j.synthesis or j.insight or j.pitch
                ) else None,
            }
            store.insert_prediction(row)
            out["emitted"][f.kind] = True
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.ticker.insert_failed", level="warn",
                symbol=symbol, kind=f.kind,
                error_class=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            out["errors"][f.kind] = type(exc).__name__
    return out


def _tick_once(cfg: Optional[dict] = None) -> dict:
    """Run one ticker cycle: peg ticks → forecasts → resolver."""
    cfg = cfg or sim_config.load()
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tick_interval_minutes": cfg["tick_interval_minutes"],
        "horizon_minutes": cfg["horizon_minutes"],
        "per_symbol": [],
        "resolver": None,
    }
    if not cfg.get("enabled", True):
        summary["disabled"] = True
        return summary

    # 1. Refresh peg ticks for every tracked symbol. The orchestrator
    # returns a consensus-kind histogram so the simulator state
    # surface can show source agreement at a glance.
    summary["peg_tick_refresh"] = peg_tick_runner.refresh_for_symbols(
        cfg["symbols"])

    # 2. Pick the "judge symbol" — the largest mover this cycle by
    # |current peg deviation|. Only this symbol gets the LLM judge
    # call. Others get the deterministic forecast without judge prose.
    # Why: 12 symbols × judge call = 12 LLM calls per cycle, which
    # was rate-limiting DeepSeek and producing empty responses. The
    # editorial framing — "the judge focuses on what's moving" — is
    # also stronger than spraying synthesis on every quiet token.
    from sca.store import get_store as _gs
    _store = _gs()
    largest_mover = None
    largest_abs = -1.0
    for symbol in cfg["symbols"]:
        try:
            recent = _store.list_peg_ticks(symbol, limit=1) or []
        except Exception:  # noqa: BLE001
            recent = []
        if recent:
            v = float(recent[0].get("deviation_bps") or 0.0)
            if abs(v) > largest_abs:
                largest_abs = abs(v)
                largest_mover = symbol
    summary["judge_symbol"] = largest_mover

    # 3. Emit predictions per symbol/kind. The in-flight cap is
    # honored per-symbol so a single misconfigured token can't drag
    # the rest of the cycle.
    in_flight_cap = cfg.get("max_in_flight_per_symbol", 24)
    for symbol in cfg["symbols"]:
        per = _emit_for_symbol(
            symbol, cfg["kinds"], cfg["horizon_minutes"],
            max_in_flight=in_flight_cap,
            run_judge=(symbol == largest_mover),
        )
        summary["per_symbol"].append(per)

    # 3. Grade what's resolvable.
    try:
        summary["resolver"] = run_resolver_cycle()
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.ticker.resolver_failed", level="warn",
            error_class=type(exc).__name__,
        )
        summary["resolver"] = {"errors": 1}

    # 4. The Discipline Trader — opens / resolves simulated trades
    # based on this cycle's predictions. Deterministic; never blocks
    # the ticker on its own failure.
    try:
        from sca.movement.trader import (
            evaluate_cycle as trader_evaluate,
            build_token_blocks_for_trader,
        )
        feed_tokens = build_token_blocks_for_trader(cfg["symbols"])
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        opened = trader_evaluate(feed_tokens, now_iso=now_iso)
        summary["trader"] = {
            "opened_this_cycle": len(opened),
            "symbols_evaluated": len(feed_tokens),
        }
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.ticker.trader_failed", level="warn",
            error_class=type(exc).__name__,
            error_message=str(exc)[:160],
        )
        summary["trader"] = {"errors": 1}

    summary["completed_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    log_event(
        "movement.ticker.cycle", level="info",
        symbols=len(cfg["symbols"]),
        kinds=len(cfg["kinds"]),
        horizon=cfg["horizon_minutes"],
    )
    return summary


def _ticker_loop() -> None:
    """Long-running loop. Sleeps tick_interval_minutes between
    cycles; rereads config every cycle so a /api/simulator/config
    POST takes effect on the next tick."""
    log_event("movement.ticker.started", level="info")
    while True:
        try:
            cfg = sim_config.load()
            with _TICKER_LOCK:
                _TICKER_STATE["running"] = True
                _TICKER_STATE["last_tick_at"] = (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
            summary = _tick_once(cfg)
            with _TICKER_LOCK:
                _TICKER_STATE["last_summary"] = summary
            sleep_s = cfg["tick_interval_minutes"] * 60
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.ticker.cycle_crashed", level="error",
                error_class=type(exc).__name__,
                error_message=str(exc)[:300],
            )
            sleep_s = 60  # back off briefly on a crash
        time.sleep(sleep_s)


def start() -> None:
    """Idempotent: launches the ticker thread once per process. Gated
    by env var so tests don't accidentally run it."""
    if os.environ.get(_TICKER_DISABLED_ENV, "").lower() in ("1", "true", "yes"):
        log_event("movement.ticker.disabled_by_env", level="info")
        return
    with _TICKER_LOCK:
        if _TICKER_STATE.get("started_at"):
            return
        _TICKER_STATE["started_at"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds"))
    threading.Thread(
        target=_ticker_loop, daemon=True, name="movement-ticker",
    ).start()
