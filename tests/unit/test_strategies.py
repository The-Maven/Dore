"""Trader v4 strategies — pluggable signal generators.

Pins per-strategy behaviour: which feed shapes trigger candidates,
which don't, and the orchestrator's de-duplication / priority sort.
"""
from __future__ import annotations

from sca.movement import strategies


# ── shared fixture helpers ─────────────────────────────────────────
def _tok(symbol, current_bps, *, p80_low, p80_high, point=None,
         cone_normal=5, cone_alert=20, yield_bearing=False,
         consensus_kind="agreed", sources=None,
         max_disagreement_bps=0.0):
    return {
        "symbol": symbol,
        "current_bps": current_bps,
        "meta": {
            "yield_bearing": yield_bearing,
            "cone_normal_bps": cone_normal,
            "cone_alert_bps": cone_alert,
            "venue_type": "CEX",
        },
        "consensus": {
            "kind": consensus_kind,
            "max_disagreement_bps": max_disagreement_bps,
            "sources": sources or [],
        },
        "latest_prediction": {
            "made_at": f"p-{symbol}",
            "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": point if point is not None else (p80_low + p80_high) / 2,
            "p50_low": p80_low + 1.5, "p50_high": p80_high - 1.5,
            "p80_low": p80_low, "p80_high": p80_high,
            "p95_low": p80_low - 2.5, "p95_high": p80_high + 2.5,
            "horizon_minutes": 5, "confidence_word": "likely",
        },
    }


# ── mean_reversion ─────────────────────────────────────────────────
def test_mean_reversion_fires_on_clean_edge():
    """Standard mean-rev trigger: trading below peg, model's nearer
    p80 edge is meaningfully above current."""
    cands = strategies.mean_reversion([
        _tok("USDC", current_bps=-10.0, p80_low=-11.5, p80_high=-8.0),
    ])
    assert len(cands) == 1
    c = cands[0]
    assert c.strategy == "mean_reversion"
    assert c.symbol == "USDC"
    assert c.direction == "long"
    assert abs(c.edge_bps - 2.0) < 1e-6  # p80_high(-8) - current(-10) = +2


def test_mean_reversion_skips_yield_bearing():
    cands = strategies.mean_reversion([
        _tok("USDY", current_bps=1300, p80_low=1280, p80_high=1320,
             yield_bearing=True),
    ])
    assert cands == []


def test_mean_reversion_skips_disputed_consensus():
    cands = strategies.mean_reversion([
        _tok("USDC", current_bps=-10, p80_low=-11.5, p80_high=-8.0,
             consensus_kind="disputed"),
    ])
    assert cands == []


def test_mean_reversion_skips_when_cone_past_alert():
    cands = strategies.mean_reversion([
        _tok("USDC", current_bps=-10, p80_low=-30, p80_high=10,
             cone_normal=5, cone_alert=15),  # ±20 half-width > 15 alert
    ])
    assert cands == []


# ── pairs_divergence ───────────────────────────────────────────────
def test_pairs_divergence_emits_both_legs_when_spread_exceeds_threshold():
    """USDC at -8bp, USDT at +2bp → spread = -10bp (USDC cheaper).
    Should emit: long USDC + short USDT, both same priority."""
    cands = strategies.pairs_divergence([
        _tok("USDC", current_bps=-8.0, p80_low=-10, p80_high=-6),
        _tok("USDT", current_bps=+2.0, p80_low=0, p80_high=4),
    ])
    syms = {(c.symbol, c.direction) for c in cands}
    assert ("USDC", "long") in syms
    assert ("USDT", "short") in syms
    # Edge = spread/2 = 5
    for c in cands:
        if c.symbol in ("USDC", "USDT") and c.strategy == "pairs_divergence":
            assert abs(c.edge_bps - 5.0) < 1e-6


def test_pairs_divergence_skips_when_spread_too_small():
    """Spread under threshold (6bp) → no candidates."""
    cands = strategies.pairs_divergence([
        _tok("USDC", current_bps=-2.0, p80_low=-4, p80_high=0),
        _tok("USDT", current_bps=+1.0, p80_low=-1, p80_high=3),
    ])
    assert cands == []


def test_pairs_divergence_skips_if_either_leg_yield_bearing():
    """Yield-bearing leg disqualifies the whole pair."""
    cands = strategies.pairs_divergence([
        _tok("USDY", current_bps=1000, p80_low=990, p80_high=1010,
             yield_bearing=True),
        _tok("USDT", current_bps=+5, p80_low=2, p80_high=8),
    ])
    assert cands == []


# ── cross_venue_arb ────────────────────────────────────────────────
def test_cross_venue_arb_fires_on_meaningful_source_spread():
    """Two sources reporting, 2bp apart, but consensus still agreed —
    we bet on the outlier coming back to consensus."""
    sources = [
        {"name": "coinbase", "price": 0.99900, "fetched_at": 1.0},
        {"name": "kraken", "price": 0.99920, "fetched_at": 1.1},
    ]
    cands = strategies.cross_venue_arb([
        _tok("USDC", current_bps=-9.0,
             p80_low=-11, p80_high=-7,
             consensus_kind="agreed",
             max_disagreement_bps=2.0,
             sources=sources),
    ])
    assert len(cands) == 1
    c = cands[0]
    assert c.strategy == "cross_venue_arb"
    assert c.direction == "long"
    # Edge ≈ min(spread/2, |current|*0.6) = min(1, 5.4) = 1bp
    assert abs(c.edge_bps - 1.0) < 1e-6


def test_cross_venue_arb_skips_when_only_one_source():
    cands = strategies.cross_venue_arb([
        _tok("FRAX", current_bps=-80, p80_low=-82, p80_high=-78,
             consensus_kind="single",
             max_disagreement_bps=0.0,
             sources=[{"name": "coingecko", "price": 0.9920}]),
    ])
    assert cands == []


def test_cross_venue_arb_skips_when_sources_agree_tightly():
    """Sources <1.5bp apart = nothing to arb."""
    sources = [
        {"name": "coinbase", "price": 0.99950, "fetched_at": 1.0},
        {"name": "kraken", "price": 0.99955, "fetched_at": 1.1},
    ]
    cands = strategies.cross_venue_arb([
        _tok("USDC", current_bps=-5.0, p80_low=-7, p80_high=-3,
             consensus_kind="agreed", max_disagreement_bps=0.5,
             sources=sources),
    ])
    assert cands == []


# ── volatility_regime ──────────────────────────────────────────────
def test_volatility_regime_fires_on_widened_cone():
    """USDT normal cone ±5bp; current cone is ±9bp = 1.8× normal →
    fire contrarian candidate."""
    cands = strategies.volatility_regime([
        _tok("USDT", current_bps=-10.0,
             p80_low=-19, p80_high=-1,  # half-width 9bp, ratio 1.8x
             cone_normal=5, cone_alert=20),
    ])
    assert len(cands) == 1
    c = cands[0]
    assert c.strategy == "volatility_regime"
    assert c.direction == "long"  # current below peg → contrarian long


def test_volatility_regime_skips_normal_cone():
    """Cone at exactly normal width → no candidate."""
    cands = strategies.volatility_regime([
        _tok("USDC", current_bps=-10, p80_low=-13, p80_high=-7,
             cone_normal=5, cone_alert=20),  # half-width 3, ratio 0.6×
    ])
    assert cands == []


def test_volatility_regime_skips_extreme_widening():
    """Cone past 2.5× normal = regime change in progress, skip."""
    cands = strategies.volatility_regime([
        _tok("USDC", current_bps=-10, p80_low=-30, p80_high=10,
             cone_normal=5, cone_alert=25),  # half-width 20, ratio 4×
    ])
    assert cands == []


# ── orchestrator ───────────────────────────────────────────────────
def test_all_candidates_deduplicates_by_symbol_direction():
    """When multiple strategies fire on the same (symbol, direction),
    the merged candidate gets the highest-priority rationale and a
    concatenated strategy name (signal aggregation)."""
    # Construct a feed where USDC trips BOTH mean_reversion AND
    # cross_venue_arb (long direction).
    sources = [
        {"name": "coinbase", "price": 0.99905, "fetched_at": 1.0},
        {"name": "kraken", "price": 0.99928, "fetched_at": 1.1},
    ]
    feed = [
        _tok("USDC", current_bps=-9.0,
             p80_low=-11, p80_high=-6.5,
             consensus_kind="agreed",
             max_disagreement_bps=2.3, sources=sources),
    ]
    merged = strategies.all_candidates(feed)
    usdc_long = [c for c in merged if c.symbol == "USDC"
                 and c.direction == "long"]
    assert len(usdc_long) == 1, (
        "must dedupe — two strategies on same (symbol, direction) "
        "merge into one candidate")
    c = usdc_long[0]
    # Strategy name should reflect that multiple strategies agreed
    assert "+" in c.strategy or "mean_reversion" in c.strategy


def test_all_candidates_sorts_by_priority_descending():
    """The orchestrator returns highest priority first so the trader
    deploys the strongest signals against the budget first."""
    feed = [
        # Small edge — low priority
        _tok("USDC", current_bps=-4, p80_low=-5, p80_high=-2.5,
             cone_normal=5),
        # Large edge — high priority
        _tok("FRAX", current_bps=-50, p80_low=-52, p80_high=-44,
             cone_normal=6),
    ]
    merged = strategies.all_candidates(feed)
    assert len(merged) >= 2
    priorities = [c.priority_score for c in merged]
    assert priorities == sorted(priorities, reverse=True), (
        "candidates must come back priority-descending")
