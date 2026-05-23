"""LLM augmentation for the REDEMPTION surface — fills the context block
when the deterministic liquidity-tier classification couldn't run.
Bounded: never states numeric reserves / coverage / redemption limits;
describes only the MECHANISM (fiat T+0/T+1 + KYC, or on-chain
burn-for-collateral)."""
from __future__ import annotations

from sca import augment
from sca.config import Stablecoin
from sca.llm.fake import FakeLLM


def _coin(symbol: str = "USDC", backing: str = "fiat_reserves") -> Stablecoin:
    return Stablecoin(
        symbol=symbol,
        name="Test " + symbol,
        issuer="Test Issuer",
        deployments=(),
        transparency_url="https://example.test/transparency",
        protocol_url="https://example.test/protocol",
        backing_model=backing,
    )


def test_augment_redemption_returns_augmented_context_shape():
    fake = FakeLLM(json_responses=[{
        "text": "Holders redeem through the issuer's primary-market "
                "account; settlement is typically T+0 or T+1.",
        "citations": ["https://example.test/redemption"],
    }])
    ctx = augment.augment_redemption_gap(
        _coin(), reason="no-attestation-no-tier-breakdown", llm=fake,
    )
    assert ctx is not None
    assert ctx.surface == "redemption"
    assert ctx.text  # non-empty body
    assert ctx.citations
    assert ctx.confidence in ("training-data-only", "web-searched")


def test_augment_redemption_prompt_mentions_key_fields_fiat():
    """For a fiat-backed token, the prompt must steer the LLM toward
    the issuer-redemption-process flavour (T+0/T+1, KYC, primary-market)."""
    coin = _coin(symbol="USDC", backing="fiat_reserves")
    system, user = augment._redemption_prompt(
        coin, reason="no-attestation-no-tier-breakdown",
    )
    assert "REDEMPTION" in system or "redemption" in system
    assert "NEVER" in system  # strict-rules block
    assert "USDC" in user
    assert "Test Issuer" in user
    assert "fiat_reserves" in user
    assert "no-attestation-no-tier-breakdown" in user
    # Fiat-backed flavour — explicitly requested in the user prompt.
    assert "FIAT" in user or "fiat" in user


def test_augment_redemption_prompt_steers_crypto_to_on_chain():
    """For a crypto-collateralized token, the prompt must steer toward
    the on-chain burn-for-collateral flavour — there is no fiat tiering
    to describe."""
    coin = _coin(symbol="DAI", backing="crypto_collateral")
    _system, user = augment._redemption_prompt(
        coin, reason="backing-model-crypto_collateral-no-fiat-tiering",
    )
    assert "DAI" in user
    assert "crypto_collateral" in user
    assert "ON-CHAIN" in user or "on-chain" in user


def test_augment_redemption_no_llm_returns_none(monkeypatch):
    monkeypatch.setattr(augment, "fallback_llm", lambda: None)
    assert augment.augment_redemption_gap(_coin(), reason="x") is None


def test_augment_redemption_swallows_llm_exception():
    class Boom:
        def extract_json(self, **_kw):
            raise RuntimeError("simulated LLM outage")

    assert augment.augment_redemption_gap(
        _coin(), reason="x", llm=Boom(),
    ) is None
