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


def test_normalised_miss_distance_is_zero_on_perfect_call():
    """Continuous-forecast band-distance scoring: a perfect point
    estimate scores 0 regardless of band width."""
    from sca.movement.resolve import normalised_miss_distance
    assert normalised_miss_distance(5.0, 5.0, 1.0, 2.0, 3.0) == 0.0


def test_normalised_miss_distance_scales_with_band():
    """An identical absolute error should score lower against a wider
    forecast — wider bands explicitly say 'we expect noise', so a
    given miss is less of a failure."""
    from sca.movement.resolve import normalised_miss_distance
    tight = normalised_miss_distance(0.0, 1.0, 0.3, 0.5, 0.8)
    wide = normalised_miss_distance(0.0, 1.0, 1.5, 2.5, 4.0)
    assert tight > wide  # same error, smaller band = bigger relative miss


def test_persistence_baseline_returns_none_when_no_history():
    """The honest persistence baseline must report None (not 0.25)
    when there's no prior data to anchor on — better a missing column
    than a meaningless one."""
    from sca.movement.resolve import _persistence_brier
    pred = {
        "symbol": "USDC", "kind": "net_flow_direction",
        "horizon_minutes": 60,
        "made_at": "2026-05-24T12:00:00+00:00",
    }
    # No snapshots in store — _persistence_brier must return None
    assert _persistence_brier(pred, True) is None


def test_reliability_bins_catch_miscalibrated_model():
    """Audit #16.1: plant a miscalibrated model (claims 80% but
    delivers 50%) and assert the reliability bin reflects the gap.
    This is the calibration plot's actual contract — previously
    only tested that the field was populated, not that it was right."""
    from sca.store import get_store
    store = get_store()
    # 200 predictions, all at prob_positive=0.8 (claims very-likely
    # to be positive). Half of them actually go positive — so the
    # 70-90 bucket should show empirical_rate ≈ 0.5.
    import uuid
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc) - timedelta(days=2)
    for i in range(200):
        pred_id = str(uuid.uuid4())
        store._predictions.append({  # noqa: SLF001 - test injection
            "id": pred_id,
            "symbol": "USDC",
            "kind": "net_flow_direction",
            "horizon_minutes": 60,
            "made_at": (base + timedelta(minutes=i)).isoformat(timespec="seconds"),
            "resolves_at": (base + timedelta(minutes=i+60)).isoformat(
                timespec="seconds"),
            "point": 0.0,
            "p50_low": None, "p50_high": None,
            "p80_low": None, "p80_high": None,
            "p95_low": None, "p95_high": None,
            "prob_positive": 0.8,
            "confidence_word": "very_likely",
            "drivers": [], "model": "test_model", "notes": "",
        })
        # Half positive, half negative — empirical rate 0.5
        actual = 1.0 if i % 2 == 0 else -1.0
        store._resolutions.append({  # noqa: SLF001 - test injection
            "id": str(uuid.uuid4()),
            "prediction_id": pred_id,
            "resolved_at": (base + timedelta(minutes=i+60)).isoformat(
                timespec="seconds"),
            "actual_value": actual,
            "brier_score": (0.8 - (1.0 if actual > 0 else 0.0)) ** 2,
            "crps_score": None,
            "outcome_kind": "inside_p80" if actual > 0 else "outside",
            "narrative": "",
            "baseline_persistence_brier": None,
            "baseline_climatology_brier": 0.25,
        })

    summary = store.calibration_summary(kind="net_flow_direction")
    bins = summary["reliability_bins"]
    # Find the 70-80 bin (covers prob 0.7-0.8) or 80-90 (covers 0.8-0.9)
    # — our predictions at exactly 0.8 land in 80-90 due to >= 0.8 < 0.9
    target = next(
        (b for b in bins
         if b["lower_pct"] <= 80 and b["upper_pct"] > 80),
        None,
    )
    assert target is not None, "expected a bin covering 0.8"
    # Empirical rate ≈ 0.5 (we planted exactly half positive).
    assert abs(target["empirical_rate"] - 0.5) < 0.02
    # Predicted mean stays at 0.8 (that's what we forecast every time).
    assert abs(target["predicted_mean"] - 0.8) < 0.01
    # Sample count = 200 (or close to it).
    assert target["count"] == 200


def test_concurrent_insert_and_resolve_no_duplicates():
    """Audit #16.3: two threads racing — one inserts predictions,
    the other inserts resolutions for them. The 1:1 invariant
    (resolutions.prediction_id is unique) must hold even under
    contention. SupabaseStore enforces it at the DB; this pins the
    FileStore lock contract that audit #8 added."""
    import threading
    import uuid
    from sca.store import get_store

    store = get_store()
    n_iterations = 200
    prediction_ids: list[str] = []
    insert_done = threading.Event()

    def inserter():
        for _ in range(n_iterations):
            pid = store.insert_prediction({
                "id": str(uuid.uuid4()),
                "symbol": "USDC",
                "kind": "net_flow_direction",
                "horizon_minutes": 60,
                "made_at": _now_iso(),
                "resolves_at": _past_iso(1),
                "point": 0.0, "prob_positive": 0.5,
                "p50_low": None, "p50_high": None,
                "p80_low": None, "p80_high": None,
                "p95_low": None, "p95_high": None,
                "confidence_word": "likely",
                "drivers": [], "model": "concur_test", "notes": "",
            })
            prediction_ids.append(pid)
        insert_done.set()

    def resolver():
        # Walk the inserted ids and try to resolve each TWICE; the
        # second insert_resolution call must return "" (dropped by
        # the unique-constraint check inside the lock).
        seen = set()
        while not insert_done.is_set() or len(seen) < len(prediction_ids):
            for pid in list(prediction_ids):
                if pid in seen:
                    continue
                # First grade — should succeed.
                first = store.insert_resolution({
                    "prediction_id": pid,
                    "actual_value": 1.0,
                    "brier_score": 0.25,
                    "crps_score": None,
                    "outcome_kind": "hit",
                    "narrative": "",
                    "baseline_persistence_brier": None,
                    "baseline_climatology_brier": 0.25,
                })
                # Second grade — same prediction_id — must drop.
                second = store.insert_resolution({
                    "prediction_id": pid,
                    "actual_value": 1.0,
                    "brier_score": 0.25,
                    "crps_score": None,
                    "outcome_kind": "hit",
                    "narrative": "",
                    "baseline_persistence_brier": None,
                    "baseline_climatology_brier": 0.25,
                })
                if first:
                    seen.add(pid)
                assert second == "", \
                    f"duplicate resolution accepted for {pid}"

    t_insert = threading.Thread(target=inserter)
    t_resolve = threading.Thread(target=resolver)
    t_insert.start()
    t_resolve.start()
    t_insert.join(timeout=30)
    t_resolve.join(timeout=30)

    # Final invariant: exactly N predictions, exactly N resolutions,
    # one per prediction_id.
    preds = store.list_predictions(symbol="USDC", limit=n_iterations + 10)
    assert len(preds) == n_iterations
    # Build a count map of resolutions per prediction_id directly
    # from the store internals so we don't rely on list_resolutions'
    # join semantics.
    res_ids = [r["prediction_id"]
                for r in store._resolutions]  # noqa: SLF001 - test invariant
    assert len(res_ids) == n_iterations
    assert len(set(res_ids)) == n_iterations  # no duplicates


def test_resolver_narrates_disputed_peg_ground_truth():
    """Audit #7: when the peg tick we resolve against is 'disputed',
    the resolution narrative must flag it so investors reading the
    archive know the ground truth itself was uncertain."""
    store = get_store()
    resolves_at = _past_iso(5)
    # Plant a disputed peg tick.
    store._peg_ticks.append({  # noqa: SLF001 - test injection
        "id": "tick-1",
        "symbol": "USDC",
        "source": "coinbase+kraken",
        "price": 1.00025,
        "deviation_bps": 2.5,
        "consensus_kind": "disputed",
        "sources": [
            {"name": "coinbase", "price": 1.0001, "fetched_at": 0},
            {"name": "kraken", "price": 1.0004, "fetched_at": 0},
        ],
        "max_disagreement_bps": 3.0,
        "read_at": resolves_at,
    })
    pid = store.insert_prediction({
        "symbol": "USDC", "kind": "peg_deviation",
        "horizon_minutes": 60,
        "made_at": _past_iso(65),
        "resolves_at": resolves_at,
        "point": 1.0,
        "p50_low": 0, "p50_high": 4,
        "p80_low": -3, "p80_high": 7,
        "p95_low": -8, "p95_high": 12,
        "prob_positive": None,
        "confidence_word": "likely",
        "drivers": [], "model": "test_model", "notes": "",
    })
    pred = store.list_predictions(symbol="USDC")[0]
    res = grade_one(pred)
    assert res is not None
    assert "contested across sources" in res["narrative"].lower() \
        or "Ground truth contested" in res["narrative"]


def test_resolver_narrates_single_source_peg_ground_truth():
    """Single-source peg ticks are honestly named in the narrative
    so a reader knows triangulation wasn't possible."""
    store = get_store()
    resolves_at = _past_iso(5)
    store._peg_ticks.append({  # noqa: SLF001 - test injection
        "id": "tick-2",
        "symbol": "USDC",
        "source": "coinbase",
        "price": 1.0001,
        "deviation_bps": 1.0,
        "consensus_kind": "single",
        "sources": [
            {"name": "coinbase", "price": 1.0001, "fetched_at": 0},
        ],
        "max_disagreement_bps": 0.0,
        "read_at": resolves_at,
    })
    pid = store.insert_prediction({
        "symbol": "USDC", "kind": "peg_deviation",
        "horizon_minutes": 60,
        "made_at": _past_iso(65),
        "resolves_at": resolves_at,
        "point": 0.0,
        "p50_low": -2, "p50_high": 2,
        "p80_low": -5, "p80_high": 5,
        "p95_low": -10, "p95_high": 10,
        "prob_positive": None,
        "confidence_word": "likely",
        "drivers": [], "model": "test_model", "notes": "",
    })
    pred = store.list_predictions(symbol="USDC")[0]
    res = grade_one(pred)
    assert res is not None
    assert "single-source" in res["narrative"].lower()
    assert "coinbase" in res["narrative"].lower()


def test_climatology_baseline_falls_back_to_half_when_thin():
    """With fewer than 5 prior resolutions the climatology baseline
    falls back to 50/50 (honest 'we have no rate to read')."""
    from sca.movement.resolve import _climatology_brier
    pred = {"symbol": "USDC", "kind": "net_flow_direction"}
    # Empty archive → fallback
    score = _climatology_brier(pred, True)
    # Brier(0.5, 1) = 0.25
    assert abs(score - 0.25) < 1e-9


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
