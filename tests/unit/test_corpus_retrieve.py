import pytest

from sca.corpus.ingest import chunk_markdown, ingest_source
from sca.corpus.retrieve import retrieve
from sca.corpus.sources import Source

SAMPLE = """# Reserve Composition
Reserves must consist of high-quality liquid assets such as Treasury bills.

# Redemption
Holders may redeem tokens at par within one business day.
"""


def test_chunk_markdown_splits_by_heading():
    chunks = chunk_markdown("s", SAMPLE)
    headings = {c.heading for c in chunks}
    assert "Reserve Composition" in headings
    assert "Redemption" in headings


@pytest.fixture
def approved_corpus(monkeypatch, tmp_path):
    src = Source("test-reg", "Test Regulation", "primary", "approved")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (src,))
    ingest_source("test-reg", SAMPLE, store=tmp_path)
    return tmp_path


def test_retrieve_finds_relevant_section(approved_corpus):
    passages = retrieve("redeem at par business day", store=approved_corpus)
    assert passages
    assert passages[0].heading == "Redemption"
    assert passages[0].citation.startswith("test-reg")


def test_retrieve_empty_without_approved_sources(monkeypatch, tmp_path):
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: ())
    assert retrieve("anything", store=tmp_path) == []


def test_ingest_refuses_unapproved_source(monkeypatch, tmp_path):
    src = Source("prop", "Proposed", "primary", "proposed")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (src,))
    with pytest.raises(PermissionError):
        ingest_source("prop", SAMPLE, store=tmp_path)
