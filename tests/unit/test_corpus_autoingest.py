"""Auto-ingest of `corpus/staging/<source-id>.md`.

The product invariant: dropping a markdown file into staging is enough.
The next time the app or a CLI runs, retrieve() picks it up — no manual
`ingest_source` step. Tests follow the hermetic pattern in conftest.py:
no real files are touched; STAGING_DIR / INGEST_STATE_PATH are tmp dirs.
"""
from __future__ import annotations

import importlib
import json

import pytest

# `sca.corpus.retrieve` resolves to the re-exported function via __init__.py;
# load the submodule explicitly so we can reset its once-per-process flag.
_retrieve_mod = importlib.import_module("sca.corpus.retrieve")

from sca.corpus import ingest as _ingest_mod
from sca.corpus.ingest import sync_staging
from sca.corpus.retrieve import retrieve
from sca.corpus.sources import Source


SAMPLE = """# Reserve Composition
Reserves must consist of high-quality liquid assets such as Treasury bills.

# Redemption
Holders may redeem tokens at par within one business day.
"""


def _stage(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def single_included_source(monkeypatch):
    """A single registered + included source named 'test-reg'."""
    src = Source("test-reg", "Test Regulation", "primary", "included")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (src,))
    return src


def test_dropping_a_staged_file_makes_passages_retrievable(
    single_included_source, tmp_path
):
    # No manual ingest_source: drop the file and call retrieve().
    _stage(_ingest_mod.STAGING_DIR / "test-reg.md", SAMPLE)
    passages = retrieve("redeem at par business day")
    assert passages, "auto-ingest did not produce passages on first retrieve()"
    assert passages[0].source_id == "test-reg"
    assert passages[0].heading == "Redemption"


def test_auto_ingest_runs_at_most_once_per_process(
    single_included_source, tmp_path, monkeypatch
):
    _stage(_ingest_mod.STAGING_DIR / "test-reg.md", SAMPLE)
    calls: list[int] = []
    real_sync = _ingest_mod.sync_staging

    def counting_sync():
        calls.append(1)
        return real_sync()

    monkeypatch.setattr(_ingest_mod, "sync_staging", counting_sync)
    retrieve("anything")
    retrieve("anything else")
    retrieve("a third query")
    assert sum(calls) == 1


def test_staging_readme_is_silently_skipped(
    single_included_source, tmp_path
):
    # README.md has no matching source id; auto-ingest must not raise.
    _stage(_ingest_mod.STAGING_DIR / "README.md", "# Not a source")
    _stage(_ingest_mod.STAGING_DIR / "test-reg.md", SAMPLE)
    ingested = sync_staging()
    assert ingested == ["test-reg"]


def test_modifying_a_staged_file_is_picked_up_on_next_process(
    single_included_source, tmp_path
):
    staged = _ingest_mod.STAGING_DIR / "test-reg.md"
    _stage(staged, SAMPLE)
    first = sync_staging()
    assert first == ["test-reg"]
    # Same content, same process: idempotent no-op.
    assert sync_staging() == []
    # Edit the file. A new process (simulated by reset_auto_ingest) re-ingests.
    _stage(staged, SAMPLE + "\n# Audit\nAn annual audit is required.\n")
    _retrieve_mod.reset_auto_ingest()
    second = sync_staging()
    assert second == ["test-reg"]
    # The new heading is now retrievable.
    passages = retrieve("annual audit")
    assert any(p.heading == "Audit" for p in passages)


def test_excluded_source_is_skipped(monkeypatch, tmp_path):
    # A human has opted this source out; staged text must NOT be ingested.
    excluded = Source("gone", "Excluded Source", "primary", "excluded")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (excluded,))
    _stage(_ingest_mod.STAGING_DIR / "gone.md", SAMPLE)
    ingested = sync_staging()
    assert ingested == []
    # And of course retrieve() returns nothing for an excluded source.
    assert retrieve("reserve composition") == []


def test_sync_staging_records_hash_in_state_file(
    single_included_source, tmp_path
):
    _stage(_ingest_mod.STAGING_DIR / "test-reg.md", SAMPLE)
    sync_staging()
    state = json.loads(_ingest_mod.INGEST_STATE_PATH.read_text())
    assert "test-reg" in state
    assert len(state["test-reg"]) == 64  # sha256 hex digest


def test_retrieve_survives_a_failing_auto_ingest(
    single_included_source, tmp_path, monkeypatch
):
    # An auto-ingest failure must NOT break retrieve() — it must still
    # return whatever passages already exist.
    def boom():
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(_ingest_mod, "sync_staging", boom)
    # No passages staged, but retrieve() must not raise.
    assert retrieve("anything") == []
