"""Judge tests — voice rules, JSON parsing, calibration self-awareness.

The judge must:
  - Strip banned weasel words.
  - Drop em-dashes (voice rule).
  - Acknowledge a bad calibration record in synthesis.
  - Return None fields when the LLM is offline so the UI knows to
    fall back to engine prose.
"""
from __future__ import annotations

from sca.movement.judge import (
    JUDGE_MODEL_VERSION,
    JudgeOutput,
    _clean,
    _parse_json_loose,
    compose,
)


class _FakeLLM:
    """In-memory LLM stub for hermetic tests. Returns a canned
    response; lets us drive the judge through specific scenarios."""

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, *, system: str, prompt: str,
                 max_tokens: int = 400) -> str:
        return self._response

    def extract_json(self, *, system, prompt, schema, max_tokens=2048):
        raise NotImplementedError("not used by judge")


def _example_forecast() -> dict:
    return {
        "symbol": "USDC", "kind": "peg_deviation",
        "horizon_minutes": 60, "point": 3.2,
        "p50_low": 1.5, "p50_high": 4.9,
        "p80_low": -0.4, "p80_high": 6.8,
        "p95_low": -2.6, "p95_high": 9.0,
        "prob_positive": None, "confidence_word": "likely",
        "engine_notes": "history=12 sigma=2.4bp",
    }


def test_clean_strips_em_dash_and_banned_words():
    s = "The forecast — which may shift — could potentially overshoot."
    out = _clean(s)
    assert "—" not in out
    assert "may" not in out.lower()
    assert "could" not in out.lower()
    assert "potentially" not in out.lower()


def test_clean_strips_future_tense():
    s = "We will see deviation widening; we are going to act."
    out = _clean(s)
    assert "we will" not in out.lower()
    assert "we are going to" not in out.lower()


def test_clean_returns_none_on_empty():
    assert _clean("") is None
    assert _clean(None) is None


def test_parse_json_loose_handles_fenced_block():
    raw = """```json
{"synthesis": "x", "insight": "y", "pitch": "z"}
```"""
    parsed = _parse_json_loose(raw)
    assert parsed == {"synthesis": "x", "insight": "y", "pitch": "z"}


def test_parse_json_loose_handles_preamble():
    raw = ('Here is the JSON object you requested:\n'
           '{"synthesis": "x", "insight": "y", "pitch": "z"}')
    parsed = _parse_json_loose(raw)
    assert parsed == {"synthesis": "x", "insight": "y", "pitch": "z"}


def test_parse_json_loose_returns_none_on_garbage():
    assert _parse_json_loose("nope no json here") is None
    assert _parse_json_loose("") is None


def test_compose_with_offline_llm_returns_empty():
    """No fake fallback: an unavailable LLM produces an honest empty
    JudgeOutput. The UI's job is to fall back to engine prose."""
    out = compose(
        _example_forecast(),
        "No driver cited.",
        attribution_candidates=[],
        calibration_record={"count": 0},
        llm_client=None,  # forces real get_llm which raises in tests
    )
    assert isinstance(out, JudgeOutput)
    assert out.synthesis is None
    assert out.insight is None
    assert out.pitch is None


def test_compose_with_fake_llm_returns_parsed_fields():
    raw = ('{"synthesis": "USDC sits inside the 50% band at +3.2bp.",'
           ' "insight": "Peg holds within stated cone.",'
           ' "pitch": "no action"}')
    out = compose(
        _example_forecast(),
        "No driver cited.",
        attribution_candidates=[],
        calibration_record={"count": 0},
        llm_client=_FakeLLM(raw),
    )
    assert out.synthesis is not None
    assert "3.2bp" in out.synthesis
    assert out.pitch == "no action"
    assert out.model == JUDGE_MODEL_VERSION


def test_compose_with_web_context_includes_web_section_in_prompt():
    """The Brave web context block must be marked as UNVERIFIED in
    the prompt — that's the discipline that prevents the LLM from
    treating search results as fact."""
    captured = {}

    class CapturingLLM(_FakeLLM):
        def complete(self, *, system, prompt, max_tokens=400):
            captured["prompt"] = prompt
            captured["system"] = system
            return ('{"synthesis": "x", "insight": "y", '
                    '"pitch": "no action"}')

    web_ctx = [
        {"title": "USDC peg holds amid regulatory clarity",
         "snippet": "The token traded inside its quoted band.",
         "url": "https://research-house.example/usdc-peg-holds",
         "trust_tier": "research_house", "domain": "research-house.example"},
    ]
    compose(
        _example_forecast(),
        "No driver cited.",
        attribution_candidates=[],
        calibration_record={"count": 0},
        web_context=web_ctx,
        llm_client=CapturingLLM(""),
    )
    assert "WEB CONTEXT" in captured["prompt"]
    assert "UNVERIFIED" in captured["prompt"]
    assert "(W0)" in captured["prompt"]
    assert "research-house.example" in captured["prompt"]


def test_compose_strips_banned_words_from_llm_output():
    raw = ('{"synthesis": "Peg may drift — perhaps lower.",'
           ' "insight": "We will see widening.",'
           ' "pitch": "could check"}')
    out = compose(
        _example_forecast(),
        "No driver cited.",
        attribution_candidates=[],
        calibration_record={"count": 5, "brier_mean": 0.18,
                              "baseline_climatology_brier_mean": 0.25},
        llm_client=_FakeLLM(raw),
    )
    assert "—" not in (out.synthesis or "")
    assert "may" not in (out.synthesis or "").lower()
    assert "perhaps" not in (out.synthesis or "").lower()
    assert "we will" not in (out.insight or "").lower()
    assert "could" not in (out.pitch or "").lower()
