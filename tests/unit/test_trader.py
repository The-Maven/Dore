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


@pytest.fixture(autouse=True)
def _no_llm_calls(monkeypatch):
    """Narration generation now runs inside evaluate_cycle. The unit
    tests are about decision/sizing logic, not the LLM. Stub the
    narrator to a fast deterministic string so the suite stays
    hermetic + sub-second."""
    from sca.movement import trader_voice
    monkeypatch.setattr(
        trader_voice, "generate_trade_narration",
        lambda trade: f"[stub] {trade.get('symbol', '?')} {trade.get('direction', '?')}",
    )


_FT_COUNTER = [1.0]


def _agreed_consensus(n_sources: int = 4, price: float = 1.0) -> dict:
    """A consensus block that satisfies v5's HIGH-tier source-count
    requirement (≥4 sources). Tests using this fixture express
    'this is a fully-corroborated setup; conviction is determined by
    edge and cone'.

    Each call advances `fetched_at` so consecutive calls (entry vs
    resolve) DON'T look like the same source-tick — STALE detection
    is reserved for the actual reuse-the-tick case."""
    _FT_COUNTER[0] += 10.0  # widen past the 0.5s same-tick window
    ft = _FT_COUNTER[0]
    return {
        "kind": "agreed",
        "max_disagreement_bps": 0.4,
        "sources": [
            {"name": f"src{i}", "price": price, "fetched_at": ft}
            for i in range(n_sources)
        ],
    }


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
        "consensus": _agreed_consensus(),
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert len(opened) == 1
    t = opened[0]
    assert t.symbol == "USDC"
    assert t.direction == "long"
    assert t.entry_bps == -10.0
    assert t.forecast_point_bps == -2.0
    assert t.notional_usd > 0
    assert t.conviction in ("HIGH", "MED")
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
        "consensus": _agreed_consensus(),
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
        "consensus": _agreed_consensus(),
    }]
    op1 = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    op2 = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:01+00:00")
    assert len(op1) == 1
    assert len(op2) == 0  # same prediction id, idempotent


def test_caps_total_open_notional(monkeypatch):
    """Trader refuses to open more than MAX_OPEN_NOTIONAL across all
    symbols. Set the MED tier notional and cap so 4 trades exhaust
    the concurrent cap."""
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 200_000.0)
    monkeypatch.setattr(
        trader, "CONVICTION_NOTIONAL", {"HIGH": 50_000.0, "MED": 5_000.0})
    monkeypatch.setattr(trader, "MAX_OPEN_NOTIONAL", 20_000.0)
    feeds = []
    for i, sym in enumerate(["USDC", "USDT", "DAI", "PYUSD", "USDP", "TUSD"]):
        feeds.append({
            "symbol": sym, "current_bps": -10.0 - i,
            "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
            "latest_prediction": {
                "made_at": f"t-{sym}",
                "resolves_at": "2030-01-01T00:00:00+00:00",
                "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
                "confidence_word": "likely",
            },
            "consensus": _agreed_consensus(n_sources=3),  # MED-tier exactly
        })
    opened = trader.evaluate_cycle(feeds, now_iso="2026-05-25T11:00:00+00:00")
    # First 4 fit ($20k cap / $5k each); 5th and 6th refused.
    assert len(opened) == 4


def test_caps_daily_budget(monkeypatch):
    """v3+ contract: even with concurrent slots free, the trader
    refuses to open if today's daily budget is exhausted."""
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 10_000.0)
    monkeypatch.setattr(
        trader, "CONVICTION_NOTIONAL", {"HIGH": 50_000.0, "MED": 10_000.0})
    monkeypatch.setattr(trader, "MAX_OPEN_NOTIONAL", 50_000.0)
    feeds = []
    for i, sym in enumerate(["USDC", "USDT", "DAI"]):
        feeds.append({
            "symbol": sym, "current_bps": -10.0 - i,
            "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
            "latest_prediction": {
                "made_at": f"t-{sym}",
                "resolves_at": "2030-01-01T00:00:00+00:00",
                "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
                "confidence_word": "likely",
            },
            "consensus": _agreed_consensus(n_sources=3),
        })
    opened = trader.evaluate_cycle(feeds, now_iso="2026-05-25T11:00:00+00:00")
    assert len(opened) == 1
    assert opened[0].day_utc == "2026-05-25"


def test_capital_cost_factor_shrinks_slow_redemption_assets():
    """Audit finding 4.2: position size must scale down on tokens
    that lock capital up. Same-day redemption gets full size; sUSDe's
    7-day cooldown gets 70%; USDY's 40-day lockup gets 30%. An
    unknown label stays at 1.0 — only KNOWN slow-redemption tokens
    are penalised, so this addition can't silently shrink anything."""
    f = trader._capital_cost_factor
    # fast-redemption labels — no penalty
    assert f("same-day") == 1.0
    assert f("instant on-chain") == 1.0
    assert f("T+0 mint/redeem (capped)") == 1.0
    # progressive penalties
    assert f("T+1 to T+2") == 0.85
    assert f("7-day cooldown") == 0.70
    assert f("40-day lockup, then daily") == 0.30
    # conservative defaults
    assert f(None) == 1.0
    assert f("") == 1.0
    assert f("unrecognised-label-from-future") == 1.0


def test_capital_cost_flows_into_trade_receipt():
    """The capital-cost shrink must be visible on the Trade record
    so the receipt can explain WHY a sUSDe position is $7k not $10k.
    Silent shrinks would break the user's receipt-transparency rule."""
    feed = [{
        "symbol": "USDC", "current_bps": -10.0,
        "meta": {
            "cone_normal_bps": 5, "cone_alert_bps": 25,
            "payout_timeline_label": "7-day cooldown",
        },
        "latest_prediction": {
            "made_at": "t-cap", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
        "consensus": _agreed_consensus(),
    }]
    import os
    if trader.TRADER_PATH.exists():
        os.remove(trader.TRADER_PATH)
    opened = trader.evaluate_cycle(
        feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened, "expected a trade to open"
    t = opened[0]
    assert t.payout_timeline_label == "7-day cooldown"
    assert t.capital_cost_scale == 0.70
    # And the notional should reflect the 70% shrink — at least
    # smaller than what the same setup would produce without a label.
    if trader.TRADER_PATH.exists():
        os.remove(trader.TRADER_PATH)
    feed_no_label = [{
        "symbol": "USDT", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "t-nocap", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
        "consensus": _agreed_consensus(),
    }]
    op_full = trader.evaluate_cycle(
        feed_no_label, now_iso="2026-05-25T11:00:00+00:00")
    assert op_full[0].notional_usd > t.notional_usd, (
        "capital-cost penalty must visibly shrink the notional")


def test_v5_high_conviction_sizes_up():
    """v5: a tight cone + many sources + big edge gets HIGH-tier
    sizing ($50k), not the default. This is the core v5 thesis —
    concentrate capital on real opportunities, not 16 mediocre ones."""
    feed = [{
        "symbol": "USDC", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "t-high", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
        "consensus": _agreed_consensus(n_sources=4),
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert len(opened) == 1
    assert opened[0].conviction == "HIGH"
    assert opened[0].notional_usd == trader.CONVICTION_NOTIONAL["HIGH"]


def test_v5_skips_low_conviction_setups():
    """v5: a weak setup (tiny edge, few sources) is SKIPPED entirely,
    not opened at a small size. Real desks pass on bad setups."""
    feed = [{
        "symbol": "USDC", "current_bps": -3.5,  # just above entry threshold
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "t-low", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -3.0,
            "p80_low": -5.0, "p80_high": -2.0,  # ±1.5bp cone
            "confidence_word": "about_as_likely_as_not",
        },
        # Only 2 sources — fails MED's 3-source floor
        "consensus": _agreed_consensus(n_sources=2),
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == [], (
        "weak setup with insufficient sources must be SKIPPED, not "
        "downsized")


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
        "consensus": _agreed_consensus(),
    }]
    trader.evaluate_cycle(feed_open, now_iso="2026-05-25T11:00:00+00:00")

    # Resolve — same symbol, current_bps moved to +1.0, time advanced past resolves_at
    feed_resolve = [{
        "symbol": "USDC", "current_bps": 1.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": feed_open[0]["latest_prediction"],
        "consensus": _agreed_consensus(),
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
        "consensus": _agreed_consensus(),
    }]
    trader.evaluate_cycle(feed_open, now_iso="2026-05-25T11:00:00+00:00")
    feed_resolve = [{
        "symbol": "USDT", "current_bps": 6.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": feed_open[0]["latest_prediction"],
        "consensus": _agreed_consensus(),
    }]
    trader.evaluate_cycle(feed_resolve, now_iso="2026-05-25T11:06:00+00:00")
    resolved = [t for t in trader.all_trades() if t["status"] == "resolved"]
    assert len(resolved) == 1
    assert resolved[0]["direction"] == "short"
    assert abs(resolved[0]["pnl_bps"] - 6.0) < 1e-6  # entry 12 - exit 6 = +6
    assert resolved[0]["pnl_usd"] > 0  # profitable


def test_track_record_aggregates_wins_losses_correctly(monkeypatch):
    """Open + resolve two trades, one winning + one losing, verify
    the aggregate stats. Lifts the daily budget so both fit; the
    cap test is separate."""
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 100_000.0)
    # Trade 1 — win
    feed_a = [{
        "symbol": "USDC", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "ta", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": 0.0, "p80_low": -5.0, "p80_high": 5.0,
            "confidence_word": "likely",
        },
        "consensus": _agreed_consensus(),
    }]
    trader.evaluate_cycle(feed_a, now_iso="2026-05-25T11:00:00+00:00")
    trader.evaluate_cycle(
        [{**feed_a[0], "current_bps": -1.0, "consensus": _agreed_consensus()}],
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
        "consensus": _agreed_consensus(),
    }]
    trader.evaluate_cycle(feed_b, now_iso="2026-05-25T11:07:00+00:00")
    trader.evaluate_cycle(
        [{**feed_b[0], "current_bps": 20.0, "consensus": _agreed_consensus()}],  # moved against us (we shorted; price went up)
        now_iso="2026-05-25T11:11:00+00:00")

    tr = trader.track_record()
    assert tr["count_resolved"] == 2
    assert tr["wins"] == 1
    assert tr["losses"] == 1
    assert tr["win_rate"] == 0.5
    # Net P&L is signed sum — could be positive or negative depending
    # on magnitudes; we just verify both trades are reflected.
    assert tr["mean_trade_usd"] is not None
    # New v2 fields: equity curve has both trades as cumulative points,
    # daily breakdown has the day they occurred on, current streak
    # reflects most-recent outcome.
    assert len(tr["equity_curve"]) == 2
    assert tr["current_streak"]["length"] >= 1
    assert tr["best_day"] is not None
    assert tr["best_day"]["pnl_usd"] >= tr["worst_day"]["pnl_usd"]


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


def test_v3_refuses_to_trade_on_disputed_consensus():
    """Production discipline: peg sources disagreeing by >5bp means
    the entry price is contested. Refuse to size in until ground
    truth resolves."""
    feed = [{
        "symbol": "USDC", "current_bps": -10.0,
        "consensus": {
            "kind": "disputed",
            "max_disagreement_bps": 7.2,
            "sources": [
                {"name": "coinbase", "price": 0.99990, "fetched_at": 100},
                {"name": "kraken", "price": 0.99997, "fetched_at": 100},
            ],
        },
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
            "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == [], (
        "must not enter a trade while peg sources are disputed — "
        "ground truth is contested")


def test_v3_captures_full_receipt_audit_trail():
    """v3 contract: every trade carries the per-source readings + the
    consensus state at entry. The UI renders this as a receipt."""
    feed = [{
        "symbol": "USDC", "current_bps": -10.0,
        "consensus": {
            "kind": "agreed", "max_disagreement_bps": 0.4,
            "sources": [
                {"name": "coinbase", "price": 0.99899, "fetched_at": 100},
                {"name": "kraken", "price": 0.99903, "fetched_at": 100.5},
                {"name": "coingecko", "price": 0.99901, "fetched_at": 100.7},
            ],
        },
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -2.0, "p50_low": -5.0, "p50_high": 1.0,
            "p80_low": -7.0, "p80_high": 3.0,
            "p95_low": -10.0, "p95_high": 6.0,
            "horizon_minutes": 5, "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert len(opened) == 1
    t = opened[0]
    # Receipt: source snapshot + consensus state + full forecast bands.
    assert t.entry_consensus_kind == "agreed"
    assert len(t.entry_sources) == 3
    assert {s["name"] for s in t.entry_sources} == {
        "coinbase", "kraken", "coingecko"}
    assert abs(t.entry_max_disagreement_bps - 0.4) < 1e-6
    assert t.forecast_p50_low == -5.0
    assert t.forecast_p95_high == 6.0
    assert t.horizon_minutes == 5
    assert t.edge_bps > 0
    # Edge for long-from-below-peg: p80_high - current = 3 - (-10) = 13
    assert abs(t.edge_bps - 13.0) < 1e-6


def test_v5_low_edge_skipped_even_on_narrow_cone():
    """v5 reverses v3's behaviour on micro-edges: a 2bp edge on a
    narrow cone used to open a tiny trade. Now it's SKIPPED because
    real desks don't take noise-sized positions. This is the change
    that turns 18 nibbling trades into a handful of meaningful ones."""
    feed = [{
        "symbol": "USDT", "current_bps": -10.0,
        "consensus": _agreed_consensus(),
        "meta": {"cone_normal_bps": 8, "cone_alert_bps": 20},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -9.5,
            "p80_low": -11.0, "p80_high": -8.0,  # narrow cone, 2bp edge
            "p50_low": -10.5, "p50_high": -9.0,
            "p95_low": -12.0, "p95_high": -7.0,
            "horizon_minutes": 5, "confidence_word": "likely",
        },
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == [], (
        "v5: a 2bp edge is noise. Skip the setup — don't open a "
        "dust position just because the cone is narrow.")


def test_v3_pnl_math_long_correct_under_wide_values(tmp_path, monkeypatch):
    """Stress: a long opened at -50bp resolving at -30bp earns
    +20bp × notional × 0.0001. Verify the math with non-trivial
    notional + bp values + scaled-up edge sizing."""
    monkeypatch.setattr(trader, "TRADER_PATH", tmp_path / "t.json")
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 100_000.0)
    feed = [{
        "symbol": "FRAX", "current_bps": -50.0,
        "consensus": _agreed_consensus(),
        "meta": {"cone_normal_bps": 6, "cone_alert_bps": 18},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": -45.0,
            "p80_low": -53.0, "p80_high": -42.0,
            "p50_low": -49.0, "p50_high": -46.0,
            "p95_low": -56.0, "p95_high": -39.0,
            "horizon_minutes": 5, "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    # Resolve at -30bp — way better than the model expected
    trader.evaluate_cycle(
        [{**feed[0], "current_bps": -30.0, "consensus": _agreed_consensus()}],
        now_iso="2026-05-25T11:06:00+00:00")
    resolved = [t for t in trader.all_trades() if t["status"] == "resolved"]
    assert len(resolved) == 1
    r = resolved[0]
    assert r["outcome"] == "WIN"
    # Long entry at -50, exit at -30 → +20bp move in our favour.
    assert abs(r["pnl_bps"] - 20.0) < 1e-6
    expected_usd = round(20.0 * r["notional_usd"] * 0.0001, 2)
    assert abs(r["pnl_usd"] - expected_usd) < 0.01
    # Cone calibration: exit at -30 is ABOVE p80_high(-42) → outside p80.
    assert r["landed_inside_p80"] is False
    assert r["landed_inside_p95"] is False  # also above p95_high(-39)


def test_v3_pnl_math_short_correct_under_wide_values(tmp_path, monkeypatch):
    """Mirror of the long test for short — entry at +40, exit at +10
    means a +30bp move in our favour."""
    monkeypatch.setattr(trader, "TRADER_PATH", tmp_path / "t.json")
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 100_000.0)
    feed = [{
        "symbol": "LUSD", "current_bps": 40.0,
        "consensus": _agreed_consensus(),
        "meta": {"cone_normal_bps": 6, "cone_alert_bps": 18},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": 35.0,
            "p80_low": 30.0, "p80_high": 43.0,
            "p50_low": 33.0, "p50_high": 41.0,
            "p95_low": 25.0, "p95_high": 47.0,
            "horizon_minutes": 5, "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    trader.evaluate_cycle(
        [{**feed[0], "current_bps": 10.0, "consensus": _agreed_consensus()}],  # big pullback past p95_low(25)
        now_iso="2026-05-25T11:06:00+00:00")
    resolved = [t for t in trader.all_trades() if t["status"] == "resolved"]
    assert len(resolved) == 1
    r = resolved[0]
    assert r["outcome"] == "WIN"
    assert r["direction"] == "short"
    # Short entry +40, exit +10 → +30bp move in our favour.
    assert abs(r["pnl_bps"] - 30.0) < 1e-6
    # Exit at +10 is below p95_low(25), so outside all bands.
    assert r["landed_inside_p50"] is False
    assert r["landed_inside_p80"] is False
    assert r["landed_inside_p95"] is False


def test_v3_pnl_math_loss_when_market_moves_against():
    """Long opened at -10, exit at -15 → market moved AGAINST us. Loss."""
    feed = [{
        "symbol": "USDC", "current_bps": -10.0,
        "consensus": _agreed_consensus(),
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": -7.0, "p80_low": -10.5, "p80_high": -3.5,
            "p50_low": -9.0, "p50_high": -5.0,
            "p95_low": -13.0, "p95_high": -1.0,
            "horizon_minutes": 5, "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    trader.evaluate_cycle(
        [{**feed[0], "current_bps": -15.0, "consensus": _agreed_consensus()}],  # market moved against us
        now_iso="2026-05-25T11:06:00+00:00")
    resolved = [t for t in trader.all_trades() if t["status"] == "resolved"]
    r = resolved[0]
    assert r["outcome"] == "LOSS"
    assert r["pnl_usd"] < 0
    # Long -10 → -15: that's -5bp against us
    assert abs(r["pnl_bps"] + 5.0) < 1e-6


def test_build_token_blocks_propagates_consensus_to_trader():
    """Regression for the autonomous-check finding: empty receipts.

    `build_token_blocks_for_trader` MUST include the latest peg_tick's
    consensus_kind + sources + max_disagreement_bps so the trader can
    stamp them on the receipt. Without this, every trade in production
    showed entry_sources=[] and entry_consensus_kind=""."""
    class _StoreWithConsensus:
        def list_peg_ticks(self, sym, limit=1):
            return [{
                "deviation_bps": -8.0,
                "consensus_kind": "agreed",
                "max_disagreement_bps": 0.45,
                "sources": [
                    {"name": "coinbase", "price": 0.99920, "fetched_at": 1.0},
                    {"name": "kraken", "price": 0.99924, "fetched_at": 1.1},
                ],
            }]
        def list_predictions(self, *a, **kw): return []
    block = trader._build_one_token_block(_StoreWithConsensus(), "USDC")
    assert block is not None
    cons = block.get("consensus") or {}
    assert cons.get("kind") == "agreed", (
        "consensus_kind must propagate through to the trader — "
        "without it, the receipt audit trail is empty")
    assert abs(cons.get("max_disagreement_bps") - 0.45) < 1e-6
    assert len(cons.get("sources") or []) == 2
    src_names = [s.get("name") for s in cons["sources"]]
    assert "coinbase" in src_names and "kraken" in src_names


def test_v5_high_tier_three_times_med_tier():
    """A 6bp edge + 4 sources → HIGH tier ($50k). A 3bp edge + 3
    sources → MED tier ($15k). The tier jump is intentional and
    visible: real desks size meaningfully different across conviction."""
    # MED setup: 3bp edge, 3 sources
    feed_med = [{
        "symbol": "USDC", "current_bps": -7.0,
        "consensus": _agreed_consensus(n_sources=3),
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
        "latest_prediction": {
            "made_at": "p-med", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -5.0,
            "p80_low": -8.0, "p80_high": -4.0,  # edge = 3bp
            "confidence_word": "likely",
        },
    }]
    op_med = trader.evaluate_cycle(
        feed_med, now_iso="2026-05-25T11:00:00+00:00")
    import os
    if trader.TRADER_PATH.exists():
        os.remove(trader.TRADER_PATH)
    # HIGH setup: 6bp edge, 4 sources
    feed_high = [{
        "symbol": "USDT", "current_bps": -10.0,
        "consensus": _agreed_consensus(n_sources=4),
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
        "latest_prediction": {
            "made_at": "p-high", "resolves_at": "2030-01-01T00:00:00+00:00",
            "point": -5.0,
            "p80_low": -11.5, "p80_high": -4.0,  # edge = 6bp
            "confidence_word": "likely",
        },
    }]
    op_high = trader.evaluate_cycle(
        feed_high, now_iso="2026-05-25T11:00:00+00:00")
    assert op_med[0].conviction == "MED"
    assert op_high[0].conviction == "HIGH"
    assert op_high[0].notional_usd >= 3 * op_med[0].notional_usd, (
        "HIGH tier must be meaningfully bigger than MED — the whole "
        "v5 thesis is conviction-based concentration")


def test_stale_data_outcome_excluded_from_win_loss_math(tmp_path, monkeypatch):
    """Never-sketchy rule: when entry and exit reference the same
    source-tick (no fresh data between them), mark the trade STALE
    and exclude from W/L math. The user's complaint about $0.00 FLAT
    outcomes was actually stale-data — calling them WINS or LOSSES
    would be dishonest."""
    import time as _t
    monkeypatch.setattr(trader, "TRADER_PATH", tmp_path / "t.json")
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 100_000.0)
    # Use a fresh fetched_at so the entry passes the freshness gate.
    # The STALE detection happens on resolve, when entry and exit
    # reference the SAME source-tick (same fetched_at).
    fresh_ts = _t.time()
    # v5: need ≥3 agreeing sources to clear conviction tier. STALE
    # detection still triggers when the SAME tick is reused at exit.
    entry_consensus = {
        "kind": "agreed", "max_disagreement_bps": 0.4,
        "sources": [
            {"name": "coinbase", "price": 0.998, "fetched_at": fresh_ts},
            {"name": "kraken",   "price": 0.998, "fetched_at": fresh_ts},
            {"name": "coingecko","price": 0.998, "fetched_at": fresh_ts},
            {"name": "pyth",     "price": 0.998, "fetched_at": fresh_ts},
        ],
    }
    feed = [{
        "symbol": "USDC", "current_bps": -20.0,  # below structural-depeg threshold
        "consensus": entry_consensus,
        "meta": {"cone_normal_bps": 6, "cone_alert_bps": 25,
                  "yield_bearing": False},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": -15.0, "p80_low": -22.0, "p80_high": -14.0,
            "horizon_minutes": 5, "confidence_word": "likely",
        },
    }]
    trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    # Resolve with SAME current_bps + SAME source fetched_at (the
    # stale-data signature: source never refreshed between entry + exit)
    feed_resolve = [{
        **feed[0],
        "current_bps": -80.0,
        "consensus": entry_consensus,  # identical, same fetched_at
    }]
    trader.evaluate_cycle(feed_resolve, now_iso="2026-05-25T11:06:00+00:00")
    resolved = [t for t in trader.all_trades() if t["status"] == "resolved"]
    assert len(resolved) == 1
    r = resolved[0]
    assert r["outcome"] == "STALE", (
        f"expected STALE outcome for same-source-tick resolution, got "
        f"{r['outcome']!r}")
    # Win/loss math must exclude it
    tr = trader.track_record()
    assert tr["wins"] == 0
    assert tr["losses"] == 0


def test_persona_constants_exposed():
    """Track-record response carries the persona / tagline / version
    so the UI can render the editorial framing without hardcoding."""
    tr = trader.track_record()
    assert tr["persona"] == "The Discipline Trader"
    assert tr["tagline"]
    assert tr["version"] == "discipline_v5"
    # v2 contract: daily budget tracking present even with no trades
    assert "daily_budget_usd" in tr
    assert tr["daily_budget_usd"] == 100_000.0
    assert "budget_remaining_today_usd" in tr
    assert "current_streak" in tr


def test_daily_budget_resets_at_new_utc_day(monkeypatch, tmp_path):
    """v2 contract: opening a position on day N consumes day N's
    budget. The next UTC day, the budget is fresh."""
    # Set DAILY_BUDGET = MED notional = $10k so one MED trade exhausts.
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 10_000.0)
    monkeypatch.setattr(
        trader, "CONVICTION_NOTIONAL", {"HIGH": 50_000.0, "MED": 10_000.0})
    feed = [{
        "symbol": "USDC", "current_bps": -7.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": -3.0, "p80_low": -8.0, "p80_high": -4.0,  # 3bp edge → MED
            "confidence_word": "likely",
        },
        "consensus": _agreed_consensus(n_sources=3),
    }]
    op1 = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert len(op1) == 1
    feed2 = [dict(feed[0], latest_prediction={
        **feed[0]["latest_prediction"], "made_at": "p2"})]
    op2 = trader.evaluate_cycle(feed2, now_iso="2026-05-25T11:01:00+00:00")
    assert op2 == [], "second trade same day should be refused (budget exhausted)"
    feed3 = [dict(feed[0], latest_prediction={
        **feed[0]["latest_prediction"], "made_at": "p3"})]
    op3 = trader.evaluate_cycle(feed3, now_iso="2026-05-26T11:00:00+00:00")
    assert len(op3) == 1, "new day must reset the daily budget"
    assert op3[0].day_utc == "2026-05-26"


def test_v5_concentrates_capital_skipping_weak_setups():
    """The headline v5 behavioural claim: given a feed of 6 tokens
    where 1 has a HIGH-tier setup and 5 have weak signals, the
    trader opens ONE meaningful trade (the HIGH one) instead of 6
    nibbling trades. This is the regression test for the user's
    'sub-$1 P&L on $100k notional' complaint."""
    feed = [
        # HIGH-tier opportunity — 6bp edge, 4 sources, tight cone
        {
            "symbol": "USDC", "current_bps": -10.0,
            "consensus": _agreed_consensus(n_sources=4),
            "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
            "latest_prediction": {
                "made_at": "p-high", "resolves_at": "2030-01-01T00:00:00+00:00",
                "point": -5.0, "p80_low": -11.0, "p80_high": -4.0,
                "confidence_word": "likely",
            },
        },
    ]
    # 5 weak signals with single-source consensus (LOW conviction)
    for sym in ["USDT", "DAI", "PYUSD", "TUSD", "FDUSD"]:
        feed.append({
            "symbol": sym, "current_bps": -4.0,
            "consensus": _agreed_consensus(n_sources=1),  # below MED's 3-source floor
            "meta": {"cone_normal_bps": 5, "cone_alert_bps": 20},
            "latest_prediction": {
                "made_at": f"p-{sym}",
                "resolves_at": "2030-01-01T00:00:00+00:00",
                "point": -3.0, "p80_low": -5.0, "p80_high": -2.5,  # 1.5bp edge
                "confidence_word": "about_as_likely_as_not",
            },
        })
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert len(opened) == 1, (
        f"v5 must skip weak setups instead of taking 6 nibbling "
        f"trades; opened {len(opened)} instead")
    assert opened[0].symbol == "USDC"
    assert opened[0].conviction == "HIGH"
    assert opened[0].notional_usd == trader.CONVICTION_NOTIONAL["HIGH"]


def test_v5_refuses_structurally_depegged_token(monkeypatch):
    """v5: a token whose deviation has sat beyond ±50bp for 5+
    consecutive ticks is BROKEN, not mean-reverting. Real example:
    USDD sat at -39bp for hours; the old trader longed it every
    cycle expecting return-to-$1 and got STALE outcomes.

    This is the structural-depeg refusal — once enough cycles
    confirm the deviation is persistent, the trader refuses to
    treat it as mean-reversion."""
    class _DepeggedStore:
        def list_peg_ticks(self, sym, limit=5):
            return [{"deviation_bps": -78.0}] * 5  # all at -78bp
    monkeypatch.setattr(
        "sca.store.get_store", lambda: _DepeggedStore())
    feed = [{
        "symbol": "FRAX", "current_bps": -78.0,
        "meta": {"cone_normal_bps": 6, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": -70.0, "p80_low": -82.0, "p80_high": -68.0,
            "confidence_word": "likely",
        },
        "consensus": _agreed_consensus(),
    }]
    opened = trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    assert opened == [], (
        "v5 must refuse structurally-depegged tokens — longing FRAX "
        "at -78bp expecting return to $1 is the noise-fishing "
        "pattern v5 was built to stop")


def test_outcome_label_set_on_resolve(monkeypatch):
    """v2 contract: each resolved trade carries outcome ∈
    {WIN, LOSS, FLAT} for the UI to render WIN/LOSS labels."""
    monkeypatch.setattr(trader, "DAILY_BUDGET_USD", 100_000.0)
    feed = [{
        "symbol": "USDC", "current_bps": -10.0,
        "meta": {"cone_normal_bps": 5, "cone_alert_bps": 25},
        "latest_prediction": {
            "made_at": "p1", "resolves_at": "2026-05-25T11:05:00+00:00",
            "point": 0.0, "p80_low": -5.0, "p80_high": 5.0,
            "confidence_word": "likely",
        },
        "consensus": _agreed_consensus(),
    }]
    trader.evaluate_cycle(feed, now_iso="2026-05-25T11:00:00+00:00")
    # Resolve as a win
    trader.evaluate_cycle(
        [{**feed[0], "current_bps": -1.0, "consensus": _agreed_consensus()}],
        now_iso="2026-05-25T11:06:00+00:00")
    resolved = [t for t in trader.all_trades() if t["status"] == "resolved"]
    assert resolved
    assert resolved[0]["outcome"] == "WIN"
    assert resolved[0]["pnl_usd"] > 0
