from datetime import date

from sca.tools.metrics import compute_metrics


def test_coverage_staleness_drift():
    m = compute_metrics(
        attested_reserves=55_000_000,
        attested_tokens=50_000_000,
        current_supply=50_000_000,
        attestation_date="2026-05-01",
        as_of=date(2026, 5, 22),
    )
    assert m.attested_coverage == 1.1
    assert m.live_coverage == 1.1
    assert m.staleness_days == 21
    assert m.supply_drift == 0.0


def test_attested_and_live_coverage_diverge_under_drift():
    # Reserves fixed; live supply above attested tokens -> live coverage lower.
    m = compute_metrics(
        attested_reserves=100,
        attested_tokens=100,
        current_supply=125,
        attestation_date="2026-05-01",
        as_of=date(2026, 5, 2),
    )
    assert m.attested_coverage == 1.0
    assert m.live_coverage == 0.8
    assert round(m.supply_drift, 4) == 0.25


def test_zero_supply_and_tokens_yield_none():
    m = compute_metrics(
        attested_reserves=100,
        attested_tokens=0,
        current_supply=0,
        attestation_date="2026-05-01",
        as_of=date(2026, 5, 2),
    )
    assert m.attested_coverage is None
    assert m.live_coverage is None
    assert m.supply_drift is None


def test_accepts_date_object():
    m = compute_metrics(
        attested_reserves=1,
        attested_tokens=1,
        current_supply=1,
        attestation_date=date(2026, 5, 1),
        as_of=date(2026, 5, 11),
    )
    assert m.staleness_days == 10
