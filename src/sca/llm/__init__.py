"""LLM abstraction — see base.LLMClient.

`get_llm()` returns a real client when LLM_API_KEY is set (DeepSeek by
default), and the offline FakeLLM otherwise, so the pipeline always runs.
"""
from __future__ import annotations

import os

from sca import config as _config  # noqa: F401 - triggers .env loading
from .base import LLMClient
from .fake import FakeLLM


def get_llm() -> LLMClient:
    """Real client if LLM_API_KEY is set, else the offline FakeLLM.

    Provider selected by LLM_PROVIDER (default: deepseek). DeepSeek and any
    OpenAI-compatible endpoint go through OpenAICompatibleLLM.
    """
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return FakeLLM()

    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()
    if provider == "anthropic":
        from .anthropic_client import AnthropicLLM

        return AnthropicLLM()

    from .openai_compatible import (
        DEEPSEEK_BASE_URL,
        DEFAULT_MODEL,
        OpenAICompatibleLLM,
    )

    return OpenAICompatibleLLM(
        api_key=key,
        base_url=os.environ.get("LLM_BASE_URL", DEEPSEEK_BASE_URL),
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
    )


__all__ = ["LLMClient", "FakeLLM", "get_llm"]
