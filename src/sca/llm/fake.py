"""Deterministic in-process LLM for tests and offline runs.

No network, no key. Returns scripted (or schema-shaped default) output so
the whole pipeline can be exercised in CI without an API key.
"""
from __future__ import annotations

from typing import Any


class FakeLLM:
    """Scripted LLM. Pass `completions` / `json_responses` to control output."""

    def __init__(
        self,
        *,
        completions: list[str] | None = None,
        json_responses: list[dict] | None = None,
    ) -> None:
        self._completions = list(completions or [])
        self._json = list(json_responses or [])
        self.calls: list[tuple[str, str, str]] = []

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 2048
    ) -> str:
        self.calls.append(("complete", system, prompt))
        if self._completions:
            return self._completions.pop(0)
        return "[fake-llm] no scripted completion"

    def extract_json(
        self, *, system: str, prompt: str, schema: dict, max_tokens: int = 2048
    ) -> dict[str, Any]:
        self.calls.append(("extract_json", system, prompt))
        if self._json:
            return self._json.pop(0)
        return {key: None for key in schema}
