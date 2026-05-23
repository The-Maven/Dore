"""LLM augmentation for the SANCTIONS surface — fills the context block
when the deterministic OFAC SDN screen couldn't run cleanly. The prompt
is bounded: never claims a token / address is on or off the SDN list,
never invents addresses, only describes the SDN list, what the operator
can do, and the regulatory regime."""
from __future__ import annotations

from sca import augment
from sca.config import Stablecoin
from sca.llm.fake import FakeLLM


def _coin(symbol: str = "USDP", backing: str = "fiat_reserves") -> Stablecoin:
    """A minimal Stablecoin for prompt-shape tests — no real registry needed."""
    return Stablecoin(
        symbol=symbol,
        name="Test " + symbol,
        issuer="Test Issuer",
        deployments=(),
        transparency_url="https://example.test/transparency",
        protocol_url="https://example.test/protocol",
        backing_model=backing,
    )


def test_augment_sanctions_returns_augmented_context_shape():
    """A scripted FakeLLM response flows through into an AugmentedContext."""
    fake = FakeLLM(json_responses=[{
        "text": "The OFAC SDN list is published by Treasury at sdn.xml; "
                "an operator can run `sca refresh` to re-fetch.",
        "citations": ["https://www.treasury.gov/ofac/downloads/sdn.xml"],
    }])
    ctx = augment.augment_sanctions_gap(
        _coin(), reason="sdn-list-unavailable: HTTP 500", llm=fake,
    )
    assert ctx is not None
    assert ctx.surface == "sanctions"
    assert "OFAC" in ctx.text or "Treasury" in ctx.text or "SDN" in ctx.text
    assert ctx.citations  # FakeLLM-scripted citation flows through
    # confidence label always set; defaults to training-data-only since
    # the web-search flag is off in tests.
    assert ctx.confidence in ("training-data-only", "web-searched")


def test_augment_sanctions_prompt_mentions_key_fields():
    """The prompt MUST give the LLM the registry-level context it needs:
    issuer, backing model, the reason the screen failed, and a strong
    instruction not to invent sanctions claims."""
    coin = _coin(symbol="USDP", backing="fiat_reserves")
    system, user = augment._sanctions_prompt(
        coin, reason="sdn-list-critically-stale: 14 days old",
    )
    # System prompt — the hard rules around what the LLM is allowed to do.
    assert "SANCTIONS" in system or "sanctions" in system
    assert "NEVER" in system  # strict-rules block
    assert "SDN" in system
    # User prompt — the per-token context the LLM needs to be useful.
    assert "USDP" in user
    assert "Test Issuer" in user
    assert "fiat_reserves" in user
    assert "sdn-list-critically-stale" in user


def test_augment_sanctions_no_llm_returns_none(monkeypatch):
    """When no LLM client is available, augmentation is a no-op — never
    raises, never blocks the deterministic screen."""
    monkeypatch.setattr(augment, "fallback_llm", lambda: None)
    assert augment.augment_sanctions_gap(_coin(), reason="x") is None


def test_augment_sanctions_swallows_llm_exception():
    """Best-effort: any LLM exception drops the augmentation, never
    propagates. The sanctions surface must keep functioning."""

    class Boom:
        def extract_json(self, **_kw):
            raise RuntimeError("simulated LLM outage")

    assert augment.augment_sanctions_gap(
        _coin(), reason="x", llm=Boom(),
    ) is None
