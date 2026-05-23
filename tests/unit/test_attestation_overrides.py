"""Attestation URL overrides — the store-side production source of truth.

The YAML `latest_attestation_url` is a bootstrap seed; the store override
is what survives a redeploy. These tests pin the contract:

- a stored override beats a YAML seed
- last-write-wins on `set_*` (the most recent row is the current URL)
- when `_head_ok` rejects the override, the resolver falls through to the
  next path (seed / web_search / locator) rather than serving a broken URL
- a discovered URL (via=web_search) writes through to the store so the
  next call short-circuits at step (1)
"""
from __future__ import annotations

from datetime import date

import pytest

import sca.tools.attestation_fetch as af
from sca.store import get_store
from sca.tools.attestation_fetch import resolve_url


def test_store_override_beats_yaml_seed(monkeypatch, tmp_path):
    """An override URL is preferred over the YAML seed."""
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: True)
    get_store().set_attestation_url_override(
        "USDC", "https://override.example/from-curator.pdf",
        via="manual", set_by=None,
    )
    out = resolve_url("USDC")
    assert out == {
        "url": "https://override.example/from-curator.pdf",
        "via": "store_override",
    }


def test_override_last_write_wins(monkeypatch, tmp_path):
    """Set twice — the most recent URL is what resolves."""
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: True)
    store = get_store()
    store.set_attestation_url_override(
        "USDC", "https://first.example/old.pdf", via="manual",
    )
    store.set_attestation_url_override(
        "USDC", "https://second.example/new.pdf", via="web_search",
    )
    out = resolve_url("USDC")
    assert out["url"] == "https://second.example/new.pdf"
    assert out["via"] == "store_override"


def _with_seed(monkeypatch, symbol: str, seed_url: str):
    """YAML seeds were removed in favour of DB-backed overrides; tests
    that need to exercise the seed-step branch monkeypatch a replacement
    Stablecoin object onto the registry (the dataclass is frozen)."""
    import dataclasses
    from sca import config
    coin = config.get_stablecoin(symbol)
    seeded = dataclasses.replace(coin, latest_attestation_url=seed_url)
    coins = dict(config.stablecoins())
    coins[symbol] = seeded
    monkeypatch.setattr(config, "stablecoins", lambda: coins)
    monkeypatch.setattr(config, "get_stablecoin",
                        lambda s: coins.get(s.upper(), seeded))


def test_broken_override_falls_through_to_seed(monkeypatch, tmp_path):
    """If the override URL HEAD-fails, the resolver tries the next path."""
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    _with_seed(monkeypatch, "USDC", "https://x.com/seed.pdf")
    get_store().set_attestation_url_override(
        "USDC", "https://dead.example/404.pdf",
    )

    def head(url, **k):
        return "dead.example" not in url

    monkeypatch.setattr(af, "_head_ok", head)
    out = resolve_url("USDC")
    # Falls through to the (injected) seed.
    assert out["via"] == "seed"


def test_seed_write_through_persists_to_store(monkeypatch, tmp_path):
    """When the seed path resolves, the URL is persisted so a future call
    short-circuits at the store-override step."""
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: True)
    _with_seed(monkeypatch, "USDC", "https://x.com/seed.pdf")
    # No override pre-set; first call falls through to seed and writes back.
    first = resolve_url("USDC")
    assert first["via"] == "seed"
    # The store now has the seed URL recorded.
    rec = get_store().get_attestation_url_override("USDC")
    assert rec is not None
    assert rec["via"] == "seed"
    assert rec["url"]  # the injected seed value


def test_list_overrides_returns_latest_per_symbol(tmp_path):
    """The Compendium needs one row per symbol, newest-first ordering."""
    store = get_store()
    store.set_attestation_url_override("USDC", "https://u.example/a.pdf")
    store.set_attestation_url_override("USDT", "https://t.example/a.pdf")
    store.set_attestation_url_override("USDC", "https://u.example/b.pdf")
    rows = store.list_attestation_url_overrides()
    by_symbol = {r["symbol"]: r["url"] for r in rows}
    # USDC's latest URL only — no duplicate rows
    assert by_symbol["USDC"] == "https://u.example/b.pdf"
    assert by_symbol["USDT"] == "https://t.example/a.pdf"
    assert len(rows) == 2
