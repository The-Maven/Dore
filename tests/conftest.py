"""Shared test fixtures. The whole suite runs offline and deterministic."""
from __future__ import annotations

import pytest
import requests

from sca import config
from sca.corpus import sources as sources_mod
from sca.tools import attestation_fetch as _afetch
from sca.tools import attestation_locator as _locator
from sca.tools import onchain_supply as _onchain

_FIFTY_M = 50_000_000


def _reset_caches() -> None:
    _onchain._cache.clear()
    from sca.agent.analyze import _analysis_cache

    _analysis_cache.clear()
    for fn in (config.stablecoins, config._raw, config.chains,
               sources_mod.all_sources):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Every test starts cold: fresh caches, isolated ledger + URL cache.

    The suite is hermetic regardless of .env — Supabase credentials are
    neutralised so get_store() always resolves to the file backend; no test
    ever touches the live database.
    """
    from sca import votes
    from sca.store import reset_store

    monkeypatch.setattr(config, "SUPABASE_URL", "")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_KEY", "")
    reset_store()
    monkeypatch.setattr(votes, "VOTES_PATH", tmp_path / "votes.yaml")
    monkeypatch.setattr(_afetch, "_CACHE", tmp_path / "attestation_cache.json")
    _reset_caches()
    yield
    reset_store()
    _reset_caches()


class FakeResponse:
    """A stand-in for requests.Response across EVM / Solana / Tron / HTTP."""

    def __init__(self, payload, *, status: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status
        self.text = text

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


@pytest.fixture
def fake_rpc(monkeypatch):
    """Offline mode: mock every chain RPC (evm / solana / tron) and block all
    attestation HTTP, so analyze() never touches the network. Every supply
    read resolves to 50,000,000 tokens."""
    raw_hex = hex(_FIFTY_M * 10**6)

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        body = json or {}
        method = body.get("method")
        if method == "eth_call":
            data = body["params"][0]["data"]
            if data == _onchain.SEL_TOTAL_SUPPLY:
                return FakeResponse({"result": raw_hex})
            if data == _onchain.SEL_DECIMALS:
                return FakeResponse({"result": hex(6)})
            return FakeResponse({"result": "0x"})
        if method == "getTokenSupply":
            return FakeResponse(
                {"result": {"value": {"amount": str(_FIFTY_M * 10**6),
                                      "decimals": 6}}}
            )
        if "triggerconstantcontract" in url:
            selector = body.get("function_selector", "")
            value = raw_hex if "totalSupply" in selector else hex(6)
            return FakeResponse({"constant_result": [value[2:]]})
        return FakeResponse({"result": "0x"})

    def blocked(*args, **kwargs):
        raise requests.exceptions.ConnectionError("network blocked in tests")

    monkeypatch.setattr(_onchain.requests, "post", fake_post)
    monkeypatch.setattr(_afetch.requests, "head", blocked)
    monkeypatch.setattr(_afetch.requests, "get", blocked)
    monkeypatch.setattr(_locator.requests, "get", blocked)
    return fake_post
