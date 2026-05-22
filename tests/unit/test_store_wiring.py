"""The newly-wired call sites route durable state through `get_store()`.

These tests exercise the wiring with the hermetic `FileStore` only — they
prove the routing works and stays behaviour-identical, without a database.
"""
from __future__ import annotations

import pytest
import yaml

from sca import config, votes
from sca.corpus.ingest import ingest_source
from sca.corpus.retrieve import retrieve
from sca.corpus.sources import all_sources
from sca.store import FileStore, get_store, reset_store


# ── votes.py delegates to the Store ───────────────────────────────────
def test_votes_record_and_overrides_round_trip():
    votes.record_source_decision("mica-title-iii", "approved")
    assert votes.source_status_overrides() == {"mica-title-iii": "approved"}


def test_address_overrides_keep_tuple_keys():
    """The Store keys by 'symbol:chain'; votes.py must return tuple keys."""
    votes.record_address_decision("PYUSD", "ethereum", "verified")
    overrides = votes.address_verified_overrides()
    assert overrides == {("PYUSD", "ethereum"): True}


def test_record_address_decision_resolves_contract():
    """The Store needs a contract; votes.py resolves it from the registry."""
    coin = config.get_stablecoin("PYUSD")
    dep = next(d for d in coin.deployments if d.chain == "ethereum")
    votes.record_address_decision("PYUSD", "ethereum", "verified")
    data = yaml.safe_load(votes.VOTES_PATH.read_text())
    entry = data["address_decisions"][0]
    assert entry["contract"] == dep.contract
    assert entry["symbol"] == "PYUSD" and entry["chain"] == "ethereum"


def test_history_returns_full_ledger():
    votes.record_source_decision("genius-act", "approved")
    votes.record_address_decision("USDC", "ethereum", "verified")
    hist = votes.history()
    assert hist["source_decisions"][0]["id"] == "genius-act"
    assert hist["address_decisions"][0]["symbol"] == "USDC"


def test_curation_history_on_filestore(tmp_path):
    """The Store gained a curation_history() method."""
    store = FileStore(votes_path=tmp_path / "votes.yaml")
    store.record_source_decision("s1", "approved")
    hist = store.curation_history()
    assert hist["source_decisions"][0]["id"] == "s1"
    assert hist["address_decisions"] == []


# ── corpus sources route through Store.list_sources ───────────────────
def test_all_sources_reads_from_store():
    """all_sources() pulls the raw registry from get_store().list_sources()."""
    ids = {s.id for s in all_sources()}
    store_ids = {r["id"] for r in get_store().list_sources()}
    assert ids == store_ids and ids


# ── ingest / retrieve route through the Store ─────────────────────────
SAMPLE = """# Reserve Composition
Reserves consist of high-quality liquid assets such as Treasury bills.

# Redemption
Holders may redeem tokens at par within one business day.
"""


@pytest.fixture
def store_corpus(monkeypatch, tmp_path):
    """A fresh FileStore wired to tmp paths, installed as the singleton."""
    corpus = tmp_path / "data"
    corpus.mkdir()
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {"sources": [{"id": "test-reg", "title": "Test Regulation",
                          "tier": "primary", "status": "approved"}]}
        )
    )
    fs = FileStore(
        votes_path=tmp_path / "votes.yaml",
        sources_path=sources,
        corpus_dir=corpus,
    )
    import sca.store as store_pkg

    monkeypatch.setattr(store_pkg, "_store", fs)
    all_sources.cache_clear()
    yield fs
    reset_store()
    all_sources.cache_clear()


def test_ingest_then_retrieve_through_store(store_corpus):
    """ingest_source -> save_passages, retrieve <- get_passages, no disk path."""
    chunks = ingest_source("test-reg", SAMPLE)
    assert chunks
    # passages landed in the store
    assert store_corpus.get_passages("test-reg")
    passages = retrieve("redeem at par business day")
    assert passages
    assert passages[0].heading == "Redemption"
    assert passages[0].source_id == "test-reg"


def test_retrieve_gate_excludes_unapproved(monkeypatch, store_corpus):
    """Only approved sources are retrievable, even via the Store."""
    ingest_source("test-reg", SAMPLE)
    # Drop the only source's approval — retrieve's gate must exclude it.
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: ())
    assert retrieve("anything") == []
