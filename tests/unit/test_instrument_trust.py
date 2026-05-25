"""Instrument-Trust Oracle — verdict composition.

Pins the non-negotiables (build spec §5):
  1. LLM extracts, never invents — `unknown` when data is missing
  2. Deterministic 3-state output at the boundary
  3. Staleness degrades toward `unknown`
  4. Multi-chain supply summed across deployments
  5. Provenance on every cited field
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from sca import instrument_trust as it


class _FakeStore:
    """Test double for the store — controls what reserves / supply /
    peg-tick rows the verdict sees."""
    def __init__(self, *, reserves=None, as_of=None,
                  supply_by_chain=None, peg_dev=None, peg_kind="agreed",
                  extraction_conf=1.0):
        self.reserves = reserves
        self.as_of = as_of
        self.supply_by_chain = supply_by_chain or {}
        self.peg_dev = peg_dev
        self.peg_kind = peg_kind
        self.extraction_conf = extraction_conf

    def latest_verified_fact(self, claim_type, subject, **kw):
        if claim_type == "reserves" and self.reserves is not None:
            return {
                "value": {
                    "total_reserves_usd": self.reserves,
                    "as_of_date": self.as_of or "",
                    "extraction_confidence": self.extraction_conf,
                },
                "sources": [{"kind": "attestation",
                              "url": "https://example.com/attestation"}],
                "as_of": self.as_of or "",
            }
        if claim_type == "native_supply":
            # subject format: "{SYM}:{CHAIN}"
            _, _, chain = subject.partition(":")
            if chain in self.supply_by_chain:
                return {"value": {"supply": self.supply_by_chain[chain]}}
        return None

    def list_peg_ticks(self, sym, limit=1):
        if self.peg_dev is None:
            return []
        return [{"deviation_bps": self.peg_dev,
                  "consensus_kind": self.peg_kind}]


def _patch_store(monkeypatch, fake):
    monkeypatch.setattr("sca.store.get_store", lambda: fake)


def _yesterday(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)
             ).strftime("%Y-%m-%d")


# ── §5.3: deterministic 3-state at the boundary ─────────────────────
def test_state_is_one_of_three(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=1_000_000, as_of=_yesterday(5),
        supply_by_chain={"ethereum": 999_000}, peg_dev=0.3))
    v = it.compute_verdict("USDC")
    assert v.state in ("sound", "degraded", "unknown")


# ── §5.1: missing data → unknown, never inventing ───────────────────
def test_no_data_is_unknown(monkeypatch):
    _patch_store(monkeypatch, _FakeStore())
    v = it.compute_verdict("USDC")
    assert v.state == "unknown"
    assert "Cannot reconcile" in v.reasoning


def test_only_reserves_no_supply_is_unknown(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=1_000_000, as_of=_yesterday(5)))
    v = it.compute_verdict("USDC")
    assert v.state == "unknown"
    assert "on-chain supply" in v.reasoning.lower()


def test_only_supply_no_reserves_is_unknown(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        supply_by_chain={"ethereum": 1_000_000}, peg_dev=0.3))
    v = it.compute_verdict("USDC")
    assert v.state == "unknown"
    assert "attested reserves" in v.reasoning.lower()


# ── §5.4: staleness degrades toward unknown ─────────────────────────
def test_attestation_120_days_old_degrades_to_unknown(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=1_000_000, as_of=_yesterday(120),
        supply_by_chain={"ethereum": 999_000}, peg_dev=0.3))
    v = it.compute_verdict("USDC")
    assert v.state == "unknown"
    assert v.fresh is False
    assert "days old" in v.reasoning


# ── §5.5: multi-chain supply summed across all deployments ──────────
def test_supply_sums_across_chains(monkeypatch):
    # USDC's registered chains are ethereum/base/arbitrum/optimism/
    # polygon/solana. Pick three real ones to test the sum.
    _patch_store(monkeypatch, _FakeStore(
        reserves=10_000_000, as_of=_yesterday(5),
        supply_by_chain={"ethereum": 5_000_000,
                          "solana": 3_000_000,
                          "base": 1_950_000},
        peg_dev=0.3))
    v = it.compute_verdict("USDC")
    assert v.supply_on_chain_total == 9_950_000
    assert set(v.supply_by_chain.keys()) == {
        "ethereum", "solana", "base"}


# ── Sound verdict: reserves ≈ supply, peg tight, agreed sources ─────
def test_sound_verdict(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=10_000_000, as_of=_yesterday(7),
        supply_by_chain={"ethereum": 9_980_000},  # 20bp over-collat
        peg_dev=0.5, peg_kind="agreed"))
    v = it.compute_verdict("USDC")
    assert v.state == "sound"
    assert v.confidence > 0.85
    assert v.fresh is True


# ── Degraded: reserve delta within band but redemption not great ────
def test_degraded_when_peg_dislocated(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=10_000_000, as_of=_yesterday(7),
        supply_by_chain={"ethereum": 9_995_000},  # 5bp over-collat
        peg_dev=35.0, peg_kind="agreed"))  # 35bp dislocation
    v = it.compute_verdict("USDC")
    assert v.state == "degraded"
    assert v.redemption_path_status == "degraded"


# ── Disputed peg → unknown regardless of reserves ───────────────────
def test_disputed_consensus_is_unknown(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=10_000_000, as_of=_yesterday(7),
        supply_by_chain={"ethereum": 9_950_000},
        peg_dev=0.5, peg_kind="disputed"))
    v = it.compute_verdict("USDC")
    assert v.state == "unknown"
    assert "disputed" in v.reasoning.lower() \
        or "contested" in v.reasoning.lower()


# ── §5.5 provenance on every cited field ────────────────────────────
def test_every_cited_field_has_provenance(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=10_000_000, as_of=_yesterday(7),
        supply_by_chain={"ethereum": 9_950_000, "solana": 50_000},
        peg_dev=0.5, peg_kind="agreed"))
    v = it.compute_verdict("USDC")
    fields = {p.field for p in v.provenance}
    assert "reserves_attested_usd" in fields
    assert "supply_on_chain.ethereum" in fields
    assert "supply_on_chain.solana" in fields
    assert "peg_deviation_bps" in fields
    # No invented provenance for absent fields
    for p in v.provenance:
        assert p.source in ("attestation", "on_chain", "peg_tick")


# ── Reserve delta math is right ─────────────────────────────────────
def test_reserve_delta_bps_math(monkeypatch):
    # 1_010_000 reserves vs 1_000_000 supply → +100bp (1%) over-collat
    _patch_store(monkeypatch, _FakeStore(
        reserves=1_010_000, as_of=_yesterday(5),
        supply_by_chain={"ethereum": 1_000_000}, peg_dev=0.3))
    v = it.compute_verdict("USDC")
    assert abs(v.reserve_delta_bps - 100.0) < 0.5


def test_reserve_shortfall_is_negative_bps(monkeypatch):
    # Reserves UNDER supply → negative delta = shortfall
    _patch_store(monkeypatch, _FakeStore(
        reserves=990_000, as_of=_yesterday(5),
        supply_by_chain={"ethereum": 1_000_000}, peg_dev=0.3))
    v = it.compute_verdict("USDC")
    assert v.reserve_delta_bps < 0
    assert v.reserve_delta_bps < -50  # 100bp shortfall
    # 100bp shortfall is within DEGRADED band (≤ 250bp) → degraded
    assert v.state == "degraded"


# ── Boundary returns a dict an agent can consume ────────────────────
def test_as_dict_serialises_cleanly(monkeypatch):
    _patch_store(monkeypatch, _FakeStore(
        reserves=1_000_000, as_of=_yesterday(5),
        supply_by_chain={"ethereum": 999_000}, peg_dev=0.5))
    v = it.compute_verdict("USDC")
    d = v.as_dict()
    # Must be JSON-serialisable shape
    import json
    s = json.dumps(d)
    assert len(s) > 100
    # Top-level keys for the AP2 policy engine
    assert "state" in d and "confidence" in d
    assert "timestamp" in d and "fresh" in d
    assert "reserve_delta_bps" in d
    assert "redemption_path_status" in d
    assert "provenance" in d and isinstance(d["provenance"], list)


def test_empty_symbol_is_unknown():
    v = it.compute_verdict("")
    assert v.state == "unknown"
    assert v.confidence == 0.0
