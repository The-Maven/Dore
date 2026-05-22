"""FileStore behaviour — the hermetic backend.

No network, no `supabase` package, no DB. analyses + monitor live in memory;
sources/curation/corpus use tmp dirs so the suite stays deterministic.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
import yaml

from sca.store import FileStore, Store, get_store


@pytest.fixture
def store(tmp_path):
    """A FileStore wired entirely to tmp paths."""
    corpus = tmp_path / "data"
    corpus.mkdir()
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "id": "genius-act",
                        "title": "GENIUS Act",
                        "tier": "primary",
                        "status": "proposed",
                        "url": "https://example.gov/genius",
                    },
                    {
                        "id": "mica-title-iii",
                        "title": "MiCA Title III",
                        "tier": "primary",
                        "status": "approved",
                    },
                ]
            }
        )
    )
    return FileStore(
        votes_path=tmp_path / "votes.yaml",
        sources_path=sources,
        corpus_dir=corpus,
    )


# ── selection ─────────────────────────────────────────────────────────
def test_get_store_returns_filestore_offline():
    """With no Supabase env, get_store() yields a FileStore singleton."""
    s = get_store()
    assert type(s).__name__ == "FileStore"
    assert isinstance(s, Store)
    assert get_store() is s  # process singleton


# ── analyses ──────────────────────────────────────────────────────────
def test_create_and_get_analysis(store):
    aid = store.create_analysis("attestation", "USDC", user_id="u1")
    row = store.get_analysis(aid)
    assert row["surface"] == "attestation"
    assert row["symbol"] == "USDC"
    assert row["status"] == "pending"
    assert row["user_id"] == "u1"
    assert row["result"] is None


def test_get_missing_analysis_returns_none(store):
    assert store.get_analysis("no-such-id") is None


def test_update_analysis_lifecycle(store):
    aid = store.create_analysis("sanctions", "USDT")
    store.update_analysis(aid, status="running", input_fingerprint="fp1")
    row = store.get_analysis(aid)
    assert row["status"] == "running"
    assert row["started_at"] is not None
    assert row["input_fingerprint"] == "fp1"

    store.update_analysis(aid, status="done", result={"score": 9})
    row = store.get_analysis(aid)
    assert row["status"] == "done"
    assert row["result"] == {"score": 9}
    assert row["completed_at"] is not None


def test_update_unknown_analysis_raises(store):
    with pytest.raises(KeyError):
        store.update_analysis("missing", status="done")


def test_find_cached_analysis(store):
    aid = store.create_analysis("attestation", "USDC")
    store.update_analysis(aid, status="done", input_fingerprint="fp-x",
                          result={"ok": True})
    hit = store.find_cached_analysis("attestation", "USDC", "fp-x")
    assert hit is not None and hit["id"] == aid
    # wrong fingerprint / pending rows are not cache hits
    assert store.find_cached_analysis("attestation", "USDC", "other") is None
    pending = store.create_analysis("attestation", "DAI")
    store.update_analysis(pending, input_fingerprint="fp-y")
    assert store.find_cached_analysis("attestation", "DAI", "fp-y") is None


def test_list_analyses_filters_and_limit(store):
    for sym in ("USDC", "USDT", "USDC"):
        store.create_analysis("attestation", sym)
    store.create_analysis("sanctions", "USDC")
    assert len(store.list_analyses()) == 4
    assert len(store.list_analyses(surface="attestation")) == 3
    assert len(store.list_analyses(symbol="USDC")) == 3
    assert len(store.list_analyses(surface="attestation", symbol="USDC")) == 2
    assert len(store.list_analyses(limit=2)) == 2


# ── sources ───────────────────────────────────────────────────────────
def test_list_sources(store):
    rows = store.list_sources()
    assert {r["id"] for r in rows} == {"genius-act", "mica-title-iii"}
    genius = next(r for r in rows if r["id"] == "genius-act")
    assert genius["status"] == "proposed"
    assert genius["url"] == "https://example.gov/genius"


def test_list_sources_missing_file(tmp_path):
    s = FileStore(sources_path=tmp_path / "absent.yaml")
    assert s.list_sources() == []


# ── corpus ────────────────────────────────────────────────────────────
def test_save_and_get_passages(store):
    passages = [
        {"heading": "Article 1", "text": "First passage."},
        {"heading": "Article 2", "text": "Second passage.", "page": 4},
    ]
    store.save_passages("mica-title-iii", passages)
    rows = store.get_passages("mica-title-iii")
    assert len(rows) == 2
    assert rows[0]["ordinal"] == 0
    assert rows[1]["ordinal"] == 1
    # citation synthesised from source + section when absent
    assert rows[0]["citation"] == "mica-title-iii — Article 1"
    assert rows[0]["section"] == "Article 1"
    assert rows[1]["page"] == 4


def test_get_passages_reads_existing_chunk_json(store, tmp_path):
    """The ingest.Chunk on-disk shape is read transparently."""
    chunk_json = [
        {"source_id": "genius-act", "section": "(intro)", "heading": "(intro)",
         "text": "Body text.", "page": None}
    ]
    (tmp_path / "data" / "genius-act.json").write_text(json.dumps(chunk_json))
    rows = store.get_passages("genius-act")
    assert len(rows) == 1
    assert rows[0]["text"] == "Body text."
    assert rows[0]["citation"] == "genius-act — (intro)"


def test_get_all_passages(store):
    store.save_passages("genius-act", [{"heading": "H", "text": "a"}])
    store.save_passages("mica-title-iii", [{"heading": "H", "text": "b"}])
    rows = store.get_passages()
    assert {r["source_id"] for r in rows} == {"genius-act", "mica-title-iii"}


def test_get_passages_unknown_source(store):
    assert store.get_passages("nope") == []


# ── curation ──────────────────────────────────────────────────────────
def test_source_decision_latest_wins(store):
    store.record_source_decision("genius-act", "rejected")
    store.record_source_decision("genius-act", "approved")
    store.record_source_decision("mica-title-iii", "approved")
    overrides = store.source_status_overrides()
    assert overrides == {"genius-act": "approved", "mica-title-iii": "approved"}


def test_source_decision_persists_to_votes_yaml(store):
    store.record_source_decision("genius-act", "approved", user_id="u9")
    data = yaml.safe_load(store._votes_path.read_text())
    entry = data["source_decisions"][0]
    assert entry["id"] == "genius-act"
    assert entry["decision"] == "approved"
    assert entry["at"] == str(date.today())
    assert entry["decided_by"] == "u9"


def test_invalid_source_decision_raises(store):
    with pytest.raises(ValueError):
        store.record_source_decision("genius-act", "maybe")


def test_address_decision_latest_wins(store):
    store.record_address_decision("USDC", "ethereum", "0xabc", "rejected")
    store.record_address_decision("USDC", "ethereum", "0xabc", "verified")
    store.record_address_decision("USDT", "tron", "0xdef", "rejected")
    overrides = store.address_verified_overrides()
    assert overrides == {"USDC:ethereum": True, "USDT:tron": False}


def test_invalid_address_decision_raises(store):
    with pytest.raises(ValueError):
        store.record_address_decision("USDC", "ethereum", "0xabc", "ok")


def test_overrides_empty_when_no_votes(store):
    assert store.source_status_overrides() == {}
    assert store.address_verified_overrides() == {}


# ── monitor ───────────────────────────────────────────────────────────
def test_save_and_list_snapshots(store):
    store.save_snapshot(
        symbol="USDC", total_supply=100.0, native_supply=90.0,
        bridged_supply=10.0, per_chain=[{"chain": "ethereum"}], warnings=[],
    )
    store.save_snapshot(
        symbol="USDC", total_supply=110.0, native_supply=100.0,
        bridged_supply=10.0, per_chain=[], warnings=["drift"],
    )
    store.save_snapshot(
        symbol="USDT", total_supply=5.0, native_supply=5.0,
        bridged_supply=0.0, per_chain=[], warnings=[],
    )
    usdc = store.list_snapshots("USDC")
    assert len(usdc) == 2
    # newest first
    assert usdc[0]["total_supply"] == 110.0
    assert usdc[0]["warnings"] == ["drift"]
    assert len(store.list_snapshots("USDT")) == 1
    assert store.list_snapshots("DAI") == []


def test_list_snapshots_limit(store):
    for _ in range(5):
        store.save_snapshot(
            symbol="USDC", total_supply=1.0, native_supply=1.0,
            bridged_supply=0.0, per_chain=[], warnings=[],
        )
    assert len(store.list_snapshots("USDC", limit=3)) == 3
