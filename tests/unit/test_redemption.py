"""Redemption surface — liquidity classification + guardrails."""
from sca.models import (
    Attestation,
    RedemptionAssessment,
    ReserveLine,
    SupplyResult,
)
from sca.tools.redemption import (
    classify_reserves,
    liquid_reserves,
    liquidity_tier,
)
from sca.validation import validate_redemption


def test_liquidity_tier_classification():
    assert liquidity_tier("Cash and cash equivalents") == "liquid"
    assert liquidity_tier("U.S. Treasury Bills") == "liquid"
    assert liquidity_tier("Overnight Repurchase Agreements") == "liquid"
    assert liquidity_tier("Commercial Paper") == "illiquid"
    assert liquidity_tier("Secured Loans") == "illiquid"
    assert liquidity_tier("Something Unusual") == "moderate"


def test_classify_reserves_and_liquid_total():
    att = Attestation(
        symbol="X",
        as_of_date="2026-03-31",
        total_reserves=100,
        tokens_outstanding=100,
        breakdown=[
            ReserveLine("Cash", 60.0),
            ReserveLine("Commercial Paper", 40.0),
        ],
    )
    tiers = classify_reserves(att)
    assert [(t.asset_class, t.tier) for t in tiers] == [
        ("Cash", "liquid"),
        ("Commercial Paper", "illiquid"),
    ]
    assert liquid_reserves(tiers) == 60.0


def test_validate_redemption_plausible_coverage_passes():
    assessment = RedemptionAssessment(
        symbol="X",
        supply=SupplyResult(symbol="X", total_supply=100.0),
        liquid_coverage=0.95,
    )
    chk = next(
        c for c in validate_redemption(assessment)
        if c.name == "liquid_coverage_plausible"
    )
    assert chk.passed


def test_validate_redemption_implausible_coverage_is_critical():
    assessment = RedemptionAssessment(
        symbol="X",
        supply=SupplyResult(symbol="X", total_supply=100.0),
        liquid_coverage=9.0,
    )
    chk = next(
        c for c in validate_redemption(assessment)
        if c.name == "liquid_coverage_plausible"
    )
    assert not chk.passed and chk.severity == "critical"
