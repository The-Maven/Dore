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


def test_clean_strips_zero_width_space_bypass():
    """Audit P3.1: a zero-width-space inside 'may' must not let it
    through the banned-word regex."""
    s = "USDC m y overshoot the cone."
    out = _clean(s, field="synthesis")
    assert "may" not in out.lower()
    # Also the ZWS itself shouldn't be in the output
    assert " " not in out and "​" not in out


def test_clean_strips_expanded_weasels():
    """Audit P3.2: 'anticipates', 'arguably', 'presumably', 'appears
    to', 'we anticipate' must all be caught — original list was too
    narrow."""
    s = ("USDC arguably overshoots; presumably the band widens; the "
         "engine appears to anticipate divergence; we anticipate "
         "movement.")
    out = _clean(s, field="synthesis")
    low = out.lower()
    assert "arguably" not in low
    assert "presumably" not in low
    assert "appears to" not in low
    assert "we anticipate" not in low


def test_clean_enforces_length_cap_per_field():
    """Audit P3.4: the 80/30/25 word caps were unenforced; an LLM
    that returns 400 words shipped verbatim. Now the cap truncates
    with an ellipsis so the audit trail shows we cut."""
    long_synthesis = " ".join(["word"] * 200)
    out = _clean(long_synthesis, field="synthesis")
    word_count = len(out.replace("…", "").split())
    assert word_count <= 80


def test_validate_citations_strips_forged_indices():
    """Audit P3.5: judge cites [99] when only 2 verified candidates
    exist. The validator must strip the forged citation."""
    from sca.movement.judge import _validate_citations
    text = "OFAC [4] confirmed (W42) imminent; corroborated by [0]."
    out = _validate_citations(text,
                                verified_candidates=[{"k": 0}, {"k": 1}],
                                web_context=[])
    assert "[4]" not in out
    assert "(W42)" not in out
    assert "[0]" in out  # this one was valid


def test_compose_dropping_a_hostile_response():
    """End-to-end hostile scenario from the audit P3 thought experiment:
    LLM returns synthesis citing forged drivers and weasels. The
    compose pipeline must strip forged citations and banned weasel
    words; the row may still ship but the audit log records every
    edit."""
    raw = ('{"synthesis": "Reports cite an OFAC announcement [4] '
           'arguably coinciding with a 12bp move (W3) presumably.", '
           '"insight": "We anticipate widening.", '
           '"pitch": "could check"}')
    out = compose(
        _example_forecast(),
        "No driver cited.",
        attribution_candidates=[{"summary": "real candidate 0"}],
        calibration_record={"count": 0},
        web_context=[],
        llm_client=_FakeLLM(raw),
    )
    # Forged citations stripped (only [0] would be valid here)
    assert "[4]" not in (out.synthesis or "")
    assert "(W3)" not in (out.synthesis or "")
    # Banned weasels stripped
    syn_low = (out.synthesis or "").lower()
    assert "arguably" not in syn_low
    assert "presumably" not in syn_low
    # Future-tense pitching stripped from insight
    assert "we anticipate" not in (out.insight or "").lower()
    # Modal verbs stripped from pitch
    assert "could" not in (out.pitch or "").lower()


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
