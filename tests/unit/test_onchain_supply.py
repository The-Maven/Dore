import pytest

from sca.tools.onchain_supply import get_onchain_supply


def test_verified_address_resolves_unverified_skipped(fake_rpc):
    # USDC: ethereum verified; base/arbitrum/optimism/polygon/solana unverified.
    res = get_onchain_supply("USDC", use_cache=False)
    assert res.total_supply == 50_000_000.0
    assert [c.chain for c in res.per_chain] == ["ethereum"]
    # User-friendly warning message: no CLI commands, plain financial copy.
    assert any("identity check" in w for w in res.warnings)


def test_unverified_included_but_flagged(fake_rpc):
    res = get_onchain_supply("USDC", allow_unverified=True, use_cache=False)
    # 6 deployments, 50M each (mock).
    assert res.total_supply == 300_000_000.0
    assert any("identity check" in w for w in res.warnings)
    # Critical: messages must NOT leak operator CLI commands to end users.
    for w in res.warnings:
        assert "sca verify" not in w
        assert "sca refresh" not in w
        assert "sca curate" not in w


def test_multichain_includes_solana(fake_rpc):
    res = get_onchain_supply("USDC", allow_unverified=True, use_cache=False)
    chains = {c.chain for c in res.per_chain}
    assert {"ethereum", "base", "solana"} <= chains


def test_tron_reader_works(fake_rpc):
    # USDT: ethereum (evm) + tron + the canonical issuer L1 deployments
    # (avalanche, polygon, solana). The point of this test is that the tron
    # reader works at all; we assert tron is present and the total is the
    # 50M-per-chain mock times the deployment count.
    res = get_onchain_supply("USDT", allow_unverified=True, use_cache=False)
    chains = {c.chain for c in res.per_chain}
    assert "tron" in chains
    assert res.total_supply == 50_000_000.0 * len(res.per_chain)


def test_supply_result_carries_provenance(fake_rpc):
    res = get_onchain_supply("USDC", allow_unverified=True, use_cache=False)
    # Config deployments are all native -> native == total, bridged == 0.
    assert res.native_supply == res.total_supply
    assert res.bridged_supply == 0.0
    assert res.read_at  # ISO read timestamp captured
    assert all(c.kind == "native" for c in res.per_chain)


def test_unknown_symbol_raises():
    with pytest.raises(ValueError):
        get_onchain_supply("NOPE", use_cache=False)


def test_rpc_failure_is_reported_not_raised(monkeypatch):
    from sca.tools import onchain_supply as mod

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(mod.requests, "post", boom)
    res = get_onchain_supply("USDT", use_cache=False)
    assert res.total_supply == 0.0
    assert any("network down" in w for w in res.warnings)
