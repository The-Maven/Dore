"""LLM fallback ladder — degrade gracefully when a provider is down.

The ladder, in order:
  1. Primary: whatever `get_llm(tier)` resolves to (DeepSeek by default).
  2. Secondary: Anthropic (Claude) if `ANTHROPIC_API_KEY` is set.
  3. Visible failure: raise the upstream error so the caller decides
     (synthesis can render facts-only; augmentation can drop the card).

This is intentionally NOT a silent FakeLLM fallback in production — a
fake answer dressed as real is the worst class of bug for a compliance
tool. The user-facing rule (from memory: dore-quality-audit-bar):
production raises visibly; tests construct FakeLLM directly.

The fallback wrapper is opt-in per call: callers pass `llm=fallback_llm()`
instead of `llm=get_llm()` when they want graceful degradation.
"""
from __future__ import annotations

import os
from typing import Any

from sca.llm.base import LLMClient
from sca.observability import log_event


class _NoSecondaryAvailable(RuntimeError):
    """Anthropic key not configured — only the primary is available."""


def _build_secondary() -> LLMClient | None:
    """Construct the Anthropic fallback if its key is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        from sca.llm.anthropic_client import AnthropicLLM
        return AnthropicLLM(api_key=os.environ["ANTHROPIC_API_KEY"])
    except Exception as exc:  # noqa: BLE001 - missing package, etc.
        log_event(
            "llm.fallback.unavailable", level="warn",
            error_class=type(exc).__name__, error_message=str(exc),
        )
        return None


class FallbackLLM:
    """A two-rung ladder: primary first, secondary if primary raises.

    Logs every fallover so a future ops dashboard sees them as signal.
    If both fail, the second exception propagates — caller chooses how
    to surface it (typically: synthesis renders facts-only with a gap).
    """

    def __init__(self, primary: LLMClient, secondary: LLMClient | None = None):
        self._primary = primary
        self._secondary = secondary or _build_secondary()
        # Expose model name so observability sees the actual primary in use
        self._model = getattr(primary, "_model", "unknown")

    def _try_call(self, method: str, **kwargs: Any) -> Any:
        try:
            return getattr(self._primary, method)(**kwargs)
        except Exception as primary_exc:  # noqa: BLE001 - fallback path
            log_event(
                "llm.primary.failed", level="warn",
                method=method,
                primary_model=getattr(self._primary, "_model", "unknown"),
                error_class=type(primary_exc).__name__,
                error_message=str(primary_exc),
                fallback_available=self._secondary is not None,
            )
            if self._secondary is None:
                raise
            try:
                result = getattr(self._secondary, method)(**kwargs)
                log_event(
                    "llm.fallback.served", level="warn",
                    method=method,
                    fallback_model=getattr(self._secondary, "_model", "unknown"),
                )
                return result
            except Exception as fallback_exc:
                log_event(
                    "llm.fallback.failed", level="error",
                    method=method,
                    error_class=type(fallback_exc).__name__,
                    error_message=str(fallback_exc),
                )
                # Surface the ORIGINAL exception — the secondary failing
                # is incidental to the user's primary problem.
                raise primary_exc

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 2048,
    ) -> str:
        return self._try_call(
            "complete", system=system, prompt=prompt, max_tokens=max_tokens,
        )

    def extract_json(
        self, *, system: str, prompt: str, schema: dict,
        max_tokens: int = 2048,
    ) -> dict:
        return self._try_call(
            "extract_json", system=system, prompt=prompt,
            schema=schema, max_tokens=max_tokens,
        )


def fallback_llm(tier: str = "fast") -> LLMClient:
    """Production entry point — primary with optional Anthropic fallback.

    Use this instead of `get_llm()` from callers that want graceful
    degradation on a provider outage (synthesis, augmentation).
    """
    from sca.llm import get_llm
    return FallbackLLM(get_llm(tier))
