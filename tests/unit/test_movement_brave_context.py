"""Brave context cost-discipline tests.

The Brave free tier is small. Five layers protect it:
  1. Hermetic mode (no key) — silent empty.
  2. 12h cache TTL (jittered).
  3. Interest gate — calm forecast skips the fetch.
  4. Daily quota cap.
  5. Stale-on-error.

These tests pin the contract on each layer. Network is mocked so the
suite remains hermetic.
"""
from __future__ import annotations

import json
import time

import pytest

from sca.movement import brave_context as bctx


def test_no_key_returns_empty(monkeypatch):
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "")
    assert bctx.fetch_context_for("USDC", "peg_deviation") == []


def test_cache_hit_avoids_network(monkeypatch, tmp_path):
    """A fresh cache entry must short-circuit before any network call."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    # Plant a fresh cache entry.
    cached = {"results": [{"title": "cached", "url": "x", "snippet": "",
                            "trust_tier": "web_unverified", "domain": "x"}]}
    bctx._save_cache({"USDC": {**cached,
                                "fetched_at": time.time(),
                                "ttl": 3600 * 12}})

    def fail_get(*a, **kw):
        raise AssertionError("Brave was called despite fresh cache")

    import requests
    monkeypatch.setattr(requests, "get", fail_get)
    out = bctx.fetch_context_for("USDC", "peg_deviation")
    assert out[0]["title"] == "cached"


def test_interest_gate_skips_calm_forecast(monkeypatch, tmp_path):
    """A calm confidence_word + cold cache must NOT make a network call.
    This is the most valuable cost guard — calm forecasts are exactly
    the ones where Brave context adds least value."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    import requests

    def fail_get(*a, **kw):
        raise AssertionError("Brave called on calm forecast")

    monkeypatch.setattr(requests, "get", fail_get)
    out = bctx.fetch_context_for(
        "USDC", "peg_deviation", confidence_word="very_likely",
    )
    assert out == []


def test_interest_gate_does_not_skip_drifting_point(monkeypatch, tmp_path):
    """Audit #11: a tight cone hides a drifting point. A calm
    confidence word + a large-magnitude point must NOT skip — that's
    the 'slow attack' shape where web context adds most value."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    called = {"n": 0}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"web": {"results": [
            {"url": "https://x.example", "title": "t",
             "description": "d"}]}}

    def fake_get(*a, **kw):
        called["n"] += 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    # very_likely + point at +18bp = "tight cone, drifting" — must fetch
    bctx.fetch_context_for(
        "USDC", "peg_deviation",
        confidence_word="very_likely", point_value=18.0,
    )
    assert called["n"] == 1


def test_interest_gate_skips_calm_AND_near_zero(monkeypatch, tmp_path):
    """The dual gate: calm word + |point| <= 5bp = skip."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    import requests

    def fail_get(*a, **kw):
        raise AssertionError("Brave called despite calm + near-zero")

    monkeypatch.setattr(requests, "get", fail_get)
    out = bctx.fetch_context_for(
        "USDC", "peg_deviation",
        confidence_word="very_likely", point_value=2.0,
    )
    assert out == []


def test_interest_gate_does_not_skip_unlikely(monkeypatch, tmp_path):
    """A 'likely' or weaker word is NOT calm — the fetch must run."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    called = {"n": 0}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"web": {"results": [
                {"url": "https://x.example", "title": "t", "description": "d"},
            ]}}

    def fake_get(*a, **kw):
        called["n"] += 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    out = bctx.fetch_context_for(
        "USDC", "peg_deviation", confidence_word="likely",
    )
    assert called["n"] == 1
    assert len(out) == 1


def test_force_overrides_interest_gate(monkeypatch, tmp_path):
    """force=True must bypass the calm-skip — the operator wants
    fresh context on demand."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    called = {"n": 0}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"web": {"results": []}}

    def fake_get(*a, **kw):
        called["n"] += 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    bctx.fetch_context_for(
        "USDC", "peg_deviation",
        confidence_word="very_likely", force=True,
    )
    assert called["n"] == 1


def test_daily_quota_cap_stops_calls(monkeypatch, tmp_path):
    """Once the daily cap is hit, no more calls — but stale cache is
    still served."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setenv("SCA_BRAVE_DAILY_QUOTA", "1")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    bctx._save_quota({"day": bctx._utc_today(), "calls": 1})
    called = {"n": 0}

    def fail_get(*a, **kw):
        called["n"] += 1
        raise AssertionError("Brave called after quota exhausted")

    import requests
    monkeypatch.setattr(requests, "get", fail_get)
    out = bctx.fetch_context_for(
        "USDC", "peg_deviation", confidence_word="exceptionally_unlikely",
    )
    assert called["n"] == 0
    assert out == []


def test_stale_on_error(monkeypatch, tmp_path):
    """Network failure with a stale cache must return the stale
    cache — better week-old than nothing."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    # Plant an EXPIRED cache entry.
    bctx._save_cache({"USDC": {
        "fetched_at": time.time() - 99999,
        "ttl": 1,
        "query": "old",
        "results": [{"title": "stale", "url": "x", "snippet": "",
                      "trust_tier": "web_unverified", "domain": "x"}],
    }})

    def boom(*a, **kw):
        raise RuntimeError("boom")

    import requests
    monkeypatch.setattr(requests, "get", boom)
    out = bctx.fetch_context_for(
        "USDC", "peg_deviation", confidence_word="exceptionally_unlikely",
    )
    assert len(out) == 1
    assert out[0]["title"] == "stale"


def test_coalesce_per_symbol(monkeypatch, tmp_path):
    """Cache is keyed by symbol, not (symbol, kind). The second call
    for a different kind on the same symbol must hit cache."""
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "x")
    monkeypatch.setattr(bctx, "_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    called = {"n": 0}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"web": {"results": [
            {"url": "https://x.example", "title": "t",
             "description": "d"}]}}

    def fake_get(*a, **kw):
        called["n"] += 1
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    bctx.fetch_context_for(
        "USDC", "peg_deviation", confidence_word="likely")
    bctx.fetch_context_for(
        "USDC", "net_flow_direction", confidence_word="likely")
    assert called["n"] == 1  # second call hit cache


def test_quota_resets_per_utc_day(monkeypatch, tmp_path):
    """A quota record from yesterday must reset to 0 for today."""
    monkeypatch.setattr(bctx, "_QUOTA_PATH", tmp_path / "quota.json")
    bctx._save_quota({"day": "2020-01-01", "calls": 999})
    state = bctx.quota_state()
    assert state["calls"] == 0
