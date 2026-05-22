from sca.corpus.sources import Source, approved_sources


def test_source_approved_property():
    assert Source("i", "t", "primary", "approved").approved
    assert not Source("i", "t", "primary", "proposed").approved


def test_approved_sources_filters_to_approved_only(monkeypatch):
    fake = (
        Source("a", "A", "primary", "approved"),
        Source("b", "B", "primary", "proposed"),
        Source("c", "C", "standard", "approved"),
    )
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: fake)
    got = approved_sources()
    assert [s.id for s in got] == ["a", "c"]


def test_shipped_registry_has_no_approved_sources():
    # The curation gate: nothing ships pre-approved. A human approves later.
    from sca.corpus.sources import all_sources

    assert all(not s.approved for s in all_sources())
