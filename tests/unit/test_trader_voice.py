"""Trader voice — LLM narration layer.

Voice never blocks the trader: an LLM failure must fall back to a
deterministic template, every artifact must be cached on first
generation, and re-runs with identical inputs must hit the cache."""
from __future__ import annotations

import json
import pytest

from sca.movement import trader_voice as tv


@pytest.fixture(autouse=True)
def _isolated_voice_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "VOICE_DIR", tmp_path)


def _fake_llm_returns(text: str | None):
    """Patch the LLM call to return `text`, or None to simulate failure."""
    def _patch(monkeypatch):
        monkeypatch.setattr(tv, "_llm_complete",
                             lambda prompt, max_tokens=350: text)
    return _patch


# ── daily brief ────────────────────────────────────────────────────
def test_daily_brief_generates_and_caches(monkeypatch):
    _fake_llm_returns("Watching USDC -8bp and USDT +5bp today.")(monkeypatch)
    brief = tv.generate_daily_brief(
        day_utc="2026-05-25",
        token_states=[
            {"symbol": "USDC", "current_bps": -8.0, "cone_half_bps": 3.0},
            {"symbol": "USDT", "current_bps": 5.0, "cone_half_bps": 2.5},
        ],
        recent_news=[{"title": "Tether transparency report released"}],
    )
    assert brief.day_utc == "2026-05-25"
    assert "USDC" in brief.body
    assert not brief.fallback
    # Second call with same inputs hits the cache (no LLM hit)
    monkeypatch.setattr(tv, "_llm_complete",
                         lambda *a, **k: pytest.fail(
                             "LLM should not be called on cache hit"))
    cached = tv.generate_daily_brief(
        day_utc="2026-05-25",
        token_states=[
            {"symbol": "USDC", "current_bps": -8.0, "cone_half_bps": 3.0},
            {"symbol": "USDT", "current_bps": 5.0, "cone_half_bps": 2.5},
        ],
        recent_news=[{"title": "Tether transparency report released"}],
    )
    assert cached.body == brief.body


def test_daily_brief_falls_back_on_llm_failure(monkeypatch):
    """LLM down → deterministic template, fallback flag set, voice
    keeps producing output."""
    _fake_llm_returns(None)(monkeypatch)
    brief = tv.generate_daily_brief(
        day_utc="2026-05-25",
        token_states=[
            {"symbol": "USDC", "current_bps": -12.0, "cone_half_bps": 3.0},
        ],
        recent_news=[],
    )
    assert brief.fallback is True
    assert brief.body  # something must be written
    assert "USDC" in brief.body


def test_daily_brief_force_refresh_bypasses_cache(monkeypatch):
    _fake_llm_returns("First version")(monkeypatch)
    tv.generate_daily_brief(
        day_utc="2026-05-25",
        token_states=[{"symbol": "USDC", "current_bps": -10.0,
                        "cone_half_bps": 3.0}],
        recent_news=[],
    )
    _fake_llm_returns("Second version after refresh")(monkeypatch)
    refreshed = tv.generate_daily_brief(
        day_utc="2026-05-25",
        token_states=[{"symbol": "USDC", "current_bps": -10.0,
                        "cone_half_bps": 3.0}],
        recent_news=[],
        force_refresh=True,
    )
    assert "Second version" in refreshed.body


# ── trade narration ────────────────────────────────────────────────
def test_trade_narration_caches_by_trade_id(monkeypatch):
    _fake_llm_returns("Long USDC $50k — 4-source agreed setup.")(monkeypatch)
    trade = {
        "id": "t-narr-1", "symbol": "USDC", "direction": "long",
        "notional_usd": 50_000, "entry_bps": -10.0, "edge_bps": 5.5,
        "conviction": "HIGH", "strategy": "mean_reversion",
        "entry_consensus_kind": "agreed",
        "entry_sources": [{"name": "a"}, {"name": "b"}, {"name": "c"},
                           {"name": "d"}],
        "rationale": "Mean reversion edge to peg",
        "entry_news_context": [],
    }
    first = tv.generate_trade_narration(trade)
    assert "USDC" in first
    # Second call with same id hits cache regardless of content
    monkeypatch.setattr(tv, "_llm_complete",
                         lambda *a, **k: pytest.fail(
                             "LLM should not be called on cache hit"))
    second = tv.generate_trade_narration(trade)
    assert second == first


def test_trade_narration_falls_back_to_template_on_llm_failure(monkeypatch):
    _fake_llm_returns(None)(monkeypatch)
    trade = {
        "id": "t-fail-1", "symbol": "USDC", "direction": "long",
        "notional_usd": 50_000, "entry_bps": -10.0, "edge_bps": 5.5,
        "conviction": "HIGH", "strategy": "mean_reversion",
        "entry_consensus_kind": "agreed",
        "entry_sources": [{"name": "a"}, {"name": "b"}, {"name": "c"},
                           {"name": "d"}],
        "rationale": "Mean reversion edge to peg",
        "entry_news_context": [],
    }
    text = tv.generate_trade_narration(trade)
    assert text  # non-empty
    assert "HIGH" in text
    assert "USDC" in text


# ── end-of-day reflection ──────────────────────────────────────────
def test_reflection_with_no_trades_returns_quiet_day_message():
    refl = tv.generate_reflection(
        day_utc="2026-05-24", trades_today=[],
    )
    assert "no resolved trades" in refl.body.lower() or "quiet" in refl.body.lower()


def test_reflection_aggregates_stats_deterministically(monkeypatch):
    """The LLM gets pre-computed stats; reflection.stats dict is the
    deterministic aggregation regardless of LLM outcome."""
    _fake_llm_returns("Solid day for mean reversion.")(monkeypatch)
    trades = [
        {"id": "t1", "status": "resolved", "outcome": "WIN",
          "symbol": "USDC", "strategy": "mean_reversion",
          "conviction": "HIGH", "pnl_usd": 12.5},
        {"id": "t2", "status": "resolved", "outcome": "LOSS",
          "symbol": "USDT", "strategy": "cross_venue_arb",
          "conviction": "MED", "pnl_usd": -4.0},
        {"id": "t3", "status": "resolved", "outcome": "WIN",
          "symbol": "DAI", "strategy": "mean_reversion",
          "conviction": "HIGH", "pnl_usd": 8.0},
    ]
    refl = tv.generate_reflection(
        day_utc="2026-05-24", trades_today=trades,
    )
    assert refl.stats["n_trades"] == 3
    assert refl.stats["wins"] == 2
    assert refl.stats["losses"] == 1
    assert refl.stats["total_pnl_usd"] == 16.5
    assert refl.stats["by_strategy"]["mean_reversion"]["pnl"] == 20.5
    assert refl.stats["by_conviction"]["HIGH"]["pnl"] == 20.5
    assert "Solid day" in refl.body


def test_reflection_excludes_stale_trades_from_aggregation(monkeypatch):
    """STALE trades aren't real outcomes — they must not pollute
    the reflection's win/loss math."""
    _fake_llm_returns("Two real outcomes today.")(monkeypatch)
    trades = [
        {"id": "t1", "status": "resolved", "outcome": "WIN",
          "symbol": "USDC", "strategy": "mean_reversion",
          "conviction": "HIGH", "pnl_usd": 10.0},
        {"id": "t2", "status": "resolved", "outcome": "STALE",
          "symbol": "USDD", "strategy": "mean_reversion",
          "conviction": "MED", "pnl_usd": 0.0},
    ]
    refl = tv.generate_reflection(
        day_utc="2026-05-24", trades_today=trades,
    )
    assert refl.stats["n_trades"] == 1  # STALE excluded
