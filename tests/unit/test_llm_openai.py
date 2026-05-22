"""OpenAI-compatible (DeepSeek) client — mocked HTTP, no network."""
import pytest

import sca.llm.openai_compatible as mod
from sca.llm.base import LLMClient
from sca.llm.openai_compatible import OpenAICompatibleLLM


class _Resp:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


def test_complete_sends_low_temperature(monkeypatch):
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["body"] = json
        captured["url"] = url
        return _Resp("hello")

    monkeypatch.setattr(mod.requests, "post", fake_post)
    llm = OpenAICompatibleLLM(api_key="k", model="m")
    assert llm.complete(system="s", prompt="p") == "hello"
    assert captured["body"]["model"] == "m"
    assert captured["body"]["temperature"] == 0.2
    assert captured["url"].endswith("/chat/completions")


def test_extract_json_uses_json_mode_and_zero_temp(monkeypatch):
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        captured["body"] = json
        return _Resp('{"a": 1}')

    monkeypatch.setattr(mod.requests, "post", fake_post)
    llm = OpenAICompatibleLLM(api_key="k")
    assert llm.extract_json(system="s", prompt="p", schema={"a": 0}) == {"a": 1}
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["temperature"] == 0.0


def test_extract_json_strips_code_fence(monkeypatch):
    monkeypatch.setattr(
        mod.requests,
        "post",
        lambda *a, **k: _Resp('```json\n{"a": 2}\n```'),
    )
    llm = OpenAICompatibleLLM(api_key="k")
    assert llm.extract_json(system="s", prompt="p", schema={}) == {"a": 2}


def test_requires_api_key():
    with pytest.raises(RuntimeError):
        OpenAICompatibleLLM(api_key="")


def test_satisfies_protocol():
    assert isinstance(OpenAICompatibleLLM(api_key="k"), LLMClient)
