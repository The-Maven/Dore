"""Anthropic-backed LLM client.

REQUIRES an API key (env LLM_API_KEY) and the `anthropic` package
(`pip install '.[llm]'`). Not exercised by the test suite — FakeLLM
covers those paths offline.
"""
from __future__ import annotations

import json
import os

DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicLLM:
    def __init__(
        self, *, model: str = DEFAULT_MODEL, api_key: str | None = None
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "anthropic package not installed — run: pip install '.[llm]'"
            ) from exc
        key = api_key or os.environ.get("LLM_API_KEY")
        if not key:  # pragma: no cover - env-dependent
            raise RuntimeError("LLM_API_KEY not set — see .env.example")
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 2048
    ) -> str:  # pragma: no cover - network
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")

    def extract_json(
        self, *, system: str, prompt: str, schema: dict, max_tokens: int = 2048
    ) -> dict:  # pragma: no cover - network
        instruction = (
            f"{prompt}\n\nReturn ONLY a JSON object matching this schema:\n"
            f"{json.dumps(schema, indent=2)}"
        )
        raw = self.complete(
            system=system, prompt=instruction, max_tokens=max_tokens
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
