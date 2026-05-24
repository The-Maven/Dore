"""Resolver tests — Brier, CRPS, outcome banding, end-to-end grade."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sca.movement.resolve import (
    actual_was_positive,
    brier_score_binary,
    crps_from_band,
    grade_one,
    outcome_band,
    run_resolver_cycle,
)
from sca.store import get_store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _past_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_brier_perfect_call():
    """A perfectly confident-and-correct call scores 0."""
    assert brier_score_binary(1.0, True) == 0.0
    assert brier_score_binary(0.0, False) == 0.0


def test_brier_perfect_miss():
    """A perfectly confident-and-wrong call scores 1.0."""
    assert brier_score_binary(1.0, False) == 1.0
    assert brier_score_binary(0.0, True) == 1.0


def test_brier_climatology_baseline():
    """A 50/50 call on any outcome scores 0.25 — the climatology floor."""
    assert brier_score_binary(0.5, True) == 0.25
    assert brier_score_binary(0.5, False) == 0.25


def test_outcome_band_walks_from_tight_to_wide():
    """Actual inside p50 -> 'inside_p50'; only inside p80 ->
    'inside_p80'; etc. Each band wins over wider ones."""
    assert outcome_band(0.5, -1, 1, -3, 3, -5, 5) == "inside_p50"
    assert outcome_band(2.0, -1, 1, -3, 3, -5, 5) == "inside_p80"
    assert outcome_band(4.0, -1, 1, -3, 3, -5, 5) == "inside_p95"
    assert outcome_band(10.0, -1, 1, -3, 3, -5, 5) == "outside"


def test_crps_grows_with_error():
    """A wider error must produce a strictly larger CRPS."""
    small = crps_from_band(actual=1.0, point=0.0,
                            p50_half=1.0, p80_half=2.0, p95_half=3.0)
    big = crps_from_band(actual=10.0, point=0.0,
                          p50_half=1.0, p80_half=2.0, p95_half=3.0)
    assert big > small


def test_grade_one_for_peg_deviation_end_to_end():
    """Plant a peg tick + a prediction whose resolves_at lines up with
    that tick; assert the resolver computes the actual + outcome
    correctly."""
    store = get_store()
    # The prediction resolves 10 minutes ago — past, so eligible to
    # grade. The peg tick at that same moment is what reality will
    # be matched against.
    resolves_at = _past_iso(10)
    store.insert_peg_tick(
        symbol="USDC", source="test",
        price=1.001, deviation_bps=10.0,
    )
    # Override the read_at on the in-memory FileStore so it sits at
    # the resolve target. The FileStore stamps read_at to _now() by
    # default; we patch the in-memory row to make this deterministic.
    last = store._peg_ticks[-1]  # noqa: SLF001 - test-only access
    last["read_at"] = resolves_at

    pid = store.insert_prediction({
        "symbol": "USDC",
        "kind": "peg_deviation",
        "horizon_minutes": 60,
        "made_at": _past_iso(70),
        "resolves_at": resolves_at,
        "point": 8.0,
        "p50_low": 5.0, "p50_high": 11.0,
        "p80_low": 3.0, "p80_high": 13.0,
        "p95_low": 1.0, "p95_high": 15.0,
        "prob_positive": None,
        "confidence_word": "likely",
        "drivers": [], "model": "test_model", "notes": "",
    })
    assert pid

    # Pull the prediction back so grade_one sees the canonical shape
    pred = store.list_predictions(symbol="USDC")[0]
    res = grade_one(pred)
    assert res is not None
    assert abs(res["actual_value"] - 10.0) < 1e-6
    # 10 lies inside the 50% band [5, 11]
    assert res["outcome_kind"] == "inside_p50"
    assert res["crps_score"] is not None
    assert res["brier_score"] is None  # peg_deviation uses CRPS


def test_run_resolver_cycle_grades_and_records():
    store = get_store()
    resolves_at = _past_iso(5)
    store.insert_peg_tick(
        symbol="USDC", source="test",
        price=0.9998, deviation_bps=-2.0,
    )
    store._peg_ticks[-1]["read_at"] = resolves_at  # noqa: SLF001
    store.insert_prediction({
        "symbol": "USDC", "kind": "peg_deviation",
        "horizon_minutes": 60,
        "made_at": _past_iso(65),
        "resolves_at": resolves_at,
        "point": -3.0,
        "p50_low": -6, "p50_high": 0,
        "p80_low": -10, "p80_high": 4,
        "p95_low": -15, "p95_high": 9,
        "prob_positive": None,
        "confidence_word": "likely",
        "drivers": [], "model": "test_model", "notes": "",
    })
    summary = run_resolver_cycle()
    assert summary["checked"] == 1
    assert summary["graded"] == 1
    # Re-running should now find nothing to grade (resolutions are
    # 1:1 with predictions)
    summary2 = run_resolver_cycle()
    assert summary2["graded"] == 0


def test_calibration_summary_has_baselines():
    """End-to-end: a graded prediction shows in calibration_summary
    with the baseline columns populated."""
    store = get_store()
    resolves_at = _past_iso(5)
    store.insert_peg_tick(
        symbol="USDC", source="test",
        price=1.0001, deviation_bps=1.0,
    )
    store._peg_ticks[-1]["read_at"] = resolves_at  # noqa: SLF001
    # Direction prediction so brier_score gets computed
    store.insert_prediction({
        "symbol": "USDC", "kind": "net_flow_direction",
        "horizon_minutes": 60,
        "made_at": _past_iso(65),
        "resolves_at": resolves_at,
        "point": 5.0,
        "p50_low": None, "p50_high": None,
        "p80_low": None, "p80_high": None,
        "p95_low": None, "p95_high": None,
        "prob_positive": 0.7,
        "confidence_word": "likely",
        "drivers": [], "model": "test_model", "notes": "",
    })
    # Plant snapshots so realized_net_flow_at returns a value
    store.save_snapshot(
        symbol="USDC", total_supply=1_000_000_000.0,
        native_supply=None, bridged_supply=None,
        per_chain=None, warnings=[])
    store._snapshots[-1]["read_at"] = _past_iso(65)  # noqa: SLF001
    store.save_snapshot(
        symbol="USDC", total_supply=1_000_001_000.0,
        native_supply=None, bridged_supply=None,
        per_chain=None, warnings=[])
    store._snapshots[-1]["read_at"] = resolves_at  # noqa: SLF001

    run_resolver_cycle()
    summary = store.calibration_summary(kind="net_flow_direction")
    assert summary["count"] == 1
    assert summary["brier_mean"] is not None
    assert summary["baseline_climatology_brier_mean"] is not None
