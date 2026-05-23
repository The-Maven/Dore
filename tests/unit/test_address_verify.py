"""Address auto-verification. Hermetic — uses the fake_rpc pattern.

Each test wires `requests.post` to return whatever shape (EVM / Solana /
Tron) the case needs, then asserts the verifier's behaviour. The tests
exercise the full ladder of `signal` values: on-chain-symbol (match),
mismatch, the zero-supply guard, unsupported-chain, error.
"""
from __future__ import annotations

from sca import auto_verify, config, votes
from sca.tools import address_verify as av
from sca.tools.address_verify import verify_address, verify_all


# ── helpers ──────────────────────────────────────────────────────────
_FIFTY_M = 50_000_000


def _abi_encode_string(s: str) -> str:
    """Encode `s` as the ABI return value for an ERC-20 `symbol()` call."""
    data = s.encode("utf-8")
    length = len(data)
    # 32-byte offset (=0x20) + 32-byte length + payload, padded to 32.
    offset_hex = (32).to_bytes(32, "big").hex()
    length_hex = length.to_bytes(32, "big").hex()
    pad = (-length) % 32
    data_hex = (data + b"\x00" * pad).hex()
    return "0x" + offset_hex + length_hex + data_hex


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _evm_responder(symbol: str, decimals: int, total_supply: int):
    """Build a `requests.post` stub that answers EVM symbol/decimals/supply
    AND the Solana getAccountInfo/getTokenSupply pair (so verify_all
    tests can exercise both chain kinds with one stub)."""
    sym_hex = _abi_encode_string(symbol)
    dec_hex = hex(decimals)
    sup_hex = hex(total_supply)

    def fake_post(url, json=None, timeout=None, headers=None):  # noqa: A002
        body = json or {}
        method = body.get("method")
        # Solana SPL verification path
        if method == "getAccountInfo":
            return _FakeResp({"result": {"value": {
                "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {
                    "type": "mint",
                    "info": {"decimals": decimals,
                             "supply": str(total_supply)},
                }, "program": "spl-token"},
                "lamports": 1, "executable": False, "rentEpoch": 0,
            }}})
        if method == "getTokenSupply":
            return _FakeResp({"result": {"value": {
                "amount": str(total_supply), "decimals": decimals,
            }}})
        # EVM symbol() / decimals() / totalSupply()
        if method != "eth_call":
            return _FakeResp({"result": "0x"})
        data = body["params"][0]["data"]
        if data == av.SEL_SYMBOL:
            return _FakeResp({"result": sym_hex})
        if data == av.SEL_DECIMALS:
            return _FakeResp({"result": dec_hex})
        if data.startswith("0x18160ddd"):  # totalSupply
            return _FakeResp({"result": sup_hex})
        return _FakeResp({"result": "0x"})

    return fake_post


# ── verify_address ───────────────────────────────────────────────────
def test_clean_match_auto_verifies(monkeypatch):
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 6, _FIFTY_M * 10**6),
    )
    # Use a real registry address to keep the chain config wiring honest.
    out = verify_address("ethereum",
                         "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                         "USDC", 6)
    assert out["verified"] is True
    assert out["signal"] == "on-chain-symbol"
    assert out["on_chain_symbol"] == "USDC"
    assert out["on_chain_decimals"] == 6


def test_symbol_match_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("usdc", 6, _FIFTY_M * 10**6),
    )
    out = verify_address("ethereum", "0x" + "00" * 20, "USDC", 6)
    assert out["verified"] is True


def test_symbol_mismatch_flagged(monkeypatch):
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("FAKE", 6, _FIFTY_M * 10**6),
    )
    out = verify_address("ethereum", "0x" + "00" * 20, "USDC", 6)
    assert out["verified"] is False
    assert out["signal"] == "mismatch"
    assert "FAKE" in out["detail"]


def test_decimals_mismatch_flagged(monkeypatch):
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 18, _FIFTY_M * 10**6),
    )
    out = verify_address("ethereum", "0x" + "00" * 20, "USDC", 6)
    assert out["verified"] is False
    assert out["signal"] == "mismatch"
    assert "18" in out["detail"]


def test_zero_supply_blocks_auto_verify(monkeypatch):
    # Symbol + decimals match, but the contract is empty — strong enough
    # to leave for a human even with a clean metadata read.
    monkeypatch.setattr(av.requests, "post", _evm_responder("USDC", 6, 0))
    out = verify_address("ethereum", "0x" + "00" * 20, "USDC", 6)
    assert out["verified"] is False
    assert out["signal"] == "mismatch"
    assert "totalSupply" in out["detail"]


def test_solana_spl_triplet_auto_verifies(monkeypatch):
    """The SPL-Token-Program-ownership + decimals + non-zero-supply
    triplet is enough proof to auto-verify a Solana mint, even without
    the on-chain symbol (which lives in a Metaplex PDA we don't read)."""
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 6, _FIFTY_M * 10**6),
    )
    out = verify_address("solana", "any-mint-address", "USDC", 6)
    assert out["verified"] is True
    assert out["signal"] == "spl-mint-verified"
    assert "SPL Token Program" in out["detail"]


def test_solana_wrong_owner_does_not_verify(monkeypatch):
    """If the account isn't owned by the SPL Token Program, refuse."""
    def fake_post(url, json=None, timeout=None, headers=None):  # noqa: A002
        if (json or {}).get("method") == "getAccountInfo":
            return _FakeResp({"result": {"value": {
                "owner": "SomeOtherProgram111111111111111111111111111",
                "data": {"parsed": {"type": "mint", "info": {"decimals": 6}}},
                "lamports": 1, "executable": False, "rentEpoch": 0,
            }}})
        return _FakeResp({"result": {"value": {"amount": "1", "decimals": 6}}})

    monkeypatch.setattr(av.requests, "post", fake_post)
    out = verify_address("solana", "any-mint-address", "USDC", 6)
    assert out["verified"] is False
    assert out["signal"] == "error"


def test_solana_zero_supply_does_not_verify(monkeypatch):
    """A correctly-classed mint with zero supply is suspicious — leave it."""
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 6, 0),
    )
    out = verify_address("solana", "any-mint-address", "USDC", 6)
    assert out["verified"] is False


def test_solana_decimals_mismatch_flags(monkeypatch):
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 9, _FIFTY_M * 10**9),
    )
    out = verify_address("solana", "any-mint-address", "USDC",
                         expected_decimals=6)
    assert out["verified"] is False
    assert out["signal"] == "mismatch"


def test_rpc_error_does_not_auto_verify(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(av.requests, "post", boom)
    out = verify_address("ethereum", "0x" + "00" * 20, "USDC", 6)
    assert out["verified"] is False
    assert out["signal"] == "error"


def test_unknown_chain_is_error():
    out = verify_address("dogechain", "0x00", "USDC", 6)
    assert out["verified"] is False
    assert out["signal"] == "error"


# ── verify_all + the cache + the curation overlay ────────────────────
def test_verify_all_writes_auto_ledger_and_flips_deployment(monkeypatch):
    # Every read across every chain (EVM/Solana/Tron) returns matching USDC.
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 6, _FIFTY_M * 10**6),
    )

    before = config.get_stablecoin("USDC")
    base_dep = next(d for d in before.deployments if d.chain == "base")
    assert base_dep.verified is False  # YAML ships it unverified

    summary = verify_all("USDC")
    assert summary["auto_verified"] >= 1

    after = config.get_stablecoin("USDC")
    base_after = next(d for d in after.deployments if d.chain == "base")
    assert base_after.verified is True
    assert base_after.verification_method == "auto: on-chain symbol match"

    # Solana now auto-verifies via the SPL structural triplet — its
    # verification_method label is distinct so a reviewer sees the proof
    # shape at a glance.
    sol_after = next(d for d in after.deployments if d.chain == "solana")
    assert sol_after.verified is True
    assert sol_after.verification_method == "auto: SPL mint + decimals match"


def test_human_decision_beats_auto(monkeypatch):
    """A human 'rejected' vote must not be overridden by a passing auto check."""
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 6, _FIFTY_M * 10**6),
    )
    votes.record_address_decision("USDC", "base", "rejected")
    config.stablecoins.cache_clear()

    summary = verify_all("USDC")
    # The check was skipped for that one — the human had spoken.
    assert summary["skipped_human"] >= 1

    coin = config.get_stablecoin("USDC")
    base_dep = next(d for d in coin.deployments if d.chain == "base")
    assert base_dep.verified is False


def test_mismatch_clears_prior_auto_entry(monkeypatch):
    # First pass: clean match — auto entry recorded.
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("USDC", 6, _FIFTY_M * 10**6),
    )
    verify_all("USDC")
    coin = config.get_stablecoin("USDC")
    base_dep = next(d for d in coin.deployments if d.chain == "base")
    assert base_dep.verified is True

    # Second pass: the symbol now reads as something different — the
    # cached auto-verification must be invalidated so the loud flag returns.
    monkeypatch.setattr(
        av.requests, "post", _evm_responder("FAKE", 6, _FIFTY_M * 10**6),
    )
    verify_all("USDC")
    coin = config.get_stablecoin("USDC")
    base_dep = next(d for d in coin.deployments if d.chain == "base")
    assert base_dep.verified is False
    assert ("USDC", "base") not in auto_verify.auto_verified_overrides()
