"""The Discipline Trader — agentic simulated trader.

A deterministic, profit-focused-but-disciplined persona that watches
the forecast stream for mean-reversion opportunities and places
simulated trades. Sits ALONGSIDE the deterministic forecast pipeline,
never inside it: the predictions remain untouched; this layer just
takes a position on them.

## Persona

"The Discipline Trader" — a market-maker / mean-reversion arbitrageur.
Profit-focused but disciplined:

  - Only trades when the model's point estimate is on the OPPOSITE
    side of the current deviation (i.e. when the forecast says
    "this will mean-revert toward peg"). No trend-following.
  - Position size scales DOWN with cone width — wide cone = small
    position. Refuses to size up when uncertainty is high.
  - Refuses to trade yield-bearing tokens (USDY, sUSDe, USDM …) —
    their drift is structural, not arbitrage.
  - Refuses to trade tokens whose cone is past the per-token alert
    threshold (regime change in progress; not the time to add risk).
  - Caps total open notional across all symbols.
  - Holds for the prediction horizon, then marks the trade as
    "resolved" and computes P&L from the actual peg movement.

The trader is fully deterministic — every decision can be reproduced
from the inputs. The "agentic" framing is editorial: a human persona
narrating the rules-based system.

## Storage

In-memory list + on-disk JSON file (data/discipline_trader.json).
Persists across restarts so the track record survives a dev cycle.
Capped at 200 trades to keep the file bounded.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from sca.config import DATA_DIR
from sca.observability import log_event
from sca.movement.token_context import get_context


# ── persona constants ────────────────────────────────────────────────
PERSONA_NAME = "The Discipline Trader"
PERSONA_TAGLINE = (
    "patient mean-reversion arbitrageur — profit-focused, risk-managed"
)
NOTIONAL_PER_TRADE = 10_000.0   # USD — fixed-size for first iteration
MAX_OPEN_NOTIONAL = 50_000.0    # 5 concurrent trades
ENTRY_THRESHOLD_BPS = 4.0       # don't trade peg deviations smaller than 4bp
TRADER_VERSION = "discipline_v1"


@dataclass
class Trade:
    """One simulated trade. Stored append-only; status migrates from
    'open' → 'resolved' once the prediction horizon elapses."""
    id: str
    symbol: str
    direction: str  # 'long' (expect peg up) | 'short' (expect peg down)
    opened_at: str
    resolves_at: str
    prediction_made_at: str
    entry_bps: float          # actual peg deviation when we entered
    forecast_point_bps: float # what the model predicted
    p80_low: float
    p80_high: float
    notional_usd: float
    cone_width_bps: float
    confidence_word: str
    rationale: str            # plain-English why we took the trade
    # Filled when the trade resolves:
    status: str = "open"      # 'open' | 'resolved'
    exit_bps: Optional[float] = None
    resolved_at_real: Optional[str] = None
    pnl_usd: Optional[float] = None  # signed USD P&L
    pnl_bps: Optional[float] = None  # signed bp move in our favour
    outcome_note: str = ""


# ── persistence ──────────────────────────────────────────────────────
TRADER_PATH: Path = DATA_DIR / "discipline_trader.json"
_TRADER_LOCK = threading.Lock()
_MAX_TRADES = 200


def _load_trades() -> list[Trade]:
    if not TRADER_PATH.exists():
        return []
    try:
        raw = json.loads(TRADER_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        out = []
        for row in raw:
            try:
                out.append(Trade(**row))
            except (TypeError, ValueError):
                continue
        return out
    except (OSError, json.JSONDecodeError):
        return []


def _save_trades(trades: list[Trade]) -> None:
    from sca.persist import atomic_write_json
    rows = [asdict(t) for t in trades[-_MAX_TRADES:]]
    try:
        atomic_write_json(TRADER_PATH, rows)
    except Exception:  # noqa: BLE001
        pass


def all_trades() -> list[dict]:
    """Public read — newest-first. Returns dicts so the API layer can
    serialise straight to JSON."""
    with _TRADER_LOCK:
        trades = _load_trades()
    trades.sort(key=lambda t: t.opened_at, reverse=True)
    return [asdict(t) for t in trades]


# ── decision logic ───────────────────────────────────────────────────
def _decide_to_trade(
    symbol: str, current_bps: float, prediction: dict, meta: dict,
) -> tuple[bool, str]:
    """Returns (should_trade, reason). reason is a plain-English
    narrative used for the rationale field on the trade — either
    "we took this trade because..." or "we DID NOT take this trade
    because..." (the negative case is logged but not surfaced)."""
    ctx = meta or {}
    if ctx.get("yield_bearing"):
        return False, "yield-bearing token — drift is structural, not arbitrage"
    if not isinstance(current_bps, (int, float)):
        return False, "no current peg reading"
    if abs(current_bps) < ENTRY_THRESHOLD_BPS:
        return False, f"peg deviation only {current_bps:+.2f}bp — too tight to enter"
    if not prediction or prediction.get("point") is None:
        return False, "no model prediction available"

    point = prediction["point"]
    p80_low = prediction.get("p80_low")
    p80_high = prediction.get("p80_high")
    if p80_low is None or p80_high is None:
        return False, "forecast has no 80% band — can't size position"
    cone_half = (p80_high - p80_low) / 2

    alert_bps = ctx.get("cone_alert_bps")
    if alert_bps is not None and cone_half >= alert_bps:
        return False, (
            f"cone ±{cone_half:.1f}bp past {symbol}'s alert ({alert_bps:.0f}bp) "
            "— regime change in progress, not the time to add risk"
        )

    # Mean-reversion thesis. The deterministic forecast model (EWMA)
    # is sticky — it predicts the recent average, which is close to
    # the current value when the regime is stable. A market-maker
    # taking peg-reversion bets does NOT wait for the model to PREDICT
    # the reversion; they bet that the cone INCLUDES peg, meaning the
    # model considers 0bp a plausible outcome over the horizon.
    #
    # Rule: take a position OPPOSITE to current_bps when the p80 band
    # straddles or reaches toward 0. Strength of edge = how much of
    # the deviation gets covered by the cone's near edge to 0.
    if current_bps < 0:
        # Below peg → long the token, profit if peg recovers toward 0.
        # The cone must REACH toward peg (p80_high >= 0 is the
        # strong signal; near-zero also acceptable).
        slack_to_peg = p80_high  # how far the upper edge is above 0
        if slack_to_peg < -1.0:
            return False, (
                f"model's 80% band tops out at {p80_high:+.1f}bp — "
                f"doesn't reach toward peg from {current_bps:+.1f}bp. No "
                f"mean-reversion edge."
            )
        recovery_target = max(0.0, p80_high) if slack_to_peg > 0 else point
        return True, (
            f"{symbol} trading {abs(current_bps):.2f}bp below peg with "
            f"the model's 80% band reaching up to {p80_high:+.1f}bp. "
            f"Long position takes the recovery toward "
            f"{recovery_target:+.1f}bp if reversion materialises."
        )
    if current_bps > 0:
        # Above peg → short, profit if peg pulls back toward 0.
        slack_to_peg = -p80_low
        if slack_to_peg < -1.0:
            return False, (
                f"model's 80% band bottoms out at {p80_low:+.1f}bp — "
                f"doesn't reach down toward peg from {current_bps:+.1f}bp. "
                f"No mean-reversion edge."
            )
        pullback_target = min(0.0, p80_low) if slack_to_peg > 0 else point
        return True, (
            f"{symbol} trading {current_bps:.2f}bp above peg with the "
            f"model's 80% band reaching down to {p80_low:+.1f}bp. "
            f"Short position takes the pullback toward "
            f"{pullback_target:+.1f}bp if reversion materialises."
        )
    return False, "no deviation to trade"


def _size_position(cone_half_bps: float, normal_bps: Optional[float]) -> float:
    """Scale the notional down when uncertainty is high. A cone twice
    the normal width gets half the position. Bounded between 25% and
    100% of the per-trade notional."""
    if normal_bps is None or normal_bps <= 0:
        return NOTIONAL_PER_TRADE
    ratio = cone_half_bps / normal_bps
    scale = max(0.25, min(1.0, 1.0 / max(ratio, 1.0)))
    return NOTIONAL_PER_TRADE * scale


def _build_one_token_block(store, raw_sym: str) -> dict | None:
    """Build one token's block. Used by build_token_blocks_for_trader's
    ThreadPoolExecutor — kept as a top-level function so the executor
    can pickle / dispatch it cleanly."""
    sym_u = (raw_sym or "").upper()
    if not sym_u:
        return None
    try:
        ticks = store.list_peg_ticks(sym_u, limit=1) or []
    except Exception:  # noqa: BLE001
        ticks = []
    current = None
    if ticks:
        try:
            current = float(ticks[0].get("deviation_bps") or 0.0)
        except (TypeError, ValueError):
            current = None
    try:
        preds = store.list_predictions(
            symbol=sym_u, kind="peg_deviation", limit=1) or []
    except Exception:  # noqa: BLE001
        preds = []
    latest_pred = preds[0] if preds else None
    ctx = get_context(sym_u) or get_context(raw_sym)
    # Defensive: cone_thresholds_bps must be a 2-element sequence.
    # A malformed registry entry would raise IndexError if we just
    # indexed it; treat as unset.
    cone_normal = None
    cone_alert = None
    if ctx is not None:
        try:
            cone_thr = getattr(ctx, "cone_thresholds_bps", None)
            if cone_thr and len(cone_thr) >= 2:
                cone_normal = float(cone_thr[0])
                cone_alert = float(cone_thr[1])
        except (TypeError, ValueError, IndexError):
            cone_normal = None
            cone_alert = None
    meta = {
        "yield_bearing": bool(getattr(ctx, "yield_bearing", False)),
        "venue_type": getattr(ctx, "venue_type", "CEX"),
        "cone_normal_bps": cone_normal,
        "cone_alert_bps": cone_alert,
    }
    return {
        "symbol": sym_u,
        "current_bps": current,
        "meta": meta,
        "latest_prediction": latest_pred,
    }


def build_token_blocks_for_trader(symbols: list[str]) -> list[dict]:
    """Build the minimal per-token payload the trader reads: latest
    peg deviation + latest prediction + structural meta. Mirrors the
    `/api/simulator/feed` token shape but trims to what evaluate_cycle
    actually uses. Kept here so the trader doesn't import from the
    web layer.

    Performance: each token requires two store reads (peg_ticks +
    predictions). Serialised, 18 symbols × ~150ms = ~2.7s blocking the
    ticker. Parallel via ThreadPoolExecutor brings this to ~300ms wall
    clock — the same discipline as web/server.py's /feed builder.
    """
    from sca.store import get_store
    from concurrent.futures import ThreadPoolExecutor
    store = get_store()
    syms = [s for s in (symbols or []) if s]
    if not syms:
        return []
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, len(syms))) as pool:
        futures = [pool.submit(_build_one_token_block, store, s)
                   for s in syms]
        for fut in futures:
            try:
                block = fut.result(timeout=10)
            except Exception:  # noqa: BLE001
                block = None
            if block is not None:
                out.append(block)
    # Preserve input order — futures may complete out of order.
    order = {s.upper(): i for i, s in enumerate(syms)}
    out.sort(key=lambda b: order.get(b["symbol"], 999))
    return out


# ── public API ───────────────────────────────────────────────────────
def evaluate_cycle(feed_tokens: list[dict], *, now_iso: str) -> list[Trade]:
    """Run the trader once over the latest feed. Returns the list of
    NEW trades opened this cycle (may be empty). Also marks-to-market
    + resolves any open trades that have reached their horizon.

    Caller (the ticker loop or an explicit endpoint) is expected to
    invoke this once per cycle. Idempotent within a cycle: re-running
    with the same feed won't open duplicate trades because we key
    open trades by (symbol, prediction_made_at) and skip when a
    matching open trade already exists.
    """
    with _TRADER_LOCK:
        trades = _load_trades()
        opened_now: list[Trade] = []

        # 1. Try to open new trades for each token. Cap total open
        # notional across all symbols.
        open_set = {
            (t.symbol, t.prediction_made_at)
            for t in trades if t.status == "open"
        }
        current_open_notional = sum(
            t.notional_usd for t in trades if t.status == "open"
        )
        for tok in feed_tokens or []:
            sym = (tok.get("symbol") or "").upper()
            if not sym:
                continue
            pred = tok.get("latest_prediction") or {}
            meta = tok.get("meta") or {}
            current = tok.get("current_bps")
            should, reason = _decide_to_trade(sym, current, pred, meta)
            if not should:
                continue
            key = (sym, pred.get("made_at", ""))
            if key in open_set:
                continue
            if current_open_notional + NOTIONAL_PER_TRADE > MAX_OPEN_NOTIONAL:
                log_event(
                    "trader.skipped.notional_cap", level="info",
                    symbol=sym,
                    open_notional=current_open_notional,
                    cap=MAX_OPEN_NOTIONAL,
                )
                break
            # Direction is keyed off the sign of current_bps. The
            # _decide_to_trade gate above already verifies current is
            # a finite number AND |current| >= ENTRY_THRESHOLD_BPS,
            # so by here we know it's a non-zero number. Belt-and-
            # braces: if a future _decide_to_trade refactor lets None
            # through, fail closed (skip the trade) rather than open
            # an "unsigned" short by accident.
            if not isinstance(current, (int, float)):
                log_event(
                    "trader.skipped.non_numeric_current",
                    level="warn", symbol=sym,
                    current_type=type(current).__name__,
                )
                continue
            direction = "long" if current < 0 else "short"
            cone_half = (pred["p80_high"] - pred["p80_low"]) / 2
            notional = _size_position(cone_half, meta.get("cone_normal_bps"))
            trade = Trade(
                id=f"t{int(time.time() * 1000)}-{sym}",
                symbol=sym,
                direction=direction,
                opened_at=now_iso,
                resolves_at=pred.get("resolves_at", ""),
                prediction_made_at=pred.get("made_at", ""),
                entry_bps=float(current),
                forecast_point_bps=float(pred["point"]),
                p80_low=float(pred["p80_low"]),
                p80_high=float(pred["p80_high"]),
                notional_usd=round(notional, 2),
                cone_width_bps=round(2 * cone_half, 2),
                confidence_word=pred.get("confidence_word", ""),
                rationale=reason,
            )
            trades.append(trade)
            opened_now.append(trade)
            current_open_notional += notional
            log_event(
                "trader.trade.opened", level="info",
                symbol=sym, direction=direction,
                entry_bps=current, target_bps=pred["point"],
                notional_usd=notional,
            )

        # 2. Mark-to-market + resolve open trades. For each open
        # trade, if its resolves_at has passed AND we have a fresh
        # current_bps for that symbol, settle.
        from datetime import datetime, timezone
        try:
            now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            now_dt = datetime.now(timezone.utc)
        feed_by_sym = {
            (t.get("symbol") or "").upper(): t for t in (feed_tokens or [])
        }
        for trade in trades:
            if trade.status != "open":
                continue
            tok = feed_by_sym.get(trade.symbol.upper())
            if not tok:
                continue
            current = tok.get("current_bps")
            if not isinstance(current, (int, float)):
                continue
            try:
                resolves_dt = datetime.fromisoformat(
                    trade.resolves_at.replace("Z", "+00:00"))
            except (ValueError, TypeError, AttributeError):
                continue
            if now_dt < resolves_dt:
                continue
            # P&L: long profits from a recovery (entry was low, exit is higher).
            # Short profits from a pullback (entry was high, exit is lower).
            if trade.direction == "long":
                pnl_bps = current - trade.entry_bps
            else:
                pnl_bps = trade.entry_bps - current
            pnl_usd = round(pnl_bps * trade.notional_usd * 0.0001, 2)
            trade.exit_bps = float(current)
            trade.resolved_at_real = now_iso
            trade.pnl_bps = round(pnl_bps, 4)
            trade.pnl_usd = pnl_usd
            trade.status = "resolved"
            trade.outcome_note = (
                f"{trade.direction.upper()} {trade.symbol} settled at "
                f"{current:+.2f}bp ({'profit' if pnl_usd >= 0 else 'loss'} "
                f"of ${abs(pnl_usd):.2f})"
            )
            log_event(
                "trader.trade.resolved", level="info",
                trade_id=trade.id, symbol=trade.symbol,
                direction=trade.direction, pnl_usd=pnl_usd,
                pnl_bps=pnl_bps,
            )

        _save_trades(trades)
        return opened_now


def track_record() -> dict:
    """Aggregate stats over all resolved trades for the UI summary
    line. Wins / losses / net P&L / win-rate / mean trade."""
    with _TRADER_LOCK:
        trades = _load_trades()
    resolved = [t for t in trades if t.status == "resolved"
                 and t.pnl_usd is not None]
    if not resolved:
        return {
            "count_resolved": 0, "count_open": sum(
                1 for t in trades if t.status == "open"),
            "wins": 0, "losses": 0, "net_pnl_usd": 0.0,
            "win_rate": None, "mean_trade_usd": None,
            "persona": PERSONA_NAME, "tagline": PERSONA_TAGLINE,
            "version": TRADER_VERSION,
        }
    wins = [t for t in resolved if (t.pnl_usd or 0) > 0]
    losses = [t for t in resolved if (t.pnl_usd or 0) < 0]
    net = round(sum(t.pnl_usd or 0 for t in resolved), 2)
    return {
        "count_resolved": len(resolved),
        "count_open": sum(1 for t in trades if t.status == "open"),
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl_usd": net,
        "win_rate": round(len(wins) / len(resolved), 3),
        "mean_trade_usd": round(net / len(resolved), 2),
        "persona": PERSONA_NAME,
        "tagline": PERSONA_TAGLINE,
        "version": TRADER_VERSION,
    }
