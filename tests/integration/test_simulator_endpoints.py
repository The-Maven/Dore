"""Integration: F9 simulator API endpoints (offline, FileStore-backed).

Exercises the surfaces the v3 UI binds to:
  /api/simulator/feed         — the single dense payload
  /api/simulator/state        — ticker + config + brave quota
  /api/simulator/predictions  — archive with resolution join
  /api/simulator/calibration  — track-record aggregation
  /api/simulator/timeline     — chart-ready peg series + candles

All run hermetically against the in-memory FileStore. The
fixtures inject realistic peg ticks + predictions + resolutions
so the endpoint shape mirrors a live cycle.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from web.server import app
    return TestClient(app)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _past_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _seed_usdc_history(store):
    """Plant a realistic USDC history: 10 peg ticks + a fresh
    prediction + one resolution. The endpoints should join these
    into a coherent feed payload."""
    for i in range(10):
        store.insert_peg_tick(
            symbol="USDC", source="coinbase+kraken",
            price=1.0 + (i - 5) * 0.0001,
            deviation_bps=(i - 5) * 1.0,
            consensus_kind="agreed",
            sources=[{"name": "coinbase", "price": 1.0001, "fetched_at": 0},
                     {"name": "kraken",   "price": 1.0002, "fetched_at": 0}],
            max_disagreement_bps=1.0,
        )
        # Override read_at to spread out across the last 10 minutes.
        store._peg_ticks[-1]["read_at"] = (  # noqa: SLF001
            datetime.now(timezone.utc)
            - timedelta(minutes=(10 - i))).isoformat(timespec="seconds")
    pid = store.insert_prediction({
        "symbol": "USDC",
        "kind": "peg_deviation",
        "horizon_minutes": 2,
        "made_at": _past_iso(120),
        "resolves_at": _past_iso(5),
        "point": 4.0,
        "p50_low": 3.0, "p50_high": 5.0,
        "p80_low": 2.0, "p80_high": 6.0,
        "p95_low": 0.5, "p95_high": 7.5,
        "prob_positive": None,
        "confidence_word": "likely",
        "drivers": [], "model": "ewma_cone_v1",
        "notes": "history=10 sigma=0.7bp",
        "judge_synthesis": "USDC at +4.0bp inside the 50% band.",
        "judge_insight": "Stable within stated cone.",
        "judge_pitch": "no action",
        "judge_model": "judge_v2",
    })
    return pid


def test_feed_returns_tokens_with_brand_and_deltas(client, monkeypatch):
    """Feed payload shape: tokens with brand colors, current_bps,
    deltas (d1m/d5m/d1h), sparkline points, latest prediction +
    judge fields, consensus state. Each is the contract the v3 UI
    binds to."""
    from sca.movement import config as sim_cfg
    monkeypatch.setattr(sim_cfg, "load",
                        lambda: {"symbols": ["USDC"], "horizon_minutes": 2,
                                  "tick_interval_minutes": 1, "enabled": True,
                                  "kinds": ["peg_deviation"],
                                  "max_in_flight_per_symbol": 24})
    from sca.store import get_store
    _seed_usdc_history(get_store())

    resp = client.get("/api/simulator/feed")
    assert resp.status_code == 200
    data = resp.json()

    # Top-level keys
    assert "tokens" in data
    assert "events" in data
    assert "sources" in data
    assert "ticker" in data
    assert "brave_quota" in data
    assert "computed_at" in data

    # Token shape
    assert len(data["tokens"]) == 1
    usdc = data["tokens"][0]
    assert usdc["symbol"] == "USDC"
    # Brand surfaces with dark-theme-tuned accent
    assert usdc["brand"]["accent"]  # non-empty
    assert usdc["brand"]["name"] == "Circle"
    assert usdc["brand"]["branded"] is True
    # Current value populated from the freshest tick
    assert usdc["current_bps"] is not None
    # Sparkline carries 10 points
    assert len(usdc["sparkline"]) == 10
    # Each point has the expected shape
    p = usdc["sparkline"][0]
    assert "t" in p and "v" in p and "ck" in p
    # Consensus state
    assert usdc["consensus"]["kind"] == "agreed"
    # Latest prediction with judge text
    pred = usdc["latest_prediction"]
    assert pred is not None
    assert pred["point"] == 4.0
    assert pred["judge_synthesis"]
    # Calibration mini-rollup per-token
    assert "calibration" in usdc


def test_feed_degrades_when_no_data(client, monkeypatch):
    """No peg ticks + no predictions = honest empty payload, not a
    crash. The v3 UI renders an empty-hero state in this case."""
    from sca.movement import config as sim_cfg
    monkeypatch.setattr(sim_cfg, "load",
                        lambda: {"symbols": ["USDC"], "horizon_minutes": 2,
                                  "tick_interval_minutes": 1, "enabled": True,
                                  "kinds": ["peg_deviation"],
                                  "max_in_flight_per_symbol": 24})
    resp = client.get("/api/simulator/feed")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tokens"]) == 1
    usdc = data["tokens"][0]
    assert usdc["current_bps"] is None
    assert usdc["sparkline"] == []
    assert usdc["latest_prediction"] is None
    assert usdc["consensus"]["kind"] == "single"  # default fallback
    # Brand still resolves so the UI can render a chip placeholder
    assert usdc["brand"]["accent"]


def test_state_endpoint_returns_ticker_config_and_quota(client):
    resp = client.get("/api/simulator/state")
    assert resp.status_code == 200
    data = resp.json()
    assert "ticker" in data
    assert "config" in data
    assert "brave_quota" in data
    # Brave quota always has these three even cold
    q = data["brave_quota"]
    assert "calls" in q and "cap" in q and "remaining" in q
    assert "peg_consensus" in data  # consensus rollup


def test_predictions_endpoint_joins_resolution(client, monkeypatch):
    """A graded prediction surfaces with its resolution joined so
    the UI can render hit/miss colour cues on history strips."""
    from sca.store import get_store
    store = get_store()
    pid = store.insert_prediction({
        "symbol": "USDC", "kind": "peg_deviation",
        "horizon_minutes": 60,
        "made_at": _past_iso(7200),
        "resolves_at": _past_iso(3600),
        "point": 2.0, "p50_low": 0, "p50_high": 4,
        "p80_low": -2, "p80_high": 6, "p95_low": -5, "p95_high": 9,
        "prob_positive": None, "confidence_word": "likely",
        "drivers": [], "model": "test_model", "notes": "",
    })
    store.insert_resolution({
        "prediction_id": pid,
        "actual_value": 1.8,
        "brier_score": None,
        "crps_score": 0.4,
        "outcome_kind": "inside_p50",
        "narrative": "USDC at +1.8bp inside the 50% band.",
        "baseline_persistence_brier": None,
        "baseline_climatology_brier": 0.25,
    })

    resp = client.get("/api/simulator/predictions?symbol=USDC&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    p = data["predictions"][0]
    assert p["symbol"] == "USDC"
    assert "resolution" in p
    assert p["resolution"]["outcome_kind"] == "inside_p50"
    assert p["resolution"]["crps_score"] == 0.4


def test_calibration_endpoint_surfaces_brier_and_baselines(client, monkeypatch):
    """A few graded direction predictions should produce a
    calibration_summary with brier + baselines populated."""
    from sca.store import get_store
    store = get_store()
    # Plant a 10-prediction history at prob_positive=0.7 with 7/10
    # actually positive (well calibrated).
    for i in range(10):
        pid = store.insert_prediction({
            "symbol": "USDC", "kind": "net_flow_direction",
            "horizon_minutes": 60,
            "made_at": _past_iso(3600 + i * 60),
            "resolves_at": _past_iso(i * 60),
            "point": 100.0,
            "p50_low": None, "p50_high": None,
            "p80_low": None, "p80_high": None,
            "p95_low": None, "p95_high": None,
            "prob_positive": 0.7,
            "confidence_word": "likely",
            "drivers": [], "model": "test_model", "notes": "",
        })
        actual = 1.0 if i < 7 else -1.0
        store.insert_resolution({
            "prediction_id": pid,
            "actual_value": actual,
            "brier_score": (0.7 - (1 if actual > 0 else 0)) ** 2,
            "crps_score": None,
            "outcome_kind": "inside_p50" if actual > 0 else "outside",
            "narrative": "",
            "baseline_persistence_brier": None,
            "baseline_climatology_brier": 0.25,
        })

    resp = client.get("/api/simulator/calibration?kind=net_flow_direction")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 10
    assert data["brier_mean"] is not None
    # 7/10 at 0.7 prob → average brier = 0.7*0.09 + 0.3*0.49 = 0.21
    assert abs(data["brier_mean"] - 0.21) < 0.01
    # Reliability bin at 0.7 should reflect empirical 0.7 hit-rate
    bins = data["reliability_bins"]
    bucket_70 = next((b for b in bins if b["lower_pct"] <= 70 < b["upper_pct"]), None)
    assert bucket_70 is not None
    assert abs(bucket_70["empirical_rate"] - 0.7) < 0.01


def test_timeline_endpoint_returns_candles_and_forecast(client, monkeypatch):
    """The timeline endpoint feeds the v3 hero chart. Candles get
    binned from raw ticks; the latest prediction's bands ride
    along for the forecast cone."""
    from sca.movement import config as sim_cfg
    monkeypatch.setattr(sim_cfg, "load",
                        lambda: {"symbols": ["USDC"], "horizon_minutes": 2,
                                  "tick_interval_minutes": 1, "enabled": True,
                                  "kinds": ["peg_deviation"],
                                  "max_in_flight_per_symbol": 24})
    from sca.store import get_store
    _seed_usdc_history(get_store())

    resp = client.get("/api/simulator/timeline?bin_minutes=5")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tokens"]) == 1
    tok = data["tokens"][0]
    assert tok["symbol"] == "USDC"
    # Raw ticks preserved for line rendering
    assert len(tok["ticks"]) == 10
    # Candles binned (5-min window with 10 ticks across ~10min)
    assert len(tok["candles"]) >= 1
    candle = tok["candles"][0]
    assert "open" in candle and "high" in candle \
        and "low" in candle and "close" in candle
    # Latest prediction passed through with bands
    pred = tok["latest_prediction"]
    assert pred is not None
    assert pred["point"] == 4.0


def test_commentary_endpoint_returns_card(client, tmp_path, monkeypatch):
    """Commentary endpoint returns a per-token AI Commentary card
    with structural_one_liner + cone thresholds. With no LLM
    configured, the deterministic fallback path produces a card
    grounded in the cheat sheet alone."""
    from sca.movement import commentary as _comm
    monkeypatch.setattr(_comm, "_CACHE_PATH", tmp_path / "ccc.json")
    from sca.store import get_store
    _seed_usdc_history(get_store())

    resp = client.get("/api/simulator/commentary/USDC")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "USDC"
    assert "Circle" in data["body"] or "money-market" in data["body"]
    assert data["citations"]
    assert data["cone_normal_bps"] > 0
    assert data["cone_alert_bps"] > data["cone_normal_bps"]


def test_commentary_endpoint_unknown_symbol_returns_404(client):
    """Honest n/a — an unregistered token must return 404 not a
    fake card."""
    resp = client.get("/api/simulator/commentary/FAKECOIN")
    assert resp.status_code == 404


# NOTE: a TestClient.stream() test for /api/simulator/stream was
# attempted but TestClient doesn't reliably terminate the async
# event_generator() (it sleeps 2s in a while-True loop). The SSE
# endpoint is exercised in production via EventSource; the snapshot
# branch is the same function that powers /api/simulator/feed which
# IS unit-tested above. If a regression breaks the snapshot path,
# the feed tests catch it.
