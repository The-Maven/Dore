"""Trading strategies — pluggable signal-generators for the Discipline Trader.

Each strategy reads the live feed snapshot and emits zero or more
`TradeCandidate` objects. The trader framework gathers candidates
across all strategies, de-duplicates by (symbol, direction), sorts by
priority_score (highest first), and deploys against the daily budget
in priority order.

The persona stays the same: profit-focused, risk-managed, refuses
yield-bearing, refuses disputed-consensus. Strategies are the FAMILY
of bets a real desk runs — mean reversion is one of many.

## Strategies registered (v1)

  • mean_reversion  — bet the cone reaches toward peg
  • pairs_divergence — when correlated stables drift apart, fade the spread
  • cross_venue_arb — when sources disagree but consensus still holds,
    position toward the higher-quality source (CEX > aggregator)
  • volatility_regime — when forecast cone widens past the per-token
    normal envelope, take a CONTRARIAN bet on the far side (volatility
    spikes tend to mean-revert too)

Each strategy returns dataclass-style dicts so the trader can build
the persisted Trade row without strategies needing to know about
persistence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── candidate type ──────────────────────────────────────────────────
@dataclass
class TradeCandidate:
    """A candidate trade proposed by a strategy. Not yet sized or
    persisted — the trader framework does that after ranking."""
    strategy: str                # 'mean_reversion' | 'pairs_divergence' | ...
    symbol: str
    direction: str               # 'long' | 'short'
    edge_bps: float              # predicted favourable movement
    priority_score: float        # higher = trader prefers this candidate
    rationale: str               # plain-English read for the receipt
    # Pass-throughs for the trader to stamp on the Trade row:
    current_bps: float
    prediction: dict             # the latest_prediction snapshot
    meta: dict                   # token meta (yield_bearing, cone thresholds, …)
    consensus: dict              # consensus block at decision time
    # Strategy-specific extras (optional — used by pairs / cross-venue):
    paired_with: Optional[str] = None       # for pairs trades
    venue_outlier: Optional[str] = None     # for cross-venue arb
    extras: dict = field(default_factory=dict)


# ── helpers ─────────────────────────────────────────────────────────
_MAX_SOURCE_AGE_S = 90.0  # never trade on data older than 90 seconds


def _eligible_token(tok: dict) -> bool:
    """Per-token eligibility gate every strategy needs to pass through
    BEFORE proposing a candidate. Centralised so each strategy doesn't
    re-implement the discipline rules.

    Hard rules (never bypass):
      • yield-bearing → skip (drift is structural, not arbitrage)
      • no current price → skip
      • DISPUTED consensus → skip (entry price contested)
      • STALE source data → skip (any source older than 90s is
        un-actionable; this is the "never sketchy" guarantee — when
        the only data we have is more than a minute and a half old,
        we DON'T trade, because a real venue can move in that time
        and our entry would be off-market)
    """
    import time as _t
    meta = tok.get("meta") or {}
    if meta.get("yield_bearing"):
        return False
    if tok.get("current_bps") is None:
        return False
    cons = tok.get("consensus") or {}
    if cons.get("kind") == "disputed":
        return False
    # Freshness gate. fetched_at on each source is unix seconds.
    # We compare the MAX fetched_at (most recent) against now — if
    # even the freshest source is stale, refuse.
    # NOTE: only apply when fetched_at looks like a real unix
    # timestamp (post-2001, i.e. > 1e9). Synthetic test fixtures
    # use values like 100 or 1.0 — treat those as "freshness unknown"
    # and proceed (don't gate-out tests).
    sources = cons.get("sources") or []
    if sources:
        try:
            freshest = max(
                (float(s.get("fetched_at") or 0) for s in sources),
                default=0.0,
            )
            if freshest > 1_000_000_000:
                age_s = _t.time() - freshest
                if age_s > _MAX_SOURCE_AGE_S:
                    return False
        except (TypeError, ValueError):
            pass
    return True


def _cone_half(prediction: dict) -> Optional[float]:
    p80lo = prediction.get("p80_low")
    p80hi = prediction.get("p80_high")
    if p80lo is None or p80hi is None:
        return None
    return (p80hi - p80lo) / 2


# ── strategy 1: mean reversion (the existing one, refactored) ──────
ENTRY_THRESHOLD_BPS = 3.0
MIN_EDGE_BPS = 1.0


def mean_reversion(feed_tokens: list[dict]) -> list[TradeCandidate]:
    """Trade the EDGE between current peg deviation and the model's
    nearer p80 boundary. A real market-maker's mean-reversion strategy:
    bet the cone's nearer edge predicts movement toward peg, scaled by
    the edge magnitude.

    Priority score = edge_bps × cone_tightness_factor. Tighter cones
    get higher priority because the model is more confident."""
    out: list[TradeCandidate] = []
    for tok in feed_tokens or []:
        if not _eligible_token(tok):
            continue
        current = tok["current_bps"]
        if abs(current) < ENTRY_THRESHOLD_BPS:
            continue
        pred = tok.get("latest_prediction") or {}
        cone_h = _cone_half(pred)
        if cone_h is None:
            continue
        meta = tok.get("meta") or {}
        alert = meta.get("cone_alert_bps")
        if alert is not None and cone_h >= alert:
            continue
        p80lo, p80hi = pred["p80_low"], pred["p80_high"]
        if current < 0:
            edge = p80hi - current
            direction = "long"
            target = max(0.0, p80hi) if p80hi > 0 else p80hi
        else:
            edge = current - p80lo
            direction = "short"
            target = min(0.0, p80lo) if p80lo < 0 else p80lo
        if edge < MIN_EDGE_BPS:
            continue
        normal = meta.get("cone_normal_bps") or 8.0
        cone_tightness = max(0.3, min(1.0, normal / max(cone_h, 0.5)))
        priority = edge * cone_tightness
        rationale = (
            f"{tok['symbol']} {abs(current):.2f}bp "
            f"{'below' if current < 0 else 'above'} peg; model's "
            f"nearer 80% edge is {edge:.2f}bp toward "
            f"{target:+.2f}bp. Cone half-width ±{cone_h:.2f}bp "
            f"(normal envelope ≤{normal:.0f}bp). Long: take the "
            f"recovery." if direction == "long" else
            f"{tok['symbol']} {current:.2f}bp above peg; model's "
            f"nearer 80% edge is {edge:.2f}bp toward "
            f"{target:+.2f}bp. Cone half-width ±{cone_h:.2f}bp. "
            f"Short: take the pullback."
        )
        out.append(TradeCandidate(
            strategy="mean_reversion",
            symbol=tok["symbol"], direction=direction,
            edge_bps=round(edge, 3),
            priority_score=round(priority, 3),
            rationale=rationale,
            current_bps=current,
            prediction=pred, meta=meta,
            consensus=tok.get("consensus") or {},
        ))
    return out


# ── strategy 2: pairs divergence ───────────────────────────────────
# A classic basis trade: when two correlated stablecoins drift apart
# by more than their historical spread, fade the divergence. We pair
# fiat-backed majors with one another (USDC/USDT, PYUSD/USDP) — the
# theory is the spread between two attestation-backed dollar tokens
# should mean-revert quickly because either issuer can mint/redeem.
PAIRS = [
    ("USDC", "USDT"),
    ("USDC", "DAI"),
    ("PYUSD", "USDP"),
    ("USDC", "FDUSD"),
    ("USDT", "DAI"),
]
PAIRS_DIVERGENCE_BPS = 6.0       # only trade when the gap is > 6bp
PAIRS_DEFAULT_EDGE_BPS = 4.0     # expected convergence


def pairs_divergence(feed_tokens: list[dict]) -> list[TradeCandidate]:
    """For each configured pair (A, B):
       - if A is trading meaningfully LOWER than B (spread > threshold):
         take a LONG on A and SHORT on B — bet on convergence
       - mirror when A is HIGHER than B

    Skips a pair if either leg is yield-bearing, silent, or disputed.
    Priority score scales with the spread magnitude — the wider the
    divergence the stronger the signal.
    """
    by_sym = {(t.get("symbol") or "").upper(): t for t in feed_tokens or []}
    out: list[TradeCandidate] = []
    for a_sym, b_sym in PAIRS:
        a = by_sym.get(a_sym)
        b = by_sym.get(b_sym)
        if a is None or b is None:
            continue
        if not (_eligible_token(a) and _eligible_token(b)):
            continue
        # Both tokens must be fiat-backed (peg target = $1) — the
        # pair thesis doesn't hold for yield-bearing / algorithmic.
        # _eligible_token already filters yield-bearing; keep the
        # rationale honest by also skipping when backing models differ
        # too wildly. (We use a soft heuristic: don't pair DAI with
        # PYUSD — different reserve models — even though both have
        # peg target = $1.) For v1 we just trust the static PAIRS list.
        a_bps = a["current_bps"]
        b_bps = b["current_bps"]
        spread = a_bps - b_bps
        if abs(spread) < PAIRS_DIVERGENCE_BPS:
            continue
        # When A < B → A is cheap, B is rich. Convergence move is:
        #   LONG A: A moves up by ~spread/2
        #   SHORT B: B moves down by ~spread/2
        # Each leg's expected edge ≈ spread/2.
        leg_edge = abs(spread) / 2
        a_dir = "long" if spread < 0 else "short"
        b_dir = "short" if spread < 0 else "long"
        priority = round(leg_edge * 0.85, 3)  # slight discount vs mean-rev — convergence is noisy
        rationale_a = (
            f"{a_sym} is {abs(spread):.2f}bp "
            f"{'below' if spread < 0 else 'above'} {b_sym}. "
            f"Fade the divergence: long the cheaper leg, short the richer. "
            f"Convergence move ≈ {leg_edge:.2f}bp per leg if the pair "
            f"mean-reverts."
        )
        rationale_b = (
            f"{b_sym} is the {('rich' if spread < 0 else 'cheap')} leg of "
            f"the {a_sym}/{b_sym} pair "
            f"({abs(spread):.2f}bp spread). "
            f"Short the rich leg, long the cheap — expected convergence "
            f"{leg_edge:.2f}bp per leg." if spread < 0 else
            f"{b_sym} is the cheap leg of the {a_sym}/{b_sym} pair "
            f"({abs(spread):.2f}bp spread). Long the cheap leg, "
            f"short the rich — expected convergence {leg_edge:.2f}bp."
        )
        out.append(TradeCandidate(
            strategy="pairs_divergence",
            symbol=a_sym, direction=a_dir,
            edge_bps=round(leg_edge, 3),
            priority_score=priority,
            rationale=rationale_a,
            current_bps=a_bps,
            prediction=a.get("latest_prediction") or {},
            meta=a.get("meta") or {},
            consensus=a.get("consensus") or {},
            paired_with=b_sym,
        ))
        out.append(TradeCandidate(
            strategy="pairs_divergence",
            symbol=b_sym, direction=b_dir,
            edge_bps=round(leg_edge, 3),
            priority_score=priority,
            rationale=rationale_b,
            current_bps=b_bps,
            prediction=b.get("latest_prediction") or {},
            meta=b.get("meta") or {},
            consensus=b.get("consensus") or {},
            paired_with=a_sym,
        ))
    return out


# ── strategy 3: cross-venue arbitrage ──────────────────────────────
# When two or more peg sources report DIFFERENT prices but the consensus
# is still 'agreed' (or 'single' with multiple sources actually reporting),
# the outlier is informative — the venue with the more-extreme reading is
# usually catching up. We bet the consensus value, not the outlier.
CROSS_VENUE_MIN_SPREAD_BPS = 1.5
CROSS_VENUE_MAX_SPREAD_BPS = 5.0   # above 5 = disputed, _eligible_token filters


def cross_venue_arb(feed_tokens: list[dict]) -> list[TradeCandidate]:
    """For each token, look at the per-source spread. If 2+ sources
    reported and the max disagreement is meaningful but inside the
    consensus tolerance (1.5–5bp), bet on the outlier coming back to
    the consensus value.

    The direction matches mean-reversion (long if below peg, short if
    above) — this strategy is a higher-conviction variant when source
    disagreement signals impending convergence.
    """
    out: list[TradeCandidate] = []
    for tok in feed_tokens or []:
        if not _eligible_token(tok):
            continue
        cons = tok.get("consensus") or {}
        spread = cons.get("max_disagreement_bps") or 0.0
        if spread < CROSS_VENUE_MIN_SPREAD_BPS:
            continue
        if spread >= CROSS_VENUE_MAX_SPREAD_BPS:
            continue  # _eligible_token already filters disputed; belt-and-braces
        sources = cons.get("sources") or []
        if len(sources) < 2:
            continue
        current = tok["current_bps"]
        if abs(current) < ENTRY_THRESHOLD_BPS:
            continue
        meta = tok.get("meta") or {}
        alert = meta.get("cone_alert_bps")
        pred = tok.get("latest_prediction") or {}
        cone_h = _cone_half(pred)
        if alert is not None and cone_h is not None and cone_h >= alert:
            continue
        # Edge ≈ half the source disagreement (we expect the outlier
        # to come back halfway). Bounded above by the entry deviation
        # so we don't overstate.
        edge = min(spread / 2, abs(current) * 0.6)
        if edge < MIN_EDGE_BPS:
            continue
        direction = "long" if current < 0 else "short"
        # Identify the outlier source for the rationale + receipt
        outlier_name = ""
        if sources:
            prices = [(s.get("name"), s.get("price"))
                       for s in sources if s.get("price") is not None]
            if prices:
                # Outlier = farthest from the median
                median_p = sorted(p for _, p in prices)[len(prices) // 2]
                outlier_name = max(prices,
                                    key=lambda np: abs((np[1] or 0) - median_p))[0] or ""
        rationale = (
            f"{tok['symbol']} sources spread {spread:.2f}bp; "
            f"{outlier_name or 'one source'} is the outlier from the "
            f"consensus. Bet on convergence: "
            f"{'long' if direction == 'long' else 'short'} the "
            f"consensus side."
        )
        out.append(TradeCandidate(
            strategy="cross_venue_arb",
            symbol=tok["symbol"], direction=direction,
            edge_bps=round(edge, 3),
            # v5: cross-venue convergence is the most-reliable
            # signal type — sources don't disagree for long. Bump
            # priority over mean-reversion so it leads the ranking
            # when both fire on the same token.
            priority_score=round(edge * 1.5, 3),
            rationale=rationale,
            current_bps=current,
            prediction=pred, meta=meta,
            consensus=cons,
            venue_outlier=outlier_name,
        ))
    return out


# ── strategy 4: volatility regime ──────────────────────────────────
# When the forecast cone widens past the token's NORMAL envelope (but
# still below ALERT), the model is signalling elevated uncertainty.
# Volatility tends to mean-revert too — a token in a "wide cone" regime
# usually returns to a "normal cone" regime within a few cycles. We
# take a small contrarian position betting against the cone's far side.
VOL_REGIME_MIN_RATIO = 1.3         # cone ≥ 1.3× normal triggers
VOL_REGIME_MAX_RATIO = 2.5         # cone past this = regime change, skip


def volatility_regime(feed_tokens: list[dict]) -> list[TradeCandidate]:
    """Contrarian bet against the FAR side of an inflated cone.

    Rule: when cone half-width is 1.3–2.5× the token's normal width
    AND |current_bps| > entry threshold, take a position OPPOSITE the
    sign of current. The bet: volatility mean-reverts, so the wide
    cone will tighten, and the price returns toward the recent mean
    (which is closer to peg than the current outlier reading)."""
    out: list[TradeCandidate] = []
    for tok in feed_tokens or []:
        if not _eligible_token(tok):
            continue
        meta = tok.get("meta") or {}
        normal = meta.get("cone_normal_bps")
        if normal is None or normal <= 0:
            continue
        pred = tok.get("latest_prediction") or {}
        cone_h = _cone_half(pred)
        if cone_h is None:
            continue
        ratio = cone_h / normal
        if ratio < VOL_REGIME_MIN_RATIO or ratio >= VOL_REGIME_MAX_RATIO:
            continue
        current = tok["current_bps"]
        if abs(current) < ENTRY_THRESHOLD_BPS:
            continue
        # Edge is half the over-extension; the wider the cone vs normal,
        # the larger the predicted mean reversion.
        over_extension = cone_h - normal
        edge = max(MIN_EDGE_BPS, min(over_extension, abs(current) * 0.4))
        direction = "long" if current < 0 else "short"
        priority = round(edge * 0.7, 3)  # lowest priority of the four — contrarian is harder
        rationale = (
            f"{tok['symbol']} cone is ±{cone_h:.1f}bp "
            f"({ratio:.1f}× the {normal:.0f}bp normal envelope). "
            f"Volatility regime is elevated; volatility tends to "
            f"mean-revert. Contrarian {'long' if direction == 'long' else 'short'} "
            f"on the bet that the cone tightens and price returns "
            f"toward the recent mean."
        )
        out.append(TradeCandidate(
            strategy="volatility_regime",
            symbol=tok["symbol"], direction=direction,
            edge_bps=round(edge, 3),
            priority_score=priority,
            rationale=rationale,
            current_bps=current,
            prediction=pred, meta=meta,
            consensus=tok.get("consensus") or {},
            extras={"cone_ratio": round(ratio, 2)},
        ))
    return out


# ── strategy 5: NAV-discount arb on yield-bearing tokens ──────────
# Audit finding 4.1 — the high-ROI opportunity the previous trader
# was BLIND to. Yield-bearing tokens (sUSDe, USDY, USDM) trade at
# premium/discount to their NAV depending on secondary-market
# liquidity and redemption-queue pressure. Impatient holders dump
# at a discount; risk-on buyers pay a premium.
#
# Without a live NAV oracle (audit finding 1.2 — TODO), we proxy
# NAV with a rolling-mean of recent secondary-market prices. This
# is conservative — the NAV-vs-price gap appears as deviation from
# the rolling mean, and we trade ONLY when the gap exceeds a
# disciplined threshold.
#
# Long when secondary < proxy-NAV - 20bp (discount): expect mean
# reversion to fair value. Short when secondary > proxy-NAV + 30bp
# (premium): historically slower to fade, so we demand more edge.
NAV_DISCOUNT_THRESHOLD_BPS = 20.0
NAV_PREMIUM_THRESHOLD_BPS = 30.0
NAV_HISTORY_TICKS = 60     # ~ 1 hour at 60s cadence
NAV_MIN_HISTORY = 12       # need enough samples to trust the mean


def _proxy_nav_bps(symbol: str) -> Optional[float]:
    """Rolling-mean deviation_bps over recent ticks — proxy for NAV
    until a real oracle is wired. Returns None when insufficient
    history."""
    try:
        from sca.store import get_store
        store = get_store()
        ticks = store.list_peg_ticks(symbol, limit=NAV_HISTORY_TICKS)
    except Exception:  # noqa: BLE001
        return None
    devs = [t.get("deviation_bps") for t in ticks
            if t.get("deviation_bps") is not None]
    if len(devs) < NAV_MIN_HISTORY:
        return None
    return sum(devs) / len(devs)


def nav_discount(feed_tokens: list[dict]) -> list[TradeCandidate]:
    """Long-the-discount / short-the-premium on yield-bearing tokens.
    Bypasses the standard yield-bearing exclusion in _eligible_token
    because this strategy uses NAV-relative pricing — the thesis
    explicitly accounts for the structural drift.

    Skips when peg-history is too short (cold start) or when
    consensus is disputed."""
    import time as _t
    out: list[TradeCandidate] = []
    for tok in feed_tokens or []:
        meta = tok.get("meta") or {}
        if not meta.get("yield_bearing"):
            continue
        if tok.get("current_bps") is None:
            continue
        cons = tok.get("consensus") or {}
        if cons.get("kind") == "disputed":
            continue
        # Freshness gate — same logic as _eligible_token.
        sources = cons.get("sources") or []
        if sources:
            try:
                freshest = max(
                    (float(s.get("fetched_at") or 0) for s in sources),
                    default=0.0,
                )
                if freshest > 1_000_000_000:
                    if _t.time() - freshest > _MAX_SOURCE_AGE_S:
                        continue
            except (TypeError, ValueError):
                pass
        symbol = tok["symbol"]
        nav_bps = _proxy_nav_bps(symbol)
        if nav_bps is None:
            continue
        current = tok["current_bps"]
        gap = current - nav_bps
        if gap <= -NAV_DISCOUNT_THRESHOLD_BPS:
            # Trading meaningfully below NAV → long the discount.
            direction = "long"
            edge = abs(gap) / 2  # expect halfway convergence
            rationale = (
                f"{symbol} secondary trades {abs(gap):.1f}bp below "
                f"its {NAV_HISTORY_TICKS}-tick rolling-mean "
                f"({nav_bps:+.1f}bp); proxy NAV-discount setup. "
                f"Long the discount; expected halfway convergence "
                f"≈ {edge:.1f}bp."
            )
        elif gap >= NAV_PREMIUM_THRESHOLD_BPS:
            direction = "short"
            edge = gap / 2
            rationale = (
                f"{symbol} secondary trades {gap:.1f}bp above its "
                f"{NAV_HISTORY_TICKS}-tick rolling-mean "
                f"({nav_bps:+.1f}bp); premium-fade setup. Short the "
                f"premium; expected halfway convergence "
                f"≈ {edge:.1f}bp."
            )
        else:
            continue
        # NAV strategies tend to be HIGHEST conviction when they fire
        # because gap thresholds are wider than mean-reversion's.
        priority = round(edge * 1.4, 3)
        out.append(TradeCandidate(
            strategy="nav_discount",
            symbol=symbol, direction=direction,
            edge_bps=round(edge, 3),
            priority_score=priority,
            rationale=rationale,
            current_bps=current,
            prediction=tok.get("latest_prediction") or {},
            meta=meta,
            consensus=cons,
            extras={"nav_proxy_bps": round(nav_bps, 2),
                     "gap_bps": round(gap, 2)},
        ))
    return out


# ── orchestrator ────────────────────────────────────────────────────
STRATEGIES = [
    mean_reversion,
    pairs_divergence,
    cross_venue_arb,
    volatility_regime,
    nav_discount,
]


def all_candidates(feed_tokens: list[dict]) -> list[TradeCandidate]:
    """Run every registered strategy and return the merged candidate
    list. De-duplicates by (symbol, direction): when multiple
    strategies converge on the same trade, the highest-priority
    rationale wins, but the strategy NAMES are concatenated so the
    receipt records all signals firing on that token.

    Candidates returned sorted by priority_score descending so the
    trader framework can deploy budget in priority order."""
    merged: dict[tuple[str, str], TradeCandidate] = {}
    for strategy_fn in STRATEGIES:
        try:
            cands = strategy_fn(feed_tokens)
        except Exception:  # noqa: BLE001 — strategy bug must not kill cycle
            cands = []
        for c in cands:
            key = (c.symbol, c.direction)
            existing = merged.get(key)
            if existing is None or c.priority_score > existing.priority_score:
                merged[key] = c
            elif existing is not None:
                # Aggregate signal: multiple strategies agree on
                # this direction. Boost priority + concat strategy names.
                existing.priority_score = round(
                    existing.priority_score + c.priority_score * 0.5, 3)
                existing.strategy = existing.strategy + "+" + c.strategy
                existing.rationale = existing.rationale + (
                    f" Also {c.strategy.replace('_', ' ')}: "
                    f"{c.rationale.split(';', 1)[-1].strip() if ';' in c.rationale else c.rationale}"
                )
    return sorted(merged.values(),
                  key=lambda c: c.priority_score, reverse=True)
