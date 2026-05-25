"""Shared test fixtures. The whole suite runs offline and deterministic."""
from __future__ import annotations

import pytest
import requests

import importlib

# `sca.corpus.retrieve` resolves to the re-exported function via __init__.py;
# load the submodule explicitly so we can reset its once-per-process flag.
_retrieve_mod = importlib.import_module("sca.corpus.retrieve")

from sca import config
from sca.corpus import ingest as _ingest_mod
from sca.corpus import sources as sources_mod
from sca.store import FileStore as _FileStore
from sca.tools import attestation_fetch as _afetch
from sca.tools import attestation_locator as _locator
from sca.tools import onchain_supply as _onchain
from sca.tools import paxos_resolver as _paxos

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
    # Reset the once-per-process auto-ingest guard so each test starts cold.
    _retrieve_mod.reset_auto_ingest()


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Every test starts cold: fresh caches, isolated ledger + URL cache.

    The suite is hermetic regardless of .env — Supabase credentials are
    neutralised so get_store() always resolves to the file backend; no test
    ever touches the live database.
    """
    from sca import auto_verify, snapshots, supply_history, votes
    from sca.store import reset_store

    monkeypatch.setattr(config, "SUPABASE_URL", "")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_KEY", "")
    # Movement simulator: keep the ticker thread + live peg-price
    # fetch off in every test. Tests construct forecasts and
    # resolutions directly; the background loop and HTTP fetches stay
    # out of the path.
    monkeypatch.setenv("SCA_MOVEMENT_TICKER_DISABLED", "1")
    monkeypatch.setenv("SCA_PEG_PRICE_DISABLED", "1")
    # Disable the simulator /feed cache so tests that mock store data
    # see fresh payloads on every call (the 3s prod cache would leak
    # fixture data across tests).
    monkeypatch.setenv("SCA_SIMULATOR_FEED_CACHE_DISABLED", "1")
    # Chaos-engineering daemon never spawns in tests; scenarios can be
    # invoked directly via test_chaos.py using run_one_cycle().
    monkeypatch.setenv("SCA_CHAOS_DISABLED", "1")
    # Isolate the movement simulator config file so test edits don't
    # leak into the real data/movement_config.json.
    try:
        from sca.movement import config as _sim_cfg_mod
    except Exception:  # noqa: BLE001 - module may not exist in older trees
        _sim_cfg_mod = None
    if _sim_cfg_mod is not None:
        monkeypatch.setattr(
            _sim_cfg_mod, "_CONFIG_PATH", tmp_path / "movement_config.json",
        )
    try:
        from sca.movement import brave_context as _brave_ctx_mod
    except Exception:  # noqa: BLE001
        _brave_ctx_mod = None
    if _brave_ctx_mod is not None:
        monkeypatch.setattr(
            _brave_ctx_mod, "_CACHE_PATH",
            tmp_path / "movement_brave_context.json",
        )
        monkeypatch.setattr(
            _brave_ctx_mod, "_QUOTA_PATH",
            tmp_path / "movement_brave_quota.json",
        )
    try:
        from sca.movement import commentary as _comm_mod
    except Exception:  # noqa: BLE001
        _comm_mod = None
    if _comm_mod is not None:
        monkeypatch.setattr(
            _comm_mod, "_CACHE_PATH",
            tmp_path / "movement_commentary_cache.json",
        )
    # Belt-and-braces: even with no Brave key in test env, ensure the
    # SCA_WEB_SEARCH_KEY env var is empty so brave_context.fetch
    # returns [] immediately without trying network.
    monkeypatch.setenv("SCA_WEB_SEARCH_KEY", "")
    reset_store()
    monkeypatch.setattr(votes, "VOTES_PATH", tmp_path / "votes.yaml")
    monkeypatch.setattr(_afetch, "_CACHE", tmp_path / "attestation_cache.json")
    monkeypatch.setattr(
        _paxos, "_CACHE_PATH", tmp_path / "paxos_resolved.json",
    )
    monkeypatch.setattr(
        auto_verify, "AUTO_VERIFICATIONS_PATH",
        tmp_path / "auto_verifications.json",
    )
    monkeypatch.setattr(
        supply_history, "HISTORY_PATH", tmp_path / "supply_history.json",
    )
    monkeypatch.setattr(
        snapshots, "SNAPSHOTS_DIR", tmp_path / "source_snapshots",
    )
    # Isolate the FileStore's corpus passage directory and the staging
    # auto-ingest from real on-disk state: each test starts with an empty
    # corpus, and only what the test stages is ever ingested.
    import sca.store as _store_mod

    monkeypatch.setattr(
        _store_mod, "FileStore",
        lambda **kw: _FileStore(
            corpus_dir=tmp_path / "corpus_data",
            attestation_overrides_path=tmp_path / "attestation_overrides.json",
            verified_facts_path=tmp_path / "verified_facts.jsonl",
            **kw,
        ),
    )
    monkeypatch.setattr(_ingest_mod, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(
        _ingest_mod, "INGEST_STATE_PATH",
        tmp_path / "corpus_ingest_state.json",
    )
    # Discovery ledger lives in DATA_DIR by default; isolate it so no test
    # ever writes to data/discovered_sources.json on the real disk.
    try:
        from sca import discovery as _discovery_mod
    except Exception:  # noqa: BLE001 - module may not exist in older trees
        _discovery_mod = None
    if _discovery_mod is not None:
        monkeypatch.setattr(
            _discovery_mod, "DISCOVERED_PATH",
            tmp_path / "discovered_sources.json",
        )
    # Health-thread state file lives in DATA_DIR too — isolate the same
    # way so flip-detection tests don't poison subsequent runs.
    try:
        from sca import health_thread as _health_mod
    except Exception:  # noqa: BLE001 - module may not exist in older trees
        _health_mod = None
    if _health_mod is not None:
        monkeypatch.setattr(
            _health_mod, "HEALTH_STATE_PATH",
            tmp_path / "health_state.json",
        )
        # Leader-election lockfile: isolate so the test suite never races
        # against a real lockfile in data/, and so each test starts
        # without an existing leader.
        if hasattr(_health_mod, "LEADER_LOCKFILE"):
            monkeypatch.setattr(
                _health_mod, "LEADER_LOCKFILE",
                tmp_path / "health_thread.leader",
            )
        _health_mod._reset_for_tests()
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

    def fake_post(url, json=None, timeout=None, headers=None):  # noqa: A002
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
        if method == "getAccountInfo":
            # Solana SPL mint shape — what address_verify._read_solana
            # consumes. Owner = SPL Token Program; data.parsed.info has
            # decimals matching the registry's 6-decimal default.
            return FakeResponse({"result": {"value": {
                "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {
                    "type": "mint",
                    "info": {"decimals": 6, "supply": str(_FIFTY_M * 10**6)},
                }, "program": "spl-token"},
                "lamports": 1, "executable": False, "rentEpoch": 0,
            }}})
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
    monkeypatch.setattr(_paxos.requests, "head", blocked)
    monkeypatch.setattr(_paxos.requests, "get", blocked)
    return fake_post
