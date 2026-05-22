"""Integration: the eval harness end-to-end, offline (mocked RPC)."""
from sca.evals import grade_case, run_evals


def test_harness_runs_all_cases_and_passes(fake_rpc):
    results = run_evals()
    assert len(results) == 4
    failing = [
        (r.case_id, [(p.point, p.passed, p.detail) for p in r.points])
        for r in results
        if not r.passed
    ]
    assert not failing, failing


def test_grade_case_supply_resolved(fake_rpc):
    case = {
        "id": "t",
        "symbol": "USDC",
        "expects": [{"kind": "supply_resolved"}],
    }
    assert grade_case(case).passed


def test_grade_case_detects_failure(fake_rpc):
    # No attestation seeded -> metrics absent -> metrics_present must fail.
    case = {
        "id": "t",
        "symbol": "USDC",
        "expects": [{"kind": "metrics_present"}],
    }
    assert not grade_case(case).passed
