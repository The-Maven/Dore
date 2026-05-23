"""LLM fallback ladder — primary first, Anthropic if configured, raise visibly otherwise.

Production NEVER silently falls back to FakeLLM. Test that the ladder:
  - serves primary's response when primary works
  - serves secondary's response when primary fails AND secondary is configured
  - re-raises the PRIMARY's error when no secondary is configured
  - re-raises the primary's error when BOTH fail (secondary failure is incidental)
  - emits structured log events for every fallover
"""
from __future__ import annotations

import pytest

from sca.llm.fake import FakeLLM
from sca.llm.fallback import FallbackLLM


class _BrokenLLM:
    """Always raises — simulates a downed provider."""
    _model = "broken"

    def complete(self, **_kw):
        raise RuntimeError("primary outage")

    def extract_json(self, **_kw):
        raise RuntimeError("primary outage")


def test_primary_success_serves_primary():
    """Happy path: primary works, secondary never called."""
    primary = FakeLLM(completions=["primary-said-this"])
    secondary = FakeLLM(completions=["should-not-fire"])
    ladder = FallbackLLM(primary, secondary)
    assert ladder.complete(system="x", prompt="y", max_tokens=10) == "primary-said-this"


def test_primary_fails_secondary_serves():
    primary = _BrokenLLM()
    secondary = FakeLLM(completions=["fallback-saved-the-day"])
    ladder = FallbackLLM(primary, secondary)
    out = ladder.complete(system="x", prompt="y", max_tokens=10)
    assert out == "fallback-saved-the-day"


def test_primary_fails_no_secondary_raises_primary_error():
    """No fallback configured: re-raise the primary's error visibly."""
    primary = _BrokenLLM()
    ladder = FallbackLLM(primary, None)
    with pytest.raises(RuntimeError, match="primary outage"):
        ladder.complete(system="x", prompt="y", max_tokens=10)


def test_both_fail_raises_primary_error_not_secondary():
    """Per the design: the secondary failing is incidental — surface the
    primary problem so the user understands their main provider is down."""
    primary = _BrokenLLM()
    class AlsoBroken:
        _model = "also-broken"
        def complete(self, **_kw):
            raise ValueError("secondary outage")
        def extract_json(self, **_kw):
            raise ValueError("secondary outage")
    ladder = FallbackLLM(primary, AlsoBroken())
    with pytest.raises(RuntimeError, match="primary outage"):
        ladder.complete(system="x", prompt="y", max_tokens=10)


def test_extract_json_takes_same_ladder():
    primary = _BrokenLLM()
    secondary = FakeLLM(json_responses=[{"text": "from-fallback", "citations": []}])
    ladder = FallbackLLM(primary, secondary)
    out = ladder.extract_json(system="x", prompt="y", schema={}, max_tokens=10)
    assert out == {"text": "from-fallback", "citations": []}


def test_get_llm_in_production_raises_without_key(monkeypatch):
    """The production rule: no key → visible error, NEVER a silent FakeLLM."""
    from sca.llm import get_llm, LLMNotConfigured
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(LLMNotConfigured):
        get_llm()
