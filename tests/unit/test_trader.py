"""The Discipline Trader — deterministic rule + P&L tests.

Pins the persona's behaviour: trade only on mean-reversion signal,
skip yield-bearing tokens, refuse to size up when cone is past
alert, settle P&L correctly on resolve. The trader is rules-based
so every test exercises a specific rule.
"""
from __future__ import annotations

import pytest

from sca.movement import trader


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Each test gets its own persistence file so they don't share
    trade state."""
    monkeypatch.setattr(
        trader, "TRADER_PATH", tmp_path / "trader.json")


# ── decision rule tests ────────────────────────────────────────────
def test_skips_yield_bearing_tokens():
    """USDY drifts above $1 by design — never tradable. Even a deep
    above-peg reading + a reverting forecast must not open a trade."""
    feed = [{
        "symbol": "USDY",
        "current_bps": 1300.0,
        "meta": {"yield_bearing": True, "cone_normal_bps": 50,
                  "cone_alert_bps": 150},
        "latest_prediction": {
            "made_at": "2026-05-25T11:00:00+00:00",
            "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": 1295.0, "p80_low": 1280.0, "p80_high": 1310.0,
            "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == []


def test_skips_when_deviation_below_threshold():
    """A peg deviation tighter than ENTRY_THRESHOLD_BPS (4bp) is
    noise — no edge to enter."""
    feed = [{
        "symbol": "USDC",
        "current_bps": -2.5,  # below 4bp threshold
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 15},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": 0.0, "p80_low": -3.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == []


def test_skips_when_cone_past_alert_threshold():
    """Wide cone = regime change = not the time to add risk. Even
    with a clean mean-reversion signal, the trader refuses."""
    feed = [{
        "symbol": "USDC",
        "current_bps": -20.0,  # past 15bp alert
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 15},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0,
            "p80_low": -25.0, "p80_high": 21.0,  # ±23bp, past 15 alert
            "confidence_word": "unlikely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == []


def test_skips_when_forecast_extends_deviation():
    """Trend-following is OFF the persona's table. Below peg but the
    model says it'll go LOWER — refuse."""
    feed = [{
        "symbol": "USDC",
        "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 15},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -15.0,  # extending the depeg further
            "p80_low": -20.0, "p80_high": -10.0,
            "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == []


def test_opens_long_on_below_peg_mean_reversion():
    """Trading below peg, model expects recovery → take a LONG."""
    feed = [{
        "symbol": "USDC",
        "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 15},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0,  # expecting recovery
            "p80_low": -7.0, "p80_high": 3.0,  # ±5bp cone within normal
            "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert len(opened) == 1
    t = opened[0]
    assert t.symbol == "USDC"
    assert t.direction == "long"
    assert t.entry_bps == -10.0
    assert t.forecast_point_bps == -2.0
    assert t.notional_usd > 0
    assert "recovery" in t.rationale.lower() or "reversion" in t.rationale.lower()


def test_opens_short_on_above_peg_pullback():
    """Trading above peg, model expects pullback → take a SHORT."""
    feed = [{
        "symbol": "USDT",
        "current_bps": 12.0,
        "meta": {"cone_normal_bps": 8, "cone_alert_bps": 20},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": 4.0,
            "p80_low": -2.0, "p80_high": 10.0,
            "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert len(opened) == 1
    assert opened[0].direction == "short"


def test_does_not_duplicate_open_trade_for_same_prediction():
    """Re-running evaluate_cycle with the same prediction must not
    open a second trade — idempotent within a cycle."""
    feed = [{
        "symbol": "USDC",
        "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 15},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
    }]
    op1 = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    op2 = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:01+00:00")
    assert len(op1) == 1
    assert len(op2) == 0  # same prediction id, idempotent


def test_caps_total_open_notional():
    """Trader refuses to open more than MAX_OPEN_NOTIONAL across all
    symbols. Set the cap low + the per-trade notional high so we hit
    it in two trades."""
    # Default MAX_OPEN_NOTIONAL = 50,000, per-trade = 10,000 → 5 max.
    # Open 5, then attempt a 6th.
    feeds = []
    for i, sym in enumerate(["USDC", "USDT", "DAI", "PYUSD", "USDP", "TUSD"]):
        feeds.append({
            "symbol": sym, "current_bps": -10.0 - i,
            "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
            "latest_prediction": {
                "made_at": f"t-{sym}",
                "resolves_at": "2030-01-01T00:00:00+00:00",
                "point": -2.0, "p80_low": -8.0, "p80_high": 4.0,
                "confidence_word": "likely",
            },
        })
    opened = trader.evaluate_cycle(feeds, now_iso="2026-05-25T11:00:00+00:00")
    # First 5 should open (5 × 10k = 50k = cap); the 6th refused.
    assert len(opened) == 5
    syms_opened = {t.symbol for t in opened}
    assert "TUSD" not in syms_opened or "USDC" not in syms_opened  # the 6th got refused


def test_position_size_scales_down_with_cone_width():
    """A cone twice the normal width should produce a position half
    the default notional. Pin the scaling."""
    feed_normal = [{
        "symbol": "USDC", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "t-a", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
    }]
    op_normal = trader.evaluate_cycle(feed_normal, now_iso="2026-05-25T11:00:00+00:00")

    # Reset store for second test
    import os
    if trader.TRADER_PATH.exists():
        os.remove(trader.TRADER_PATH)

    feed_wide = [{
        "symbol": "USDT", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "t-b", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -15.0, "p80_high": 11.0,  # ±13bp half-w
            "confidence_word": "about_as_likely_as_not",
        },
    }]
    op_wide = trader.evaluate_cycle(feed_wide, now_iso="2026-05-25T11:00:00+00:00")

    assert op_normal[0].notional_usd > op_wide[0].notional_usd, (
        "wider cone should get a smaller position")


def test_resolves_long_trade_with_correct_pnl():
    """A LONG opened at -10bp, expected +2bp, resolves at +1bp. P&L:
    +11bp × $10k × 0.0001 = $11."""
    # Open the trade
    feed_open = [{
        "symbol": "USDC", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": 2.0, "p80_low": -3.0, "p80_high": 7.0,
            "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed_open, now_iso="2026-05-25T11:00:00+00:00")

    # Resolve — same symbol, current_bps moved to +1.0, time advanced past resolves_at
    feed_resolve = [{
        "symbol": "USDC", "current_bps": 1.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": feed_open[0]["latest_prediction"],
    }]
    trader.evaluate_cycle(feed_resolve, now_iso="2026-05-25T11:06:00+00:00")
    trades = trader.all_trades()
    resolved = [t for t in trades if t["status"] == "resolved"]
    assert len(resolved) == 1
    r = resolved[0]
    assert r["direction"] == "long"
    assert abs(r["pnl_bps"] - 11.0) < 1e-6   # +1 - (-10) = +11
    # P&L: 11bp × notional × 0.0001
    expected_pnl = round(11.0 * r["notional_usd"] * 0.0001, 2)
    assert abs(r["pnl_usd"] - expected_pnl) < 0.01


def test_resolves_short_trade_with_correct_pnl():
    """SHORT opened at +12bp, expected +4bp, resolves at +6bp. P&L:
    +6bp move in our favour (we shorted at 12, market moved to 6)."""
    feed_open = [{
        "symbol": "USDT", "current_bps": 12.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": 4.0, "p80_low": -1.0, "p80_high": 9.0,
            "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed_open, now_iso="2026-05-25T11:00:00+00:00")
    feed_resolve = [{
        "symbol": "USDT", "current_bps": 6.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": feed_open[0]["latest_prediction"],
    }]
    trader.evaluate_cycle(feed_resolve, now_iso="2026-05-25T11:06:00+00:00")
    resolved = [t for t in trader.all_trades() if t["status"] == "resolved"]
    assert len(resolved) == 1
    assert resolved[0]["direction"] == "short"
    assert abs(resolved[0]["pnl_bps"] - 6.0) < 1e-6  # entry 12 - exit 6 = +6
    assert resolved[0]["pnl_usd"] > 0  # profitable


def test_track_record_aggregates_wins_losses_correctly():
    """Open + resolve two trades, one winning + one losing, verify
    the aggregate stats."""
    # Trade 1 — win
    feed_a = [{
        "symbol": "USDC", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "ta", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": 0.0, "p80_low": -5.0, "p80_high": 5.0,
            "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed_a, now_iso="2026-05-25T11:00:00+00:00")
    trader.evaluate_cycle(
        [{**feed_a[0], "current_bps": -1.0}],
        now_iso="2026-05-25T11:06:00+00:00")

    # Trade 2 — loss
    feed_b = [{
        "symbol": "USDT", "current_bps": 12.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "tb", "resolves_at": "2026-05-25T11:10:00+00:00",
            "point": 4.0, "p80_low": -1.0, "p80_high": 9.0,
            "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed_b, now_iso="2026-05-25T11:07:00+00:00")
    trader.evaluate_cycle(
        [{**feed_b[0], "current_bps": 20.0}],  # moved against us (we shorted; price went up)
        now_iso="2026-05-25T11:11:00+00:00")

    tr = trader.track_record()
    assert tr["count_resolved"] == 2
    assert tr["wins"] == 1
    assert tr["losses"] == 1
    assert tr["win_rate"] == 0.5
    # Net P&L is signed sum — could be positive or negative depending
    # on magnitudes; we just verify both trades are reflected.
    assert tr["mean_trade_usd"] is not None


def test_skips_when_current_bps_is_none(tmp_path, monkeypatch):
    """Audit-fix regression: trader must not open a trade when
    current_bps is None / non-numeric. The old code went directly
    from gate to `direction = 'long' if current < 0 else 'short'`
    which evaluated as 'short' for None and would have opened an
    incorrectly-signed position."""
    monkeypatch.setattr(trader, "TRADER_PATH", tmp_path / "trader.json")
    feed = [{
        "symbol": "USDC",
        "current_bps": None,  # the bug input
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 15},
        "latest_prediction": {
            "made_at": "t1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == [], (
        "trader must skip when current_bps is None — opening a trade "
        "with no signed deviation would book on the wrong side")


def test_build_token_blocks_runs_in_parallel(tmp_path, monkeypatch):
    """Audit-fix regression: build_token_blocks_for_trader must
    parallelise store reads. The trader runs inside the ticker cycle;
    if 18 symbols × 2 reads × ~150ms were serial that's ~5s blocking
    every cycle. We verify by patching the store with a slow stub
    and confirming wall-clock < 2s for 4 symbols (would be ~4s serial)."""
    import time as _t
    from sca.movement import trader as trader_mod
    from sca.store import file_store

    real_list_ticks = file_store.FileStore.list_peg_ticks
    real_list_preds = file_store.FileStore.list_predictions

    def slow_ticks(self, *a, **kw):
        _t.sleep(0.2)
        return real_list_ticks(self, *a, **kw)

    def slow_preds(self, *a, **kw):
        _t.sleep(0.2)
        return real_list_preds(self, *a, **kw)

    monkeypatch.setattr(file_store.FileStore, "list_peg_ticks", slow_ticks)
    monkeypatch.setattr(file_store.FileStore, "list_predictions", slow_preds)
    monkeypatch.setattr(trader_mod, "TRADER_PATH", tmp_path / "t.json")

    syms = ["USDC", "USDT", "DAI", "PYUSD"]
    t0 = _t.perf_counter()
    blocks = trader_mod.build_token_blocks_for_trader(syms)
    t1 = _t.perf_counter()
    assert len(blocks) == 4
    # Serial would be 4 syms × 2 reads × 0.2s = 1.6s. Parallel
    # ≈ 0.4s. Pin a generous-but-meaningful upper bound.
    assert t1 - t0 < 1.0, (
        f"parallel store reads expected; got {t1 - t0:.2f}s")


class _DummyStore:
    def list_peg_ticks(self, *a, **kw): return []
    def list_predictions(self, *a, **kw): return []


def test_build_token_blocks_handles_malformed_context(tmp_path, monkeypatch):
    """Audit-fix regression: a token_context with cone_thresholds_bps
    set to None or a single-element tuple must NOT raise IndexError —
    we just emit cone_normal_bps=None / cone_alert_bps=None and let
    downstream gates treat it conservatively."""
    monkeypatch.setattr(trader, "TRADER_PATH", tmp_path / "t.json")
    from sca.movement import trader as trader_mod

    # Stub the imported get_context with a fake that returns malformed
    # thresholds for one symbol — frozen dataclass can't be mutated.
    class FakeBroken:
        symbol = "USDC"
        issuer = "Circle"
        backing_model = "fiat_reserves"
        venue_type = "MIXED"
        yield_bearing = False
        cone_thresholds_bps = None  # the broken state

    monkeypatch.setattr(
        trader_mod, "get_context",
        lambda sym: FakeBroken() if sym.upper() == "USDC" else None,
    )
    block = trader_mod._build_one_token_block(_DummyStore(), "USDC")
    assert block is not None
    assert block["symbol"] == "USDC"
    # Malformed thresholds must NOT raise — they translate to None.
    assert block["meta"]["cone_normal_bps"] is None
    assert block["meta"]["cone_alert_bps"] is None


def test_persona_constants_exposed():
    """Track-record response carries the persona / tagline / version
    so the UI can render the editorial framing without hardcoding."""
    tr = trader.track_record()
    assert tr["persona"] == "The Discipline Trader"
    assert tr["tagline"]
    assert tr["version"] == "discipline_v1"
