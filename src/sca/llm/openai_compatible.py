"""OpenAI-compatible LLM client — DeepSeek and any compatible endpoint.

Used for bounded jobs only: structured extraction and analysis synthesis.
Genuinely agentic tool-calling lives in the hermes-agent deployment, not here.

Zero extra dependencies — talks the chat-completions API over `requests`.
"""
from __future__ import annotations

import json

import requests

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEEPSEEK_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise RuntimeError("api_key is required")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def _chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> str:
        body: dict = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = requests.post(
            f"{self._base}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def complete(
        self, *, system: str, prompt: str, max_tokens: int = 2048
    ) -> str:
        # Low temperature: compliance synthesis must be consistent, not creative.
        return self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )

    def extract_json(
        self, *, system: str, prompt: str, schema: dict, max_tokens: int = 2048
    ) -> dict:
        instruction = (
            f"{prompt}\n\nReturn ONLY a JSON object matching this schema:\n"
            f"{json.dumps(schema, indent=2)}"
        )
        raw = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": instruction},
            ],
            max_tokens=max_tokens,
            json_mode=True,
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
