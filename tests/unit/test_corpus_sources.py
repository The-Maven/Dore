from sca.corpus.sources import Source, included_sources


def test_source_included_by_default():
    # Opt-out model: a source is included unless explicitly excluded.
    assert Source("i", "t", "primary", "included").included
    assert Source("i", "t", "primary").included  # status defaults to included
    assert not Source("i", "t", "primary", "excluded").included
    assert Source("i", "t", "primary", "excluded").excluded


def test_included_sources_filters_out_excluded(monkeypatch):
    fake = (
        Source("a", "A", "primary", "included"),
        Source("b", "B", "primary", "excluded"),
        Source("c", "C", "standard", "included"),
    )
    monkeypatch.setattr("sca.corpus.sources.all_sources", lambda: fake)
    got = included_sources()
    assert [s.id for s in got] == ["a", "c"]


def test_shipped_registry_is_included_by_default():
    # The opt-out default: every registered source ships citable.
    from sca.corpus.sources import all_sources

    sources = all_sources()
    assert sources
    assert all(s.included for s in sources)


def test_verified_is_a_signal_not_a_gate():
    # A source can be both included and human-verified — verification is a
    # badge, never what decides whether the source is used.
    verified = Source("v", "V", "primary", "included", verified=True)
    assert verified.included and verified.verified
    unverified = Source("u", "U", "primary", "included", verified=False)
    assert unverified.included and not unverified.verified
