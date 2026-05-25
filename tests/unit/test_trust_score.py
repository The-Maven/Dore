"""Trust Score — institutional differentiator.

Pins: dimensions are scored, composite is weighted correctly, verdict
language matches the score band, missing data scores conservatively
and marks 'unverified' (never silently scores high)."""
from __future__ import annotations

import pytest

from sca import trust_score as ts


def test_dimensions_sum_to_100_weight():
    assert ts.TOTAL_WEIGHT == 100.0


def test_compute_returns_well_formed_score():
    score = ts.compute_trust_score("USDC")
    assert score.symbol == "USDC"
    assert 0 <= score.score <= 100
    assert score.verdict  # non-empty
    assert len(score.dimensions) == len(ts.DIMENSIONS)
    # Composite is weighted sum
    expected = sum(d.score * d.weight for d in score.dimensions) / 100
    assert abs(score.score - round(expected, 1)) < 0.5


def test_verdict_thresholds_match_score_band():
    # Cherry-pick scores and verify the verdict mapping
    cases = [
        (90, "Eligible for Tier-1 corporate treasury IPS"),
        (75, "Eligible with concentration limits"),
        (60, "Speculative — limit to non-strategic reserve"),
        (45, "High-risk — short-duration tactical only"),
        (20, "Not eligible under standard treasury policy"),
    ]
    for score, expected in cases:
        assert ts._verdict_for(score) == expected


def test_unknown_symbol_falls_back_conservatively():
    """The composite should be defensible even when nothing is on
    record for the symbol."""
    score = ts.compute_trust_score("TOTALLY_UNKNOWN_TOKEN")
    assert 0 <= score.score <= 100
    # Should NOT silently score high — most dimensions tag 'unverified'
    unverified = [d for d in score.dimensions if d.tier == "unverified"]
    assert len(unverified) >= 3, (
        "Unknown token should score multiple dimensions as 'unverified'")


def test_big_four_auditor_scores_high():
    score, tier, _ = ts._auditor_score("Deloitte")
    assert score >= 90 and tier == "verified"
    score, tier, _ = ts._auditor_score("Ernst & Young")
    assert score >= 90 and tier == "verified"


def test_no_auditor_scores_unverified_low():
    score, tier, _ = ts._auditor_score("")
    assert score < 50 and tier == "unverified"


def test_algorithmic_backing_scores_red_flag():
    """Algorithmic models (UST lineage) must score low and unverified."""
    s, tier, _, _, _ = ts._backing_model_score("algorithmic", "", "")
    assert s < 35 and tier == "unverified"


def test_crypto_collateral_scores_well_without_attestation():
    """Crypto-collateralised tokens don't NEED an attestation URL — the
    reserves are readable on-chain. Their score should reflect that."""
    s, tier, _, src, _ = ts._backing_model_score("crypto_collateral", "", "")
    assert s >= 80 and tier == "verified"
    assert src == "on-chain"


def test_tier_color_bands():
    assert ts.tier_color(90) == "#4AF6C3"
    assert ts.tier_color(75) == "#A3E635"
    assert ts.tier_color(60) == "#D4A24A"
    assert ts.tier_color(45) == "#F59E0B"
    assert ts.tier_color(20) == "#FF433D"


def test_dimensions_carry_citations():
    """Every dimension must record where its evidence came from. This
    is the non-negotiable institutional-buyer requirement."""
    score = ts.compute_trust_score("USDC")
    for d in score.dimensions:
        # Either a source label OR an explicit "computed/estimated" tag
        assert d.evidence_source, f"{d.name} missing evidence_source"
        assert d.reasoning, f"{d.name} missing reasoning"
