"""Deterministic synthesis fallback — user must never see 'No narrative synthesised'.

When the LLM call throws, returns empty, or is unavailable, synthesis
composes a real paragraph from the typed result. Honest, never invents."""
from __future__ import annotations

from datetime import date

from sca.agent.synthesis import (
    _deterministic_attestation_narrative,
    _deterministic_surface_narrative,
    synthesize,
)
from sca.llm.fake import FakeLLM
from sca.models import (
    Attestation, ChainSupply, CorpusPassage, Metrics, ReserveLine, SupplyResult,
)


def _supply(complete: bool = True) -> SupplyResult:
    return SupplyResult(
        symbol="USDC",
        total_supply=50_000_000_000,
        per_chain=[
            ChainSupply(chain="ethereum", contract="0xA0", raw=32e18, decimals=6,
                        supply=32_000_000_000, kind="native", verified=True,
                        consensus="2/2 agree", endpoint="https://ethereum-rpc.publicnode.com"),
            ChainSupply(chain="base", contract="0x83", raw=18e18, decimals=6,
                        supply=18_000_000_000, kind="native", verified=True,
                        consensus="1/2 single source", endpoint="https://base-rpc.publicnode.com"),
        ],
        warnings=[],
        native_supply=50_000_000_000, bridged_supply=0,
        read_at="2026-05-23T00:00:00+00:00",
        complete=complete, chains_expected=2, chains_read=2 if complete else 1,
        failed_chains=[] if complete else ["base"],
    )


def _att() -> Attestation:
    return Attestation(
        symbol="USDC", as_of_date=str(date(2026, 4, 30)),
        total_reserves=50_500_000_000, tokens_outstanding=50_400_000_000,
        breakdown=[ReserveLine(asset_class="cash", amount=5e9)],
        source_url="https://circle.com/x", confidence=0.92,
    )


def _metrics() -> Metrics:
    return Metrics(
        attested_coverage=1.002, live_coverage=1.01,
        staleness_days=10, supply_drift=-0.008,
        provenance="1 issuer attestation · 2/2 chains read · 1 cross-RPC corroborated",
    )


# ── deterministic attestation narrative ──
def test_deterministic_attestation_has_supply_lineage():
    n = _deterministic_attestation_narrative("USDC", _supply(), _att(), _metrics(), [])
    assert "USDC" in n
    assert "$50.00B" in n
    assert "2 chains" in n
    assert "2026-04-30" in n


def test_deterministic_attestation_handles_missing_attestation():
    n = _deterministic_attestation_narrative("PYUSD", _supply(), None, None, [])
    assert "No issuer attestation could be retrieved" in n
    assert "supply" in n.lower()
    # Never claims a coverage figure
    assert "100.00%" not in n


def test_deterministic_attestation_surfaces_partial_loud():
    n = _deterministic_attestation_narrative("USDC", _supply(complete=False), None, None, [])
    assert "PARTIAL" in n
    assert "base" in n  # failed chain named


def test_deterministic_attestation_includes_provenance():
    n = _deterministic_attestation_narrative("USDC", _supply(), _att(), _metrics(), [])
    assert "Provenance" in n
    assert "cross-RPC corroborated" in n


def test_deterministic_attestation_always_acknowledges_fallback():
    """A reader must always know they're seeing the deterministic fallback,
    not an LLM-generated synthesis — honesty over polish."""
    n = _deterministic_attestation_narrative("USDC", _supply(), _att(), _metrics(), [])
    assert "Auto-composed" in n or "deterministic" in n.lower()


# ── deterministic surface narrative (sanctions / redemption) ──
def test_deterministic_surface_includes_facts_block():
    facts = "symbol: USDC\n[tool:sanctions] OFAC SDN list published 05/21/2026"
    n = _deterministic_surface_narrative("sanctions-screen", facts, [])
    assert "Ofac Sanctions Screen" in n or "OFAC SDN list" in n
    assert "USDC" in n


def test_deterministic_surface_includes_passages_when_present():
    p = CorpusPassage(source_id="bis", section="1", heading="x", text="...",
                     citation="BIS-1", page=1)
    n = _deterministic_surface_narrative("redemption", "facts", [p])
    assert "[BIS-1]" in n


# ── synthesize() wraps LLM with fallback ──
class _ExplodingLLM:
    _model = "broken"
    def complete(self, **_kw):
        raise RuntimeError("synth provider down")
    def extract_json(self, **_kw):
        raise RuntimeError("synth provider down")


def test_synthesize_falls_back_when_llm_explodes():
    n = synthesize(
        symbol="USDC", supply=_supply(), attestation=_att(),
        metrics=_metrics(), passages=[], llm=_ExplodingLLM(),
    )
    assert n  # never empty
    assert "USDC" in n
    assert "Auto-composed" in n


def test_synthesize_falls_back_when_llm_returns_empty():
    """FakeLLM returns empty by default — synthesis should still produce a paragraph."""
    n = synthesize(
        symbol="USDC", supply=_supply(), attestation=_att(),
        metrics=_metrics(), passages=[], llm=FakeLLM(completions=[""]),
    )
    assert n  # never empty
    assert "USDC" in n
