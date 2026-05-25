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
# Capital sizing (v3 — May 2026 rework):
#   • $10,000 daily budget per UTC day, fresh at 00:00 UTC.
#   • Default position $2,000 — fits 5 full-size trades per day so the
#     tape has variety. Position scales DOWN with cone width + UP with
#     edge magnitude (see _size_position).
#   • Concurrent cap raised to $20,000 (10 trades at default size) so
#     the trader isn't constantly bumping the cap.
#
# Entry rule (v3): switched from "cone reaches peg" to "trade the
# edge." Detail:
#   - Below peg → look at p80_high. If p80_high > current_bps, the
#     model expects an upward move of at least (p80_high - current).
#     Take a long; size scales with that gap.
#   - Above peg → look at p80_low. If p80_low < current_bps, the
#     model expects a pullback. Take a short.
#   - Demand at least MIN_EDGE_BPS of expected movement before entering;
#     anything smaller is microstructure noise.
#
# This unlocks the common case where the cone is narrow but offset
# from current (the model says "I think peg moves slightly toward 0"),
# without requiring the cone to fully bridge to peg.
DAILY_BUDGET_USD = 25_000.0     # USD — resets at 00:00 UTC daily (v4: bumped 10k→25k)
NOTIONAL_PER_TRADE = 2_500.0    # default per-trade size (v4: bumped 2k→2.5k)
MAX_OPEN_NOTIONAL = 40_000.0    # 16 concurrent trades absolute cap (v4: 20k→40k)
ENTRY_THRESHOLD_BPS = 3.0       # only trade meaningful deviations
MIN_EDGE_BPS = 1.0              # minimum predicted movement to enter
EDGE_FULL_SIZE_BPS = 5.0        # edge at which we deploy full notional
TRADER_VERSION = "discipline_v4"


@dataclass
class Trade:
    """One simulated trade. Stored append-only; status migrates from
    'open' → 'resolved' once the prediction horizon elapses.

    v3 fields carry the full audit trail a professional decision-
    maker needs: which sources reported what at entry + exit, the
    consensus state at each point, and the exact forecast that
    triggered the trade. This is the "receipt" the UI renders."""
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
    # v2 fields — used for daily budget tracking + the story-over-time UX.
    day_utc: str = ""         # YYYY-MM-DD, set on open; never mutated
    # v3 fields — the receipt audit trail.
    edge_bps: float = 0.0                # predicted in-favour movement at entry
    entry_sources: list = field(default_factory=list)
        # ^ list[{name, price, fetched_at}] — per-source snapshot at entry
    entry_consensus_kind: str = ""       # 'agreed' | 'single' | 'disputed'
    entry_max_disagreement_bps: float = 0.0
    entry_consensus_price: Optional[float] = None  # canonical price used
    forecast_p50_low: Optional[float] = None
    forecast_p50_high: Optional[float] = None
    forecast_p95_low: Optional[float] = None
    forecast_p95_high: Optional[float] = None
    horizon_minutes: Optional[int] = None
    # v4 fields — strategy attribution. Receipts + UI surface which
    # strategy fired (or which strategies agreed) on this trade.
    strategy: str = "mean_reversion"
    paired_with: Optional[str] = None    # pairs_divergence: other leg's symbol
    venue_outlier: Optional[str] = None  # cross_venue_arb: outlier source name
    priority_score: float = 0.0          # the score the candidate had at decision time
    # Filled when the trade resolves:
    status: str = "open"      # 'open' | 'resolved'
    exit_bps: Optional[float] = None
    resolved_at_real: Optional[str] = None
    pnl_usd: Optional[float] = None  # signed USD P&L
    pnl_bps: Optional[float] = None  # signed bp move in our favour
    outcome: str = ""         # 'WIN' | 'LOSS' | 'FLAT' (resolved trades only)
    outcome_note: str = ""
    # Exit-side receipt
    exit_sources: list = field(default_factory=list)
    exit_consensus_kind: str = ""
    exit_max_disagreement_bps: float = 0.0
    exit_consensus_price: Optional[float] = None
    # Calibration: did the actual outcome land where the model expected?
    landed_inside_p50: Optional[bool] = None
    landed_inside_p80: Optional[bool] = None
    landed_inside_p95: Optional[bool] = None


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


def _cone_half_p80(prediction: dict) -> Optional[float]:
    """Half-width of the 80% band — used for position sizing."""
    p80lo = prediction.get("p80_low") if prediction else None
    p80hi = prediction.get("p80_high") if prediction else None
    if p80lo is None or p80hi is None:
        return None
    try:
        return (float(p80hi) - float(p80lo)) / 2
    except (TypeError, ValueError):
        return None


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


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

    # v3 mean-reversion thesis. The EWMA forecast is sticky, so the
    # old rule ("cone reaches peg") was almost never satisfied on
    # tokens with persistent depegs. Real market-makers trade the
    # EDGE — the gap between where price is now and where the model
    # thinks it'll go, even if neither point is at peg.
    #
    # Below peg → check p80_high (the optimistic edge of the cone).
    #   If p80_high > current_bps, the model expects at least
    #   (p80_high - current_bps) bp of upward movement. That's our
    #   edge.
    # Above peg → mirror: check p80_low. The expected pullback is
    #   (current_bps - p80_low).
    if current_bps < 0:
        edge_bps = p80_high - current_bps
        if edge_bps < MIN_EDGE_BPS:
            return False, (
                f"model's optimistic edge ({p80_high:+.2f}bp) is only "
                f"{edge_bps:.2f}bp above current — below {MIN_EDGE_BPS}bp "
                "noise floor"
            )
        target = (max(0.0, p80_high) if p80_high > 0 else p80_high)
        return True, (
            f"{symbol} trading {abs(current_bps):.2f}bp below peg. "
            f"Model's optimistic 80% edge ({p80_high:+.2f}bp) is "
            f"{edge_bps:.2f}bp above current — long takes the recovery "
            f"toward {target:+.2f}bp."
        )
    if current_bps > 0:
        edge_bps = current_bps - p80_low
        if edge_bps < MIN_EDGE_BPS:
            return False, (
                f"model's pessimistic edge ({p80_low:+.2f}bp) is only "
                f"{edge_bps:.2f}bp below current — below {MIN_EDGE_BPS}bp "
                "noise floor"
            )
        target = (min(0.0, p80_low) if p80_low < 0 else p80_low)
        return True, (
            f"{symbol} trading {current_bps:.2f}bp above peg. "
            f"Model's pessimistic 80% edge ({p80_low:+.2f}bp) is "
            f"{edge_bps:.2f}bp below current — short takes the pullback "
            f"toward {target:+.2f}bp."
        )
    return False, "no deviation to trade"


def _size_position(
    cone_half_bps: float,
    edge_bps: float,
    normal_bps: Optional[float],
) -> float:
    """Compute the position size for this opportunity.

    Two factors:
      • Edge magnitude — how much movement the model predicts in our
        favour. Larger edge = larger position. Linear scaling to
        EDGE_FULL_SIZE_BPS, capped at 1.0.
      • Cone width — how UNCERTAIN the model is. A cone twice the
        token's normal width halves the position. Floored at 0.25
        of the default notional so we don't open dust positions.

    The final size is base × edge_scale × cone_scale.
    """
    edge_scale = min(1.0, max(0.0, edge_bps / EDGE_FULL_SIZE_BPS))
    if normal_bps is None or normal_bps <= 0:
        cone_scale = 1.0
    else:
        ratio = cone_half_bps / normal_bps
        cone_scale = max(0.25, min(1.0, 1.0 / max(ratio, 1.0)))
    # Floor at 0.25 of default so trades aren't sub-meaningful.
    combined = max(0.25, edge_scale * cone_scale)
    return NOTIONAL_PER_TRADE * combined


def _build_one_token_block(store, raw_sym: str) -> dict | None:
    """Build one token's block. Used by build_token_blocks_for_trader's
    ThreadPoolExecutor — kept as a top-level function so the executor
    can pickle / dispatch it cleanly.

    Carries the LATEST peg_tick's consensus state + per-source readings
    so the trader can stamp them on the receipt at entry/exit. Without
    this the receipt fields are empty and the audit trail is broken."""
    sym_u = (raw_sym or "").upper()
    if not sym_u:
        return None
    try:
        ticks = store.list_peg_ticks(sym_u, limit=1) or []
    except Exception:  # noqa: BLE001
        ticks = []
    current = None
    # Consensus block — used by the trader to (1) refuse to trade on
    # DISPUTED and (2) populate the receipt's source snapshot.
    consensus: dict = {
        "kind": "",
        "max_disagreement_bps": 0.0,
        "sources": [],
    }
    if ticks:
        latest_tick = ticks[0]
        try:
            current = float(latest_tick.get("deviation_bps") or 0.0)
        except (TypeError, ValueError):
            current = None
        consensus["kind"] = latest_tick.get("consensus_kind", "") or ""
        try:
            consensus["max_disagreement_bps"] = float(
                latest_tick.get("max_disagreement_bps") or 0.0)
        except (TypeError, ValueError):
            consensus["max_disagreement_bps"] = 0.0
        raw_sources = latest_tick.get("sources") or []
        if isinstance(raw_sources, list):
            consensus["sources"] = raw_sources
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
        "consensus": consensus,
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

        # 1. Try to open new trades for each token. Two capital limits:
        #    (a) MAX_OPEN_NOTIONAL — cross-day cap on concurrent open positions
        #    (b) DAILY_BUDGET_USD  — fresh allocation at 00:00 UTC each day
        # A new trade must fit BOTH.
        today_utc = (now_iso or _now_iso())[:10]  # YYYY-MM-DD
        open_set = {
            (t.symbol, t.prediction_made_at)
            for t in trades if t.status == "open"
        }
        current_open_notional = sum(
            t.notional_usd for t in trades if t.status == "open"
        )
        # Capital deployed TODAY = sum of notional for every trade
        # opened today (resolved or still open). Once spent, it's
        # spent; resolution doesn't replenish today's allocation,
        # only frees the per-position concurrent slot.
        deployed_today = sum(
            t.notional_usd for t in trades if t.day_utc == today_utc
        )
        day_budget_remaining = DAILY_BUDGET_USD - deployed_today

        # v4: gather candidates from EVERY strategy. The strategies
        # module handles per-strategy eligibility (yield-bearing skip,
        # disputed-consensus skip, threshold floor, alert-cone refuse).
        # Returns candidates sorted by priority_score descending.
        from sca.movement.strategies import all_candidates
        candidates = all_candidates(feed_tokens or [])

        for c in candidates:
            sym = c.symbol
            pred = c.prediction or {}
            meta = c.meta or {}
            consensus = c.consensus or {}
            current = c.current_bps
            # Idempotence: skip when a trade with this prediction is
            # already open. Avoids opening duplicates on re-runs of
            # the same cycle.
            key = (sym, pred.get("made_at", ""))
            if key in open_set:
                continue
            if current_open_notional + NOTIONAL_PER_TRADE > MAX_OPEN_NOTIONAL:
                log_event(
                    "trader.skipped.notional_cap", level="info",
                    symbol=sym, strategy=c.strategy,
                    open_notional=current_open_notional,
                    cap=MAX_OPEN_NOTIONAL,
                )
                break
            # Direction null-safety (audit-fix retained).
            if not isinstance(current, (int, float)):
                log_event(
                    "trader.skipped.non_numeric_current",
                    level="warn", symbol=sym, strategy=c.strategy,
                    current_type=type(current).__name__,
                )
                continue
            cone_half_p80 = _cone_half_p80(pred)
            if cone_half_p80 is None:
                # Strategies may emit candidates from non-peg-deviation
                # forecasts (future): skip when we don't have a sized
                # cone for the position formula.
                continue
            notional = _size_position(
                cone_half_p80, c.edge_bps, meta.get("cone_normal_bps"))
            # Day-budget gate. Shrinks the size if today's remainder
            # is between 25% and 100% of the requested notional;
            # refuses if below 25%.
            if notional > day_budget_remaining:
                if day_budget_remaining < NOTIONAL_PER_TRADE * 0.25:
                    log_event(
                        "trader.skipped.daily_budget",
                        level="info", symbol=sym, strategy=c.strategy,
                        remaining=round(day_budget_remaining, 2),
                        floor=NOTIONAL_PER_TRADE * 0.25,
                        day_utc=today_utc,
                    )
                    break
                notional = round(day_budget_remaining, 2)

            # Entry source snapshot for the receipt audit trail.
            sources_snapshot = list(consensus.get("sources") or [])
            entry_consensus_price = None
            for s in sources_snapshot:
                p = s.get("price")
                if isinstance(p, (int, float)) and entry_consensus_price is None:
                    entry_consensus_price = float(p)
            if entry_consensus_price is None and isinstance(current, (int, float)):
                entry_consensus_price = 1.0 + (current / 10_000.0)

            trade = Trade(
                id=f"t{int(time.time() * 1000)}-{sym}",
                symbol=sym,
                direction=c.direction,
                opened_at=now_iso,
                resolves_at=pred.get("resolves_at", ""),
                prediction_made_at=pred.get("made_at", ""),
                entry_bps=float(current),
                forecast_point_bps=float(pred.get("point") or 0.0),
                p80_low=float(pred.get("p80_low") or 0.0),
                p80_high=float(pred.get("p80_high") or 0.0),
                notional_usd=round(notional, 2),
                cone_width_bps=round(2 * cone_half_p80, 2),
                confidence_word=pred.get("confidence_word", "") or "",
                rationale=c.rationale,
                day_utc=today_utc,
                edge_bps=round(c.edge_bps, 4),
                entry_sources=sources_snapshot,
                entry_consensus_kind=consensus.get("kind", "") or "",
                entry_max_disagreement_bps=float(
                    consensus.get("max_disagreement_bps") or 0.0),
                entry_consensus_price=entry_consensus_price,
                forecast_p50_low=_safe_float(pred.get("p50_low")),
                forecast_p50_high=_safe_float(pred.get("p50_high")),
                forecast_p95_low=_safe_float(pred.get("p95_low")),
                forecast_p95_high=_safe_float(pred.get("p95_high")),
                horizon_minutes=_safe_int(pred.get("horizon_minutes")),
                strategy=c.strategy,
                paired_with=c.paired_with,
                venue_outlier=c.venue_outlier,
                priority_score=c.priority_score,
            )
            trades.append(trade)
            opened_now.append(trade)
            open_set.add(key)
            current_open_notional += notional
            day_budget_remaining -= notional
            log_event(
                "trader.trade.opened", level="info",
                symbol=sym, direction=c.direction, strategy=c.strategy,
                entry_bps=current, edge_bps=round(c.edge_bps, 2),
                notional_usd=notional,
                consensus_kind=consensus.get("kind", ""),
                source_count=len(sources_snapshot),
                priority_score=round(c.priority_score, 2),
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
            # Outcome label — used by the UI to surface clear WIN /
            # LOSS markers. FLAT is the rare exactly-zero P&L case
            # (we still distinguish it so it's not mis-counted).
            if pnl_usd > 0:
                trade.outcome = "WIN"
            elif pnl_usd < 0:
                trade.outcome = "LOSS"
            else:
                trade.outcome = "FLAT"
            trade.outcome_note = (
                f"{trade.direction.upper()} {trade.symbol} settled at "
                f"{current:+.2f}bp ({trade.outcome} "
                f"${'+' if pnl_usd >= 0 else '-'}{abs(pnl_usd):.2f})"
            )
            # Exit-side receipt: snapshot the source readings + the
            # consensus state at exit so the receipt shows the full
            # round-trip audit trail.
            exit_consensus = tok.get("consensus") or {}
            trade.exit_sources = list(exit_consensus.get("sources") or [])
            trade.exit_consensus_kind = exit_consensus.get("kind", "") or ""
            trade.exit_max_disagreement_bps = float(
                exit_consensus.get("max_disagreement_bps") or 0.0)
            # Convert bp → price for the canonical exit_consensus_price.
            trade.exit_consensus_price = 1.0 + (current / 10_000.0)
            # Calibration: did the actual exit land inside the model's
            # forecast bands? Used by the LLM judge for trust signal.
            if trade.forecast_p50_low is not None \
                    and trade.forecast_p50_high is not None:
                trade.landed_inside_p50 = (
                    trade.forecast_p50_low <= current <= trade.forecast_p50_high)
            if trade.p80_low is not None and trade.p80_high is not None:
                trade.landed_inside_p80 = (
                    trade.p80_low <= current <= trade.p80_high)
            if trade.forecast_p95_low is not None \
                    and trade.forecast_p95_high is not None:
                trade.landed_inside_p95 = (
                    trade.forecast_p95_low <= current <= trade.forecast_p95_high)
            log_event(
                "trader.trade.resolved", level="info",
                trade_id=trade.id, symbol=trade.symbol,
                direction=trade.direction, pnl_usd=pnl_usd,
                pnl_bps=pnl_bps,
            )

        _save_trades(trades)
        return opened_now


def track_record() -> dict:
    """Aggregate stats — the story-over-time payload the UI reads.

    Includes everything the v2 trader UX needs:
      • wins / losses / net P&L / win-rate
      • today: notional deployed + remaining budget
      • equity_curve: cumulative P&L points (newest last) for the
        sparkline
      • current_streak: consecutive same-outcome resolved trades
      • daily: per-day P&L breakdown (last 14 UTC days)
      • best_day / worst_day: peak / trough by P&L
    """
    from datetime import datetime, timezone, timedelta
    with _TRADER_LOCK:
        trades = _load_trades()
    resolved = [t for t in trades if t.status == "resolved"
                 and t.pnl_usd is not None]
    open_trades = [t for t in trades if t.status == "open"]
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    deployed_today = sum(
        t.notional_usd for t in trades if t.day_utc == today_utc)
    budget_remaining_today = round(
        DAILY_BUDGET_USD - deployed_today, 2)

    base = {
        "persona": PERSONA_NAME,
        "tagline": PERSONA_TAGLINE,
        "version": TRADER_VERSION,
        "today_utc": today_utc,
        "daily_budget_usd": DAILY_BUDGET_USD,
        "deployed_today_usd": round(deployed_today, 2),
        "budget_remaining_today_usd": budget_remaining_today,
        "count_open": len(open_trades),
        "open_notional_usd": round(
            sum(t.notional_usd for t in open_trades), 2),
    }

    if not resolved:
        return {
            **base,
            "count_resolved": 0, "wins": 0, "losses": 0,
            "net_pnl_usd": 0.0,
            "win_rate": None, "mean_trade_usd": None,
            "current_streak": {"outcome": None, "length": 0},
            "equity_curve": [],
            "daily": [],
            "best_day": None,
            "worst_day": None,
        }

    # Order resolved trades chronologically for streaks + equity curve.
    by_time = sorted(
        resolved, key=lambda t: t.resolved_at_real or t.opened_at or "")
    wins = [t for t in resolved if (t.pnl_usd or 0) > 0]
    losses = [t for t in resolved if (t.pnl_usd or 0) < 0]
    net = round(sum(t.pnl_usd or 0 for t in resolved), 2)

    # Current streak — walk backward through resolved trades while
    # the outcome stays consistent.
    streak_outcome: Optional[str] = None
    streak_length = 0
    for t in reversed(by_time):
        o = t.outcome
        if not o:
            o = "WIN" if (t.pnl_usd or 0) > 0 else (
                "LOSS" if (t.pnl_usd or 0) < 0 else "FLAT")
        if streak_outcome is None:
            streak_outcome = o
            streak_length = 1
        elif o == streak_outcome:
            streak_length += 1
        else:
            break

    # Equity curve — cumulative P&L after each resolved trade.
    # The UI renders this as a small sparkline.
    cumulative = 0.0
    equity_curve = []
    for t in by_time:
        cumulative += t.pnl_usd or 0
        equity_curve.append({
            "at": t.resolved_at_real or t.opened_at,
            "pnl_usd_running": round(cumulative, 2),
            "outcome": t.outcome or ("WIN" if (t.pnl_usd or 0) > 0
                                      else "LOSS"),
        })

    # Per-day aggregates — last 14 UTC days. A day appears even if
    # no trades resolved (P&L = 0) so the timeline shows the gap.
    daily_map: dict[str, dict] = {}
    for t in resolved:
        day = t.day_utc or (t.resolved_at_real or "")[:10]
        if not day:
            continue
        agg = daily_map.setdefault(day, {
            "day_utc": day, "trades": 0, "wins": 0, "losses": 0,
            "pnl_usd": 0.0, "notional_usd": 0.0,
        })
        agg["trades"] += 1
        if (t.pnl_usd or 0) > 0:
            agg["wins"] += 1
        elif (t.pnl_usd or 0) < 0:
            agg["losses"] += 1
        agg["pnl_usd"] = round(agg["pnl_usd"] + (t.pnl_usd or 0), 2)
        agg["notional_usd"] = round(
            agg["notional_usd"] + t.notional_usd, 2)
    # Fill in zero-trade days so the timeline reads contiguously.
    now_dt = datetime.now(timezone.utc)
    for back in range(14):
        d = (now_dt - timedelta(days=back)).strftime("%Y-%m-%d")
        if d not in daily_map:
            daily_map[d] = {
                "day_utc": d, "trades": 0, "wins": 0, "losses": 0,
                "pnl_usd": 0.0, "notional_usd": 0.0,
            }
    daily = sorted(daily_map.values(), key=lambda d: d["day_utc"],
                    reverse=True)[:14]

    # Best / worst day by P&L (over the full history, not just last 14).
    by_pnl = sorted(
        [d for d in daily_map.values() if d["trades"] > 0],
        key=lambda d: d["pnl_usd"], reverse=True)
    best_day = by_pnl[0] if by_pnl else None
    worst_day = by_pnl[-1] if by_pnl else None

    # Per-strategy breakdown — which strategy is winning? Each trade
    # is keyed by its `strategy` field (may be a composite like
    # "mean_reversion+cross_venue_arb" when multiple strategies
    # agreed; we count it under the LEAD strategy for headline
    # numbers but keep the full composite name on the receipt).
    strategy_map: dict[str, dict] = {}
    for t in resolved:
        strat = t.strategy or "mean_reversion"
        lead = strat.split("+")[0]
        agg = strategy_map.setdefault(lead, {
            "strategy": lead, "trades": 0, "wins": 0, "losses": 0,
            "pnl_usd": 0.0,
        })
        agg["trades"] += 1
        if (t.pnl_usd or 0) > 0:
            agg["wins"] += 1
        elif (t.pnl_usd or 0) < 0:
            agg["losses"] += 1
        agg["pnl_usd"] = round(agg["pnl_usd"] + (t.pnl_usd or 0), 2)
    # Also count OPEN trades so the panel shows live deployment per
    # strategy (useful for the user's "obvious what it's doing" ask).
    for t in open_trades:
        strat = t.strategy or "mean_reversion"
        lead = strat.split("+")[0]
        agg = strategy_map.setdefault(lead, {
            "strategy": lead, "trades": 0, "wins": 0, "losses": 0,
            "pnl_usd": 0.0,
        })
        agg["open"] = agg.get("open", 0) + 1
    per_strategy = sorted(
        strategy_map.values(),
        key=lambda s: s["pnl_usd"], reverse=True)

    return {
        **base,
        "count_resolved": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl_usd": net,
        "win_rate": round(len(wins) / len(resolved), 3),
        "mean_trade_usd": round(net / len(resolved), 2),
        "current_streak": {
            "outcome": streak_outcome,
            "length": streak_length,
        },
        "equity_curve": equity_curve[-50:],  # last 50 points for the sparkline
        "daily": daily,
        "best_day": best_day,
        "worst_day": worst_day,
        "per_strategy": per_strategy,
    }
