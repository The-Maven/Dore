"""Deterministic guardrails over the system's critical data."""
from sca.models import (
    Attestation,
    CorpusPassage,
    Metrics,
    ReserveLine,
    SupplyResult,
)
from sca.validation import (
    validate_attestation,
    validate_metrics,
    validate_supply,
    verify_citations,
)


def _supply(total: float = 100.0) -> SupplyResult:
    return SupplyResult(symbol="X", total_supply=total, native_supply=total)


def _att(**overrides) -> Attestation:
    base = dict(
        symbol="X",
        as_of_date="2026-03-31",
        total_reserves=100.0,
        tokens_outstanding=98.0,
        confidence=0.9,
        breakdown=[],
    )
    base.update(overrides)
    return Attestation(**base)


def test_clean_attestation_passes_all_checks():
    assert all(c.passed for c in validate_attestation(_att()))


def test_negative_reserves_is_critical():
    bad = next(
        c for c in validate_attestation(_att(total_reserves=-5.0))
        if c.name == "reserves_positive"
    )
    assert not bad.passed and bad.severity == "critical"


def test_breakdown_must_sum_to_total():
    att = _att(
        total_reserves=100.0,
        breakdown=[ReserveLine("T-bills", 50.0), ReserveLine("cash", 10.0)],
    )
    chk = next(
        c for c in validate_attestation(att)
        if c.name == "breakdown_sums_to_total"
    )
    assert not chk.passed  # 60 vs 100


def test_breakdown_within_tolerance_passes():
    att = _att(
        total_reserves=100.0,
        breakdown=[ReserveLine("T-bills", 99.0), ReserveLine("cash", 0.5)],
    )
    chk = next(
        c for c in validate_attestation(att)
        if c.name == "breakdown_sums_to_total"
    )
    assert chk.passed  # 99.5 vs 100 — within 2%


def test_future_date_is_critical():
    bad = next(
        c for c in validate_attestation(_att(as_of_date="2099-01-01"))
        if c.name == "attestation_date_valid"
    )
    assert not bad.passed and bad.severity == "critical"


def test_low_confidence_warns():
    chk = next(
        c for c in validate_attestation(_att(confidence=0.3))
        if c.name == "extraction_confidence"
    )
    assert not chk.passed and chk.severity == "warn"


def test_implausible_coverage_is_critical():
    m = Metrics(attested_coverage=12.0, live_coverage=12.0,
                staleness_days=5, supply_drift=0.0)
    chk = validate_metrics(m)[0]
    assert not chk.passed and chk.severity == "critical"


def test_plausible_coverage_passes():
    m = Metrics(attested_coverage=1.02, live_coverage=1.0,
                staleness_days=5, supply_drift=0.0)
    assert validate_metrics(m)[0].passed


def test_zero_supply_is_critical():
    chk = validate_supply(SupplyResult(symbol="X", total_supply=0.0))[0]
    assert not chk.passed and chk.severity == "critical"


# ── citation verification ─────────────────────────────────────────────
def test_unknown_tool_citation_warns():
    checks = verify_citations(
        "Coverage looks fine [tool:made_up].",
        supply=_supply(), attestation=None, passages=[],
    )
    chk = next(c for c in checks if c.name == "citations_tool_valid")
    assert not chk.passed and chk.severity == "warn"


def test_valid_tool_citation_passes():
    checks = verify_citations(
        "Supply is X per [tool:onchain_supply].",
        supply=_supply(), attestation=None, passages=[],
    )
    assert next(
        c for c in checks if c.name == "citations_tool_valid"
    ).passed


def test_citing_non_approved_source_is_critical():
    checks = verify_citations(
        "Per [mica-title-iii §36] reserves must be 1:1.",
        supply=_supply(), attestation=None, passages=[],
    )
    chk = next(c for c in checks if c.name == "citations_source_valid")
    assert not chk.passed and chk.severity == "critical"


def test_citing_approved_source_passes():
    passage = CorpusPassage(
        source_id="mica-title-iii", section="36", heading="h",
        text="t", citation="mica-title-iii §36",
    )
    checks = verify_citations(
        "Per [mica-title-iii §36] reserves must be 1:1.",
        supply=_supply(), attestation=None, passages=[passage],
    )
    assert next(
        c for c in checks if c.name == "citations_source_valid"
    ).passed


def test_untraceable_figure_is_critical():
    checks = verify_citations(
        "Reserves stood at $99,999,999,999 last month.",
        supply=_supply(total=100.0), attestation=None, passages=[],
    )
    chk = next(c for c in checks if c.name == "figures_traceable")
    assert not chk.passed and chk.severity == "critical"


def test_traceable_figure_passes():
    checks = verify_citations(
        "On-chain supply is $77,125,330,954 today.",
        supply=_supply(total=77_125_330_954.0), attestation=None, passages=[],
    )
    assert next(
        c for c in checks if c.name == "figures_traceable"
    ).passed
