"""Integration: the full analyze() pipeline, offline (mocked RPC + FakeLLM)."""
from sca.agent import analyze
from sca.llm import FakeLLM
from sca.models import Attestation


def test_analyze_with_supplied_attestation(fake_rpc):
    att = Attestation(
        symbol="USDC",
        as_of_date="2026-05-01",
        total_reserves=55_000_000,
        tokens_outstanding=50_000_000,
        confidence=0.9,
    )
    result = analyze(
        "USDC",
        attestation=att,
        allow_unverified=False,
        llm=FakeLLM(completions=["narrative ok"]),
    )
    assert result.supply.total_supply == 50_000_000.0
    assert result.metrics is not None
    assert result.metrics.attested_coverage == 1.1
    assert result.narrative == "narrative ok"
    assert result.attestation is att
    assert result.checks  # deterministic guardrails ran


def test_analyze_reports_gaps_without_attestation(fake_rpc):
    result = analyze("USDC", synthesize_narrative=False)
    assert result.attestation is None
    assert result.metrics is None
    assert any("attestation" in g.message.lower() for g in result.gaps)
    assert any(g.category == "corpus" for g in result.gaps)


def test_low_confidence_surfaces_as_failed_check(fake_rpc):
    att = Attestation(
        symbol="USDC",
        as_of_date="2026-05-01",
        total_reserves=55_000_000,
        tokens_outstanding=50_000_000,
        confidence=0.3,
    )
    result = analyze(
        "USDC", attestation=att, allow_unverified=False,
        synthesize_narrative=False,
    )
    conf = [c for c in result.checks if c.name == "extraction_confidence"]
    assert conf and not conf[0].passed


def test_critical_guardrail_failure_becomes_a_structured_gap(fake_rpc):
    # Negative reserves trip a critical guardrail -> escalated to a Gap.
    att = Attestation(
        symbol="USDC",
        as_of_date="2026-05-01",
        total_reserves=-1.0,
        tokens_outstanding=50_000_000,
        confidence=0.9,
    )
    result = analyze(
        "USDC", attestation=att, allow_unverified=False,
        synthesize_narrative=False,
    )
    assert any(
        g.severity == "critical" and g.category == "guardrail"
        for g in result.gaps
    )


def test_analysis_is_cached(fake_rpc):
    first = analyze("USDC", llm=FakeLLM(completions=["n"]))
    # Same inputs -> same fingerprint -> cache hit, NOT recomputed.
    second = analyze("USDC", llm=FakeLLM(completions=["would-differ"]))
    assert second is first


def test_refresh_bypasses_the_cache(fake_rpc):
    first = analyze("USDC", llm=FakeLLM(completions=["n"]))
    fresh = analyze("USDC", llm=FakeLLM(completions=["n"]), refresh=True)
    assert fresh is not first


def test_canonical_run_is_persisted_through_the_store(fake_rpc):
    """A canonical analyze() additively records a done row in the Store."""
    from sca.store import get_store

    store = get_store()
    before = len(store.list_analyses(symbol="USDC"))
    analyze("USDC", llm=FakeLLM(completions=["n"]), refresh=True,
            user_id="auditor-1")
    rows = store.list_analyses(symbol="USDC")
    assert len(rows) == before + 1
    latest = rows[0]
    assert latest["status"] == "done"
    assert latest["user_id"] == "auditor-1"
    assert latest["input_fingerprint"]
    assert latest["result"]["symbol"] == "USDC"


def test_persistence_is_behaviour_neutral(fake_rpc):
    """The returned Analysis is identical whether or not a user_id is given."""
    a = analyze("USDC", llm=FakeLLM(completions=["n"]), refresh=True)
    b = analyze("USDC", llm=FakeLLM(completions=["n"]), refresh=True,
                user_id="u2")
    assert a.symbol == b.symbol
    assert a.supply.total_supply == b.supply.total_supply
    assert [c.name for c in a.checks] == [c.name for c in b.checks]
