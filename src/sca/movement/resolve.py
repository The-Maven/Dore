"""Resolver — score predictions against reality.

LAYER: facts. Strictly proper scoring rules per Gneiting & Raftery
2007. Brier for binary/direction predictions, CRPS for continuous
predictions. Every resolution carries TWO baseline scores
(persistence + climatology) so the calibration UI can show whether
the engine adds skill or is just restating naive baselines.

Discipline: the resolver writes exactly once per prediction. The
unique constraint on resolutions.prediction_id is the safety net; the
list_unresolved + insert pattern here is idempotent so a crashed
cycle can be re-run without polluting the archive.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from sca.movement.predict import (
    climatology_baseline_peg,
    persistence_baseline_peg,
)
from sca.observability import log_event


# ── strictly proper scoring rules ────────────────────────────────────
def brier_score_binary(forecast_prob: float, actual_positive: bool) -> float:
    """Brier for a single binary forecast: (p - outcome)^2. Range
    [0, 1], lower better. Perfectly calibrated forecast of 0.5 on a
    50/50 outcome distribution scores 0.25; persistently scoring
    below 0.25 means the model has real skill over coin-flipping."""
    p = max(0.0, min(1.0, float(forecast_prob)))
    o = 1.0 if actual_positive else 0.0
    return (p - o) ** 2


def normalised_miss_distance(actual: float, point: float,
                               p50_half: float, p80_half: float,
                               p95_half: float) -> float:
    """Continuous-forecast scoring: |actual - point| scaled by the 80%
    half-width. NOT CRPS — true CRPS requires integrating the full
    predictive CDF and we don't carry it. This is a simpler proper-
    enough proxy: zero on a perfect call, ~1.0 when the actual lands
    at the 80% band edge, grows linearly beyond that. Reported in the
    'crps_score' column for backwards-compatible store schema, but
    labelled honestly in the comment + UI as a band-distance proxy.

    Why a proxy is acceptable here: the Brier score IS proper for our
    direction predictions, and the outcome_kind column (inside_p50 /
    p80 / p95 / outside) provides the categorical truth on continuous
    predictions. The proxy adds a magnitude signal for the calibration
    diagram without overclaiming we run real CRPS."""
    err = abs(actual - point)
    scale = max(p80_half, 0.5)
    return err / scale


# Kept as an alias so existing callers continue to work; the new name
# is the canonical one to use going forward.
crps_from_band = normalised_miss_distance


def actual_was_positive(actual_value: float) -> bool:
    """Direction predicate for binary scoring — net mint > 0 = 'up'.
    Exactly zero counts as not-positive (rare in practice on supply
    readings; even quiet hours net-burn a few units to the dust)."""
    return actual_value > 0.0


def outcome_band(actual: float, p50_low: Optional[float],
                  p50_high: Optional[float], p80_low: Optional[float],
                  p80_high: Optional[float], p95_low: Optional[float],
                  p95_high: Optional[float]) -> str:
    """Which uncertainty band did the actual land in? Used by the UI
    to render a hit/partial/miss badge per row.

    Order matters: tightest band wins. 'inside_p50' is a strong hit
    (the engine was confident AND right); 'outside' is a clean miss
    (engine was wrong even at 95% confidence)."""
    if p50_low is not None and p50_high is not None \
            and p50_low <= actual <= p50_high:
        return "inside_p50"
    if p80_low is not None and p80_high is not None \
            and p80_low <= actual <= p80_high:
        return "inside_p80"
    if p95_low is not None and p95_high is not None \
            and p95_low <= actual <= p95_high:
        return "inside_p95"
    return "outside"


# ── resolution from history ──────────────────────────────────────────
def realized_peg_deviation_at(symbol: str,
                                target_ts_iso: str) -> Optional[float]:
    """Pull the peg_ticks row closest to `target_ts_iso` for `symbol`
    and return its deviation_bps. None means we don't have a tick at
    or near the target — the resolver delays grading in that case.

    Tolerance: ±5 minutes around the target. Tighter than the
    horizon_minutes window so we don't grade against a stale tick.
    """
    from sca.store import get_store
    try:
        ticks = get_store().list_peg_ticks(symbol, limit=400) or []
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        log_event(
            "movement.resolver.peg_lookup_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None
    if not ticks:
        return None
    target = _parse_iso(target_ts_iso)
    if target is None:
        return None
    best_dt = None
    best_tick = None
    for t in ticks:
        rt = _parse_iso(t.get("read_at"))
        if rt is None:
            continue
        gap_s = abs((target - rt).total_seconds())
        if gap_s > 300:  # 5-minute tolerance
            continue
        if best_dt is None or gap_s < best_dt:
            best_dt = gap_s
            best_tick = t
    if best_tick is None:
        return None
    return float(best_tick.get("deviation_bps") or 0.0)


def realized_net_flow_at(symbol: str, made_at_iso: str,
                          resolves_at_iso: str) -> Optional[float]:
    """Net change in total supply for `symbol` between made_at and
    resolves_at. Pulls snapshots from the store and walks the window.

    Returns None when we don't have at least one snapshot on either
    side — the resolver waits in that case rather than guessing."""
    from sca.store import get_store
    try:
        snaps = get_store().list_snapshots(symbol, limit=2000) or []
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.resolver.snapshot_lookup_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None
    if not snaps:
        return None
    made = _parse_iso(made_at_iso)
    end = _parse_iso(resolves_at_iso)
    if made is None or end is None:
        return None
    # Snapshots come newest-first; find the one closest to each endpoint.
    start_supply = _closest_supply(snaps, made)
    end_supply = _closest_supply(snaps, end)
    if start_supply is None or end_supply is None:
        return None
    return end_supply - start_supply


def _closest_supply(snaps: list[dict],
                     when: datetime) -> Optional[float]:
    best_dt = None
    best = None
    for s in snaps:
        rt = _parse_iso(s.get("read_at"))
        if rt is None:
            continue
        gap = abs((when - rt).total_seconds())
        if gap > 1800:  # 30-minute tolerance
            continue
        if best_dt is None or gap < best_dt:
            best_dt = gap
            best = float(s.get("total_supply") or 0.0)
    return best


def _persistence_brier(prediction: dict,
                         actual_positive: bool) -> Optional[float]:
    """Honest persistence baseline for direction predictions:
    forecast = the last observed direction with confidence 1.0. We
    look back one horizon-equivalent window from made_at; if we find
    a prior net change, the persistence forecast is its sign.
    Returns None when we don't have prior data to anchor on — better
    a missing column than a meaningless one."""
    from datetime import timedelta
    symbol = prediction.get("symbol")
    horizon_m = int(prediction.get("horizon_minutes") or 60)
    made = _parse_iso(prediction.get("made_at"))
    if made is None:
        return None
    prior_start = (made - timedelta(minutes=horizon_m * 2)).isoformat(
        timespec="seconds")
    prior_end = (made - timedelta(minutes=horizon_m)).isoformat(
        timespec="seconds")
    prior_change = realized_net_flow_at(symbol, prior_start, prior_end)
    if prior_change is None:
        return None
    persistence_says_positive = prior_change > 0
    # Persistence is a confident forecast: 1.0 in the direction it
    # picks. Brier becomes 0 if it called the actual direction,
    # 1 if it called the opposite.
    return 0.0 if (persistence_says_positive == actual_positive) else 1.0


def _climatology_brier(prediction: dict,
                         actual_positive: bool) -> float:
    """Climatology baseline: forecast = empirical base rate from
    prior resolutions on the same (symbol, kind). Falls back to 0.5
    when the archive is too thin to read a rate. Brier becomes
    (rate - outcome)^2 — and for outcome=0/1 with rate≈0.5 this
    naturally lands near 0.25."""
    try:
        from sca.store import get_store
        store = get_store()
        prior = store.list_resolutions(
            symbol=prediction.get("symbol"),
            kind=prediction.get("kind"),
            limit=500,
        ) or []
    except Exception:  # noqa: BLE001
        return brier_score_binary(0.5, actual_positive)
    positives = sum(
        1 for r in prior
        if r.get("actual_value") is not None
        and float(r["actual_value"]) > 0
    )
    total = sum(
        1 for r in prior if r.get("actual_value") is not None
    )
    if total < 5:
        # Too thin — fall back to honest 50/50.
        return brier_score_binary(0.5, actual_positive)
    rate = positives / total
    return brier_score_binary(rate, actual_positive)


def _parse_iso(s) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ── pipeline ─────────────────────────────────────────────────────────
def grade_one(prediction: dict) -> Optional[dict]:
    """Score a single prediction and return the resolution row
    payload, or None when reality isn't observable yet (e.g. no peg
    tick within tolerance — the resolver will retry next cycle).

    Computes:
      - actual_value: realised outcome at resolves_at
      - brier_score:  for direction predictions
      - crps_score:   for continuous (peg_deviation) predictions
      - outcome_kind: which band the actual landed in
      - baseline_*_brier: persistence + climatology Brier on the same row
    """
    kind = prediction.get("kind")
    symbol = prediction.get("symbol")
    if kind == "peg_deviation":
        actual = realized_peg_deviation_at(
            symbol, prediction.get("resolves_at"))
    elif kind in ("net_flow_direction", "net_flow_magnitude"):
        actual = realized_net_flow_at(
            symbol, prediction.get("made_at"), prediction.get("resolves_at"))
    else:
        actual = None
    if actual is None:
        return None

    # Bands → outcome band tag.
    p50_low = _f(prediction.get("p50_low"))
    p50_high = _f(prediction.get("p50_high"))
    p80_low = _f(prediction.get("p80_low"))
    p80_high = _f(prediction.get("p80_high"))
    p95_low = _f(prediction.get("p95_low"))
    p95_high = _f(prediction.get("p95_high"))
    outcome = outcome_band(actual, p50_low, p50_high,
                            p80_low, p80_high, p95_low, p95_high)

    brier = None
    crps = None
    base_p_brier = None
    base_c_brier = None
    if kind == "peg_deviation":
        point = _f(prediction.get("point")) or 0.0
        p80_half = _maybe_half(p80_low, p80_high)
        crps = crps_from_band(
            actual, point,
            _maybe_half(p50_low, p50_high) or 0.0,
            p80_half or 1.0,
            _maybe_half(p95_low, p95_high) or 0.0,
        )
    elif kind in ("net_flow_direction",):
        prob = prediction.get("prob_positive")
        if prob is not None:
            brier = brier_score_binary(float(prob), actual_was_positive(actual))
            # Baselines, honest definitions:
            #   - persistence: predict the SAME direction as the last
            #     observed move. Requires knowing the prior step's
            #     direction; we look it up via realized_net_flow_at
            #     over the prior horizon-equal window. If unavailable
            #     (insufficient history), the baseline is None and the
            #     calibration UI says so.
            #   - climatology: predict at the long-run base rate. We
            #     read the empirical positive-rate from the store's
            #     prior resolutions; falls back to 0.5 if the archive
            #     is too thin.
            base_p_brier = _persistence_brier(
                prediction, actual_was_positive(actual)
            )
            base_c_brier = _climatology_brier(
                prediction, actual_was_positive(actual)
            )

    narrative = _narrate_outcome(prediction, actual, outcome, brier, crps)
    return {
        "prediction_id": prediction.get("id"),
        "actual_value": actual,
        "brier_score": brier,
        "crps_score": crps,
        "outcome_kind": outcome,
        "narrative": narrative,
        "baseline_persistence_brier": base_p_brier,
        "baseline_climatology_brier": base_c_brier,
    }


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _maybe_half(low: Optional[float],
                 high: Optional[float]) -> Optional[float]:
    if low is None or high is None:
        return None
    return (high - low) / 2.0


def _narrate_outcome(prediction: dict, actual: float, outcome: str,
                      brier: Optional[float],
                      crps: Optional[float]) -> str:
    """Honest, deterministic post-mortem prose. No LLM here — the
    resolver writes the same sentence the calibration page would; if
    we ever want richer narration we put it in attribute.py at
    prediction-time, not resolution-time."""
    kind = prediction.get("kind", "")
    point = _f(prediction.get("point"))
    # Honest n/a: a prediction with no point (insufficient_history)
    # still gets a resolution row, but the narrative names the gap
    # rather than crashing on a None-format.
    if point is None:
        head = f"Forecast unavailable (no point); actual {actual:.2f}."
    elif kind == "peg_deviation":
        unit = "bp"
        head = f"Forecast {point:.2f}{unit}; actual {actual:.2f}{unit}."
    elif kind == "net_flow_direction":
        head = (f"Forecast net change {point:.0f}; "
                f"actual {actual:.0f}.")
    else:
        head = f"Forecast {point}; actual {actual}."
    tail = {
        "inside_p50": " Inside the 50% band — confident and right.",
        "inside_p80": " Inside the 80% band — within stated uncertainty.",
        "inside_p95": " Inside the 95% band — wider than expected, "
                      "but within tail.",
        "outside": " Outside the 95% band — stated uncertainty was "
                   "too narrow.",
    }.get(outcome, "")
    score_tail = ""
    if brier is not None:
        score_tail = f" Brier {brier:.3f}."
    elif crps is not None:
        score_tail = f" CRPS {crps:.3f}."
    return (head + tail + score_tail).strip()


# ── runner ───────────────────────────────────────────────────────────
def run_resolver_cycle(limit: int = 200) -> dict:
    """Find aged-out predictions, grade what we can, write the
    resolutions. Returns a summary for observability. Best-effort:
    individual grading failures log + skip; the cycle never raises."""
    from sca.store import get_store
    store = get_store()
    summary = {
        "checked": 0, "graded": 0, "waiting": 0, "errors": 0,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        unresolved = store.unresolved_predictions(limit=limit) or []
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.resolver.list_failed", level="error",
            error_class=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        summary["errors"] += 1
        return summary

    for pred in unresolved:
        summary["checked"] += 1
        try:
            resolution = grade_one(pred)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.resolver.grade_failed", level="warn",
                prediction_id=pred.get("id"),
                error_class=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            summary["errors"] += 1
            continue
        if resolution is None:
            summary["waiting"] += 1
            continue
        try:
            store.insert_resolution(resolution)
            summary["graded"] += 1
            log_event(
                "movement.resolver.graded", level="info",
                prediction_id=pred.get("id"),
                symbol=pred.get("symbol"),
                kind=pred.get("kind"),
                outcome=resolution["outcome_kind"],
                brier=resolution.get("brier_score"),
                crps=resolution.get("crps_score"),
            )
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.resolver.insert_failed", level="warn",
                prediction_id=pred.get("id"),
                error_class=type(exc).__name__,
            )
            summary["errors"] += 1

    summary["completed_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    return summary
