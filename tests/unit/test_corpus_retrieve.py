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
def ingested_corpus(monkeypatch, tmp_path):
    # Opt-out model: an included source is retrievable by default.
    src = Source("test-reg", "Test Regulation", "primary", "included")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (src,))
    ingest_source("test-reg", SAMPLE, store=tmp_path)
    return tmp_path


def test_retrieve_finds_relevant_section(ingested_corpus):
    passages = retrieve("redeem at par business day", store=ingested_corpus)
    assert passages
    assert passages[0].heading == "Redemption"
    assert passages[0].citation.startswith("test-reg")


def test_retrieve_returns_passages_by_default(ingested_corpus):
    # The opt-out flip: passages come back without any human approval step.
    assert retrieve("reserve composition", store=ingested_corpus)


def test_retrieve_omits_excluded_source(monkeypatch, tmp_path):
    # A source a human has excluded is dropped from the citable set.
    src = Source("test-reg", "Test Regulation", "primary", "included")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (src,))
    ingest_source("test-reg", SAMPLE, store=tmp_path)
    assert retrieve("reserve composition", store=tmp_path)

    excluded = Source("test-reg", "Test Regulation", "primary", "excluded")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (excluded,))
    assert retrieve("reserve composition", store=tmp_path) == []


def test_retrieve_empty_without_sources(monkeypatch, tmp_path):
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: ())
    assert retrieve("anything", store=tmp_path) == []


def test_passage_carries_verified_signal(monkeypatch, tmp_path):
    src = Source("test-reg", "T", "primary", "included", verified=True)
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (src,))
    ingest_source("test-reg", SAMPLE, store=tmp_path)
    passages = retrieve("redeem at par", store=tmp_path)
    assert passages and passages[0].source_verified is True


def test_ingest_refuses_excluded_source(monkeypatch, tmp_path):
    src = Source("gone", "Excluded", "primary", "excluded")
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: (src,))
    with pytest.raises(PermissionError):
        ingest_source("gone", SAMPLE, store=tmp_path)
