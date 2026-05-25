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
DAILY_BUDGET_USD = 100_000.0    # USD — resets at 00:00 UTC daily
MAX_OPEN_NOTIONAL = 200_000.0   # absolute concurrent-position cap
ENTRY_THRESHOLD_BPS = 3.0       # only trade meaningful deviations
MIN_EDGE_BPS = 1.0              # absolute minimum predicted movement
EDGE_FULL_SIZE_BPS = 5.0        # edge at which the conviction tier saturates
TRADER_VERSION = "discipline_v5"

# v5 conviction tiers. The previous version sized every trade at
# $10k base, shrinking on weak signals. The result was 18 trades on
# $100k notional earning $0.49 total — noise-fishing. v5 inverts the
# logic: weak signals get SKIPPED, not downsized. Real desks
# concentrate capital on conviction.
#
# HIGH:  ≥ 5bp edge, ≥ 4 agreeing sources, cone within normal.
#        → $50k notional. The "this is the trade" call.
# MED:   ≥ 3bp edge, ≥ 3 agreeing sources, cone ≤ 1.5× normal.
#        → $15k notional. Normal-quality opportunity.
# LOW:   anything else passing MIN_EDGE_BPS.
#        → SKIPPED. A real trader wouldn't take a $7k position to
#        scrape 80¢. Better to wait for the next setup.
CONVICTION_NOTIONAL = {
    "HIGH": 50_000.0,
    "MED":  15_000.0,
}
HIGH_EDGE_BPS = 5.0
HIGH_MIN_SOURCES = 4
MED_EDGE_BPS = 3.0
MED_MIN_SOURCES = 3
MED_CONE_RATIO_MAX = 1.5

# v5 structural-depeg refusal. When a token sits beyond 50bp deviation
# for at least this many consecutive cycles, its mean-reversion
# assumption is broken (USDD has been -39bp for hours, FRAX -78bp
# for days). The trader REFUSES to trade these — every cycle that
# longs USDD expecting reversion to $1 wastes a position slot.
STRUCTURAL_DEPEG_THRESHOLD_BPS = 50.0
STRUCTURAL_DEPEG_MIN_CYCLES = 5


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
    # v5 — market context. Recent web-search results surfaced at
    # decision time so the rationale + receipt include the news /
    # corpus state the trader could see. Cached aggressively via
    # brave_context.py (12h TTL + dual interest gate + daily quota
    # cap), so adding this layer does NOT burn through the Brave
    # token budget.
    entry_news_context: list = field(default_factory=list)
    # v6 — capital-cost rationale. When position size is shrunk
    # because the token locks capital up (e.g. sUSDe 7-day cooldown
    # gets 70% notional), the receipt explains why. Empty/null when
    # no shrink was applied — keeps fast-redemption trade receipts
    # clean.
    payout_timeline_label: Optional[str] = None
    capital_cost_scale: Optional[float] = None  # 1.0 = no shrink; 0.3 = 30% of base
    # v5 — conviction tier the trader assigned to this setup at open.
    # Receipt shows it; track_record can attribute outcomes by tier.
    conviction: Optional[str] = None  # 'HIGH' | 'MED'
    # v5 — LLM-narrated rationale (1-2 sentences in the trader's
    # voice). Generated once at open, cached on the trade row. Falls
    # back to a deterministic template if the LLM call fails.
    narration: str = ""
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


# Capital-cost weighting: estimated days-to-cash by payout label.
# Gemini-audit finding 4.2: a trade that locks up capital for 7 days
# costs more than one that resolves same-day. Scale down position
# size on slow-redemption assets so the $100k daily budget doesn't
# sit idle in sUSDe's 7-day cooldown when same-day USDC opportunities
# are available.
_PAYOUT_DAYS_BY_LABEL = {
    "same-day": 0.25,                       # ~6 hours typical
    "instant on-chain": 0.05,               # AMM swap + Circle off-ramp
    "instant on-chain (premium)": 0.05,
    "T+0 to T+2": 1.0,                      # midpoint
    "T+0 mint/redeem (capped)": 0.25,
    "T+1": 1.0,
    "T+1 to T+2": 1.5,
    "PSM swap": 0.5,                        # plus CEX off-ramp
    "AMM exit only": 0.1,                   # liquidity-dependent
    "7-day cooldown": 7.0,
    "40-day lockup, then daily": 40.0,
}
# Annualised capital cost — what the trader's $100k could earn risk-
# free per day if not deployed. 4.5% / 365 ≈ 0.0123% per day. A 7-day
# lockup at 0.0123%/day costs ~86bp of opportunity vs same-day; the
# weighting formula reflects this honestly.
_CAPITAL_COST_PER_DAY = 0.045 / 365  # 4.5% APY


def _capital_cost_factor(payout_label: Optional[str]) -> float:
    """Returns a multiplier in (0, 1] that shrinks position size based
    on time-to-cash. A label we don't know defaults to 1.0 (no shrink)
    so the addition is conservative — only KNOWN slow-redemption
    assets get downsized."""
    if not payout_label:
        return 1.0
    # Unknown label → days=0 (no penalty). Only KNOWN slow-redemption
    # labels are penalised, so adding this layer can't silently shrink
    # a token whose timeline we haven't characterised.
    days = _PAYOUT_DAYS_BY_LABEL.get(payout_label, 0.0)
    if days <= 0.25:  # same-day-or-faster — no penalty
        return 1.0
    # Discount factor: notional /= (1 + capital_cost * days_locked).
    # A 7-day lockup: 1 / (1 + 0.000123 × 7) ≈ 0.999 — small but real.
    # The penalty compounds for longer locks (40 days → ~0.995).
    # We rescale to make the impact visible at trader timescales: a
    # 7-day position gets 70% of full size, 40-day gets 30%.
    if days >= 30:
        return 0.30
    if days >= 7:
        return 0.70
    # Anything > 0.25 days that didn't hit the bigger gates above
    # (e.g. T+1, T+1 to T+2, PSM swap, AMM-exit-only) gets the mild
    # 0.85× penalty — capital is tied up for at least a trading day.
    return 0.85


def _is_structurally_depegged(symbol: str, current_bps: float) -> bool:
    """A token whose deviation has sat beyond ±50bp for 5+ consecutive
    ticks is NOT mean-reverting — it's a broken peg (USDD has been
    at -39bp for hours; longing it expecting return-to-$1 just burns
    cycles). Refuse to trade.

    Cheap check — only hits the store when the current snapshot is
    already in the danger zone."""
    if abs(current_bps) < STRUCTURAL_DEPEG_THRESHOLD_BPS:
        return False
    try:
        from sca.store import get_store
        store = get_store()
        ticks = store.list_peg_ticks(symbol, limit=STRUCTURAL_DEPEG_MIN_CYCLES)
    except Exception:  # noqa: BLE001
        return False
    if len(ticks) < STRUCTURAL_DEPEG_MIN_CYCLES:
        return False
    # Every recent tick must be beyond threshold on the same side
    sign = 1 if current_bps > 0 else -1
    threshold = STRUCTURAL_DEPEG_THRESHOLD_BPS * sign
    for t in ticks:
        bps = t.get("deviation_bps")
        if bps is None:
            return False
        if sign > 0 and bps < threshold:
            return False
        if sign < 0 and bps > threshold:
            return False
    return True


def _classify_conviction(
    edge_bps: float,
    cone_half_bps: float,
    normal_bps: Optional[float],
    source_count: int,
    consensus_kind: str,
) -> Optional[str]:
    """Return 'HIGH', 'MED', or None (skip).

    A real desk concentrates on the best handful of setups per day,
    not 16 mediocre ones. Anything below MED quality is skipped —
    not downsized — because $7k positions on 1-3bp edges earn cents
    after slippage and waste position slots that a real opportunity
    could use later in the day."""
    if consensus_kind == "disputed":
        return None
    norm = normal_bps if (normal_bps and normal_bps > 0) else 8.0
    cone_ratio = cone_half_bps / norm
    if (edge_bps >= HIGH_EDGE_BPS
            and source_count >= HIGH_MIN_SOURCES
            and cone_ratio <= 1.0):
        return "HIGH"
    if (edge_bps >= MED_EDGE_BPS
            and source_count >= MED_MIN_SOURCES
            and cone_ratio <= MED_CONE_RATIO_MAX):
        return "MED"
    return None


def _size_position(
    conviction: str,
    payout_label: Optional[str] = None,
) -> float:
    """Conviction tier × capital-cost discount.

    No tunable edge/cone shrinks here — the conviction classifier
    already filtered weak signals out entirely. Once a trade is
    eligible, its size is the tier's notional times the capital-cost
    factor (sUSDe 7-day cooldown → 70%, USDY 40-day → 30%, same-day
    tokens unaffected)."""
    base = CONVICTION_NOTIONAL.get(conviction, 0.0)
    return base * _capital_cost_factor(payout_label)


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
        "payout_timeline_label": getattr(ctx, "payout_timeline_label", None),
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
            if current_open_notional >= MAX_OPEN_NOTIONAL:
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
            # v5: refuse structurally-depegged tokens. USDD has sat at
            # -39bp for hours; longing it every cycle expecting
            # reversion is the noise-fishing pattern v5 is built to
            # stop. Same logic catches FRAX, any future broken peg.
            #
            # EXCEPTION: yield-bearing tokens (sUSDe, USDY) are
            # EXPECTED to live far from $1 — their thesis is NAV-
            # relative, not peg-relative. Skip the structural-depeg
            # check; the nav_discount strategy enforces its own
            # discipline (NAV proxy threshold).
            if (not meta.get("yield_bearing")
                    and _is_structurally_depegged(sym, current)):
                log_event(
                    "trader.skipped.structural_depeg", level="info",
                    symbol=sym, strategy=c.strategy,
                    current_bps=round(current, 2),
                )
                continue
            # v5: conviction tier decides everything. Weak signals
            # are skipped, not downsized — a real desk waits for the
            # next setup rather than nibbling on 1-3bp edges.
            source_count = len(consensus.get("sources") or [])
            conviction = _classify_conviction(
                edge_bps=c.edge_bps,
                cone_half_bps=cone_half_p80,
                normal_bps=meta.get("cone_normal_bps"),
                source_count=source_count,
                consensus_kind=consensus.get("kind", "") or "",
            )
            if conviction is None:
                log_event(
                    "trader.skipped.low_conviction", level="info",
                    symbol=sym, strategy=c.strategy,
                    edge_bps=round(c.edge_bps, 2),
                    sources=source_count,
                    consensus_kind=consensus.get("kind", ""),
                )
                continue
            notional = _size_position(
                conviction, payout_label=meta.get("payout_timeline_label"))
            # Day-budget gate. Refuse when remainder can't fund even
            # a half-tier-MED position ($7.5k floor) — better to skip
            # the rest of the day than open dust positions just
            # because budget exists.
            min_meaningful = CONVICTION_NOTIONAL["MED"] * 0.5
            if notional > day_budget_remaining:
                if day_budget_remaining < min_meaningful:
                    log_event(
                        "trader.skipped.daily_budget",
                        level="info", symbol=sym, strategy=c.strategy,
                        remaining=round(day_budget_remaining, 2),
                        floor=min_meaningful,
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

            # Market context — pull Brave web-search results for the
            # symbol from the existing cached layer (12h TTL + interest
            # gate + daily quota cap). Cheap when cached (just a JSON
            # read); never makes a network call when on calm forecast
            # OR the cache is warm. Stamps the receipt with the news
            # the trader could see at decision time.
            news_context = []
            try:
                from sca.movement.brave_context import fetch_context_for
                news_context = fetch_context_for(
                    sym, kind="peg_deviation",
                    confidence_word=pred.get("confidence_word"),
                    point_value=pred.get("point"),
                )[:3]  # cap to 3 results on the trade row; full list cached
            except Exception:  # noqa: BLE001 — never block a trade on the layer
                news_context = []

            # v5.1: strategy-specific horizon override. Hard-depeg
            # trades hold for hours-to-days because that's how real
            # depeg recoveries play out (SVB-era USDC took 72h);
            # closing them after the prediction's 30min default
            # captures pennies of intra-window noise instead of the
            # actual move. NAV-discount holds for days. Mean-reversion
            # and volatility-regime use the prediction's native horizon.
            horizon_override = (c.extras or {}).get("horizon_min_override")
            if horizon_override and isinstance(horizon_override, (int, float)):
                from datetime import datetime, timedelta, timezone
                try:
                    base_dt = datetime.fromisoformat(
                        now_iso.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    base_dt = datetime.now(timezone.utc)
                override_resolves_at = (
                    base_dt + timedelta(minutes=int(horizon_override))
                ).isoformat(timespec="seconds")
                override_horizon_min = int(horizon_override)
            else:
                override_resolves_at = pred.get("resolves_at", "")
                override_horizon_min = _safe_int(pred.get("horizon_minutes"))
            trade = Trade(
                id=f"t{int(time.time() * 1000)}-{sym}",
                symbol=sym,
                direction=c.direction,
                opened_at=now_iso,
                resolves_at=override_resolves_at,
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
                horizon_minutes=override_horizon_min,
                strategy=c.strategy,
                paired_with=c.paired_with,
                venue_outlier=c.venue_outlier,
                priority_score=c.priority_score,
                entry_news_context=news_context,
                payout_timeline_label=meta.get("payout_timeline_label"),
                capital_cost_scale=_capital_cost_factor(
                    meta.get("payout_timeline_label")),
                conviction=conviction,
            )
            # v5: generate the narration BEFORE persisting so it
            # rides on the trade row. Falls back to a deterministic
            # template when the LLM is unavailable — never blocks
            # the open path.
            try:
                from sca.movement.trader_voice import generate_trade_narration
                trade.narration = generate_trade_narration(asdict(trade))
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "trader.narration_failed", level="warn",
                    symbol=sym, error_class=type(exc).__name__,
                )
                trade.narration = ""
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
            # v5: dual-write to the snapshot archive. The local JSON
            # remains the read-cache; this is the durable copy that
            # joins peg_ticks / predictions / resolutions in the same
            # archive (audit finding 3.1). Never blocks the open path —
            # a Supabase failure is just a log event.
            try:
                from sca.store import get_store
                get_store().insert_trade(asdict(trade))
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "trader.insert_trade.failed", level="warn",
                    symbol=sym, error_class=type(exc).__name__,
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
            # STALE-DATA detection. Many "FLAT" outcomes were not the
            # market being quiet — they were the trader reading the
            # SAME peg_tick row at entry and at exit because the only
            # source available (CoinGecko, rate-limited) hadn't returned
            # a fresh price. We can't honestly evaluate a trade whose
            # entry and exit reference the same source-tick, so we mark
            # it STALE and exclude it from win/loss math.
            entry_sources = list(trade.entry_sources or [])
            exit_sources = list((tok.get("consensus") or {}).get("sources") or [])
            entry_fetched_at = None
            exit_fetched_at = None
            for s in entry_sources:
                fa = s.get("fetched_at")
                if isinstance(fa, (int, float)) and (
                        entry_fetched_at is None or fa > entry_fetched_at):
                    entry_fetched_at = float(fa)
            for s in exit_sources:
                fa = s.get("fetched_at")
                if isinstance(fa, (int, float)) and (
                        exit_fetched_at is None or fa > exit_fetched_at):
                    exit_fetched_at = float(fa)
            stale_data = (
                entry_fetched_at is not None
                and exit_fetched_at is not None
                and abs(exit_fetched_at - entry_fetched_at) < 0.5
                # within half a second = same source tick row
            ) or (
                # Fallback heuristic when fetched_at isn't available:
                # exit bps EXACTLY equals entry to 4dp on a single-
                # source token is almost certainly stale data.
                isinstance(current, (int, float))
                and abs(current - trade.entry_bps) < 1e-6
                and (trade.entry_consensus_kind == "single"
                     or not entry_sources)
            )
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
            # Outcome label. STALE = no fresh data between entry + exit,
            # un-evaluable, excluded from win/loss math. FLAT = market
            # genuinely didn't move enough to register a sign.
            if stale_data:
                trade.outcome = "STALE"
            elif pnl_usd > 0:
                trade.outcome = "WIN"
            elif pnl_usd < 0:
                trade.outcome = "LOSS"
            else:
                trade.outcome = "FLAT"
            if trade.outcome == "STALE":
                trade.outcome_note = (
                    f"{trade.direction.upper()} {trade.symbol}: no "
                    f"fresh source data between entry and resolve "
                    f"(source rate-limited or unchanged). Trade not "
                    f"evaluated; excluded from win/loss math."
                )
            else:
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
            # v5: patch the resolution into the snapshot archive.
            try:
                from sca.store import get_store
                get_store().update_trade_resolution(trade.id, {
                    "status": "resolved",
                    "exit_bps": trade.exit_bps,
                    "resolved_at_real": trade.resolved_at_real,
                    "pnl_usd": trade.pnl_usd,
                    "pnl_bps": trade.pnl_bps,
                    "outcome": trade.outcome,
                    "outcome_note": trade.outcome_note,
                    "exit_sources": trade.exit_sources,
                    "exit_consensus_kind": trade.exit_consensus_kind,
                    "exit_max_disagreement_bps":
                        trade.exit_max_disagreement_bps,
                    "exit_consensus_price": trade.exit_consensus_price,
                    "landed_inside_p50": trade.landed_inside_p50,
                    "landed_inside_p80": trade.landed_inside_p80,
                    "landed_inside_p95": trade.landed_inside_p95,
                })
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "trader.update_trade.failed", level="warn",
                    trade_id=trade.id, error_class=type(exc).__name__,
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
    # STALE trades are resolved-but-un-evaluable — exclude from
    # win/loss math but surface the count so the user knows.
    stale = [t for t in resolved if t.outcome == "STALE"]
    decided = [t for t in resolved if t.outcome != "STALE"]
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
            "starting_capital_usd": 100_000.0,
            "account_equity_usd": 100_000.0,
            "account_return_pct": 0.0,
            "pnl_today_usd": 0.0,
            "pnl_24h_usd": 0.0,
            "pnl_7d_usd": 0.0,
            "equity_curve": [],
            "daily": [],
            "best_day": None,
            "worst_day": None,
            "per_strategy": [],
        }

    # Order DECIDED trades chronologically for streaks + equity curve.
    # STALE trades don't count toward streaks or W/L math because they
    # were never actually evaluated.
    by_time = sorted(
        decided, key=lambda t: t.resolved_at_real or t.opened_at or "")
    wins = [t for t in decided if (t.pnl_usd or 0) > 0]
    losses = [t for t in decided if (t.pnl_usd or 0) < 0]
    net = round(sum(t.pnl_usd or 0 for t in decided), 2)

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

    # Per-day aggregates — last 14 UTC days. STALE trades count
    # toward "trades attempted" but not toward wins/losses (the
    # daily ledger needs the full attempted volume for the user
    # to see real activity).
    daily_map: dict[str, dict] = {}
    for t in resolved:
        day = t.day_utc or (t.resolved_at_real or "")[:10]
        if not day:
            continue
        agg = daily_map.setdefault(day, {
            "day_utc": day, "trades": 0, "wins": 0, "losses": 0,
            "stale": 0, "pnl_usd": 0.0, "notional_usd": 0.0,
        })
        agg["trades"] += 1
        if t.outcome == "STALE":
            agg["stale"] += 1
        elif (t.pnl_usd or 0) > 0:
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
    # When only one trading day exists, "best" and "worst" point at the
    # same row — which is misleading and the user has flagged it twice.
    # Surface a separate `single_day` field; UI shows that block instead
    # of the duplicated best+worst pair.
    by_pnl = sorted(
        [d for d in daily_map.values() if d["trades"] > 0],
        key=lambda d: d["pnl_usd"], reverse=True)
    single_day = None
    if len(by_pnl) == 0:
        best_day = None
        worst_day = None
    elif len(by_pnl) == 1:
        # Only one day on record — don't pretend it's both best and worst.
        single_day = by_pnl[0]
        best_day = None
        worst_day = None
    else:
        best_day = by_pnl[0]
        worst_day = by_pnl[-1]

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

    # Account equity + time-windowed P&L. The "account" framing makes
    # the system legible to a professional reader: simulated capital
    # starts at $10,000, equity = starting + cumulative_pnl. Windows
    # let the reader see "today the trader is up $5" vs "all-time
    # +$23" — short-term vs durable performance.
    # Editorial anchor — the account starts at $100k of simulated
    # capital. Sized to match the v4.5 daily budget so a "spend all
    # of today's allocation" looks like a meaningful 100% deployment.
    STARTING_CAPITAL_USD = 100_000.0
    account_equity = round(STARTING_CAPITAL_USD + net, 2)
    account_return_pct = round((net / STARTING_CAPITAL_USD) * 100, 3)
    # Cutoff timestamps for windows.
    from datetime import timedelta as _td
    now_dt = datetime.now(timezone.utc)
    cutoff_24h = (now_dt - _td(hours=24)).isoformat(timespec="seconds")
    cutoff_7d = (now_dt - _td(days=7)).isoformat(timespec="seconds")
    pnl_24h = round(sum(
        (t.pnl_usd or 0) for t in resolved
        if (t.resolved_at_real or "") >= cutoff_24h), 2)
    pnl_7d = round(sum(
        (t.pnl_usd or 0) for t in resolved
        if (t.resolved_at_real or "") >= cutoff_7d), 2)
    pnl_today = round(sum(
        (t.pnl_usd or 0) for t in resolved
        if (t.day_utc or "") == today_utc), 2)

    # v5: surface the trader voice — daily brief (today) +
    # reflection (yesterday's review). Read-only here: track_record
    # doesn't generate, only reads cached. Generation is triggered
    # by the ticker cycle and the resolve cycle respectively.
    brief = None
    reflection = None
    try:
        from sca.movement.trader_voice import get_daily_brief, get_reflection
        brief_obj = get_daily_brief(today_utc)
        if brief_obj is not None:
            brief = asdict(brief_obj)
        # Yesterday's reflection — the "what happened" companion to
        # today's brief.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)
                      ).strftime("%Y-%m-%d")
        refl_obj = get_reflection(yesterday)
        if refl_obj is not None:
            reflection = asdict(refl_obj)
    except Exception:  # noqa: BLE001
        pass

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
        # Account equity story
        "starting_capital_usd": STARTING_CAPITAL_USD,
        "account_equity_usd": account_equity,
        "account_return_pct": account_return_pct,
        "pnl_today_usd": pnl_today,
        "pnl_24h_usd": pnl_24h,
        "pnl_7d_usd": pnl_7d,
        "equity_curve": equity_curve[-50:],  # last 50 points for the sparkline
        "daily": daily,
        "best_day": best_day,
        "worst_day": worst_day,
        "single_day": single_day,
        "per_strategy": per_strategy,
        # v5 trader voice payload
        "daily_brief": brief,
        "yesterday_reflection": reflection,
    }
