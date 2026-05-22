"""LLM client interface.

The agent and the attestation extractor depend ONLY on this protocol,
never on a concrete SDK. That keeps them testable with FakeLLM and lets
the provider swap without touching business logic.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 2048
    ) -> str:
        """Return a free-text completion."""
        ...

    def extract_json(
        self, *, system: str, prompt: str, schema: dict, max_tokens: int = 2048
    ) -> dict:
        """Return a structured object conforming to `schema`."""
        ...
