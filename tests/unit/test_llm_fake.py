from sca.llm import FakeLLM
from sca.llm.base import LLMClient


def test_scripted_completion():
    fake = FakeLLM(completions=["hello"])
    assert fake.complete(system="s", prompt="p") == "hello"
    assert fake.calls[0][0] == "complete"


def test_default_json_matches_schema_keys():
    fake = FakeLLM()
    out = fake.extract_json(system="s", prompt="p", schema={"a": 1, "b": 2})
    assert set(out) == {"a", "b"}


def test_scripted_json_response():
    fake = FakeLLM(json_responses=[{"x": 1}])
    assert fake.extract_json(system="s", prompt="p", schema={}) == {"x": 1}


def test_fake_satisfies_protocol():
    assert isinstance(FakeLLM(), LLMClient)
