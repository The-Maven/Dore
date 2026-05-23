"""LLM abstraction — see base.LLMClient.

`get_llm()` returns a real client when LLM_API_KEY is set (DeepSeek by
default), and the offline FakeLLM otherwise, so the pipeline always runs.
"""
from __future__ import annotations

import os

from sca import config as _config  # noqa: F401 - triggers .env loading
from .base import LLMClient
from .fake import FakeLLM


class LLMNotConfigured(RuntimeError):
    """Raised when `get_llm()` is called in production with no API key.

    Production never silently falls back to FakeLLM — the user must see a
    clear error so the misconfiguration is fixed instead of producing
    fake-looking output that gets cited as real. Tests construct
    FakeLLM directly via `FakeLLM(...)`.
    """


def get_llm(tier: str = "fast") -> LLMClient:
    """Real client only — raises LLMNotConfigured if no API key is set.

    `tier` selects the model:
      - 'fast' (default): LLM_MODEL — quick + cheap, every call uses this
      - 'deep': LLM_MODEL_DEEP — slower reasoning model the UI opts into

    Provider selected by LLM_PROVIDER (default: deepseek). DeepSeek and any
    OpenAI-compatible endpoint go through OpenAICompatibleLLM.

    NO silent FakeLLM fallback in production — a missing key produces a
    visible error, never fake output dressed as real.
    """
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise LLMNotConfigured(
            "LLM_API_KEY is not set. Doré never falls back to a fake LLM in "
            "production — fake output dressed as real would be the worst "
            "possible kind of bug for a compliance tool. Configure "
            "LLM_API_KEY in .env (DeepSeek by default; set LLM_PROVIDER + "
            "LLM_BASE_URL for an alternate vendor)."
        )

    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    if provider == "anthropic":
        from .anthropic_client import AnthropicLLM

        return AnthropicLLM()

    from .openai_compatible import (
        DEEPSEEK_BASE_URL,
        DEFAULT_MODEL,
        OpenAICompatibleLLM,
    )

    fast = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    deep = os.environ.get("LLM_MODEL_DEEP", fast)  # fall back to fast if unset
    chosen = deep if tier == "deep" else fast
    # Deep-tier reasoning needs more headroom: default 120s vs 45s for fast.
    # Override per-tier via LLM_TIMEOUT_S / LLM_TIMEOUT_DEEP_S in .env.
    timeout = float(os.environ.get(
        "LLM_TIMEOUT_DEEP_S" if tier == "deep" else "LLM_TIMEOUT_S",
        "120" if tier == "deep" else "45",
    ))
    return OpenAICompatibleLLM(
        api_key=key,
        base_url=os.environ.get("LLM_BASE_URL", DEEPSEEK_BASE_URL),
        model=chosen,
        timeout=timeout,
    )


__all__ = ["LLMClient", "FakeLLM", "LLMNotConfigured", "get_llm"]
