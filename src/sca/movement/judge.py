"""LLM judge — turn deterministic numbers into investor-grade insight.

LAYER: judgement, NOT facts. The judge is allowed to interpret but
NEVER to invent. Every number it cites must come from the forecast
struct it was given; every driver it names must come from the
attribution candidates that were already validated by attribute.py.
Recent calibration metrics (Brier vs baselines) provide the
self-awareness — the judge tempers its own confidence based on how
the model has actually been performing.

The output is structured: synthesis (one paragraph), insight (what it
means for an investor), pitch (one concrete idea or 'no action').
Three fields so the UI can render each in its own panel and so the
calibration page can later score the judge's pitches separately from
the engine's predictions.

Strict guardrails:
  - No em-dashes (voice rule).
  - Banned future-tense pitching: 'we will', 'we expect to', 'we are
    going to'. Use the present + the cone.
  - Banned weasel words: 'may', 'could', 'might', 'potentially'. The
    cone IS the uncertainty — using both is double-counting.
  - If the calibration record is bad (Brier > baseline), the judge
    MUST acknowledge it in the synthesis.
  - A pitch may be 'no action'. That is honest and acceptable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sca.observability import log_event


# Bump on any change to the prompt or schema so the predictions archive
# carries an audit trail of which judge version wrote what.
JUDGE_MODEL_VERSION = "judge_v1"

# Voice rules — applied post-hoc to LLM output. Cheap, defense in depth.
_BANNED_WORDS = re.compile(
    r"\b(may|could|might|potentially|possibly|perhaps)\b",
    re.IGNORECASE,
)
_BANNED_FUTURE = re.compile(
    r"\b(we will|we expect|we are going to|we'll)\b",
    re.IGNORECASE,
)


@dataclass
class JudgeOutput:
    synthesis: Optional[str]
    insight: Optional[str]
    pitch: Optional[str]
    model: str = JUDGE_MODEL_VERSION


# ── prompt ───────────────────────────────────────────────────────────
_JUDGE_SYSTEM = """\
You are the judge layer of a stablecoin forecasting product. You \
read a deterministic forecast (numbers + bands), a cited driver list \
(verified candidates from our corpus + observability events), a \
WEB CONTEXT block (live Brave search — UNVERIFIED leads), and the \
model's recent calibration record. You produce three short outputs.

Output schema — return EXACTLY this JSON, no preamble:
{
  "synthesis": "one paragraph (≤ 80 words) restating the forecast in \
plain English. Cite numbers verbatim. Name uncertainty with the \
phrase 'inside the X% band' instead of weasel words.",
  "insight": "one sentence (≤ 30 words) on what this means for a \
sophisticated investor or compliance team. No future-tense pitching.",
  "pitch": "one sentence (≤ 25 words) describing ONE concrete \
consideration the operator should weigh. Acceptable values include: \
a watch action, an exposure check, a reconcile step, or the literal \
string 'no action'."
}

Strict rules:
1. Every number you cite must be in the forecast. Never invent one.
2. Verified drivers — cite by index in square brackets, e.g. [0]. \
Treat as authoritative.
3. Web context — cite by web index e.g. (W2). These are LEADS, not \
facts: write 'reports cite' or 'public commentary notes' (W1), not \
'X happened'. Do not assert causation from web context alone — pair \
with a verified driver where possible.
4. NO em-dashes. NO 'may', 'could', 'might', 'potentially', \
'possibly', 'perhaps'. NO 'we will', 'we expect', 'we are going to'.
5. If the calibration record shows Brier worse than the climatology \
baseline (0.25), say so in the synthesis: 'our calibration on this \
target trails the 50/50 baseline so treat the call with caution'.
6. If verified candidates AND web context are both empty, the \
synthesis must say 'no driver cited' and the pitch should default \
to 'no action'.
"""


def compose(forecast_summary: dict,
             attribution_sentence: str,
             attribution_candidates: list[dict],
             calibration_record: dict,
             *,
             web_context: list[dict] | None = None,
             llm_client=None) -> JudgeOutput:
    """Run the judge on a single forecast. `forecast_summary` is a
    dict carrying the bare math the LLM is allowed to cite; the
    enforcement happens via the prompt + post-validation.

    `web_context` is the optional Brave search context (list of
    {title, snippet, url, trust_tier} dicts). Empty / None is the
    honest default — the prompt explicitly marks web items as
    unverified leads so the LLM cites them accordingly."""
    if llm_client is None:
        try:
            from sca.llm import get_llm
            llm_client = get_llm(tier="fast")
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.judge.llm_unavailable", level="info",
                error_class=type(exc).__name__,
            )
            # Honest empty output — UI falls back to engine prose.
            return JudgeOutput(synthesis=None, insight=None, pitch=None)

    prompt = _build_prompt(
        forecast_summary, attribution_sentence,
        attribution_candidates, calibration_record,
        web_context or [],
    )
    try:
        raw = llm_client.complete(
            system=_JUDGE_SYSTEM, prompt=prompt, max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.judge.llm_failed", level="warn",
            error_class=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        return JudgeOutput(synthesis=None, insight=None, pitch=None)

    parsed = _parse_json_loose(raw)
    if parsed is None:
        log_event("movement.judge.parse_failed", level="warn",
                  raw_head=str(raw)[:200])
        return JudgeOutput(synthesis=None, insight=None, pitch=None)

    return JudgeOutput(
        synthesis=_clean(parsed.get("synthesis")),
        insight=_clean(parsed.get("insight")),
        pitch=_clean(parsed.get("pitch")),
    )


# ── helpers ──────────────────────────────────────────────────────────
def _build_prompt(forecast_summary: dict, attribution_sentence: str,
                    attribution_candidates: list[dict],
                    calibration_record: dict,
                    web_context: list[dict]) -> str:
    lines: list[str] = []
    lines.append("FORECAST (cite these numbers verbatim):")
    for k, v in forecast_summary.items():
        if v is None:
            continue
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(f"ATTRIBUTION (the model's own one-sentence read): "
                 f"{attribution_sentence}")
    lines.append("")
    lines.append("VERIFIED CANDIDATES (drivers — cite by [index]):")
    if attribution_candidates:
        for i, c in enumerate(attribution_candidates[:6]):
            lines.append(f"  [{i}] {c.get('summary', '')} "
                         f"(trust: {c.get('trust_tier', '')})")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("WEB CONTEXT (Brave search — UNVERIFIED leads, "
                 "cite as (Wn)):")
    if web_context:
        for i, w in enumerate(web_context[:5]):
            domain = w.get("domain", "") or "?"
            tier = w.get("trust_tier", "web_unverified")
            title = (w.get("title") or "").strip()
            snippet = (w.get("snippet") or "").strip()
            lines.append(f"  (W{i}) [{tier} · {domain}] {title}")
            if snippet:
                lines.append(f"        {snippet}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("CALIBRATION (recent track record for this target):")
    if calibration_record and calibration_record.get("count", 0) > 0:
        cb = calibration_record
        lines.append(f"  resolved_count: {cb.get('count')}")
        if cb.get("brier_mean") is not None:
            lines.append(f"  brier_mean: {cb['brier_mean']:.3f}")
        if cb.get("crps_mean") is not None:
            lines.append(f"  crps_mean: {cb['crps_mean']:.3f}")
        if cb.get("baseline_climatology_brier_mean") is not None:
            lines.append("  climatology_brier_baseline: "
                         f"{cb['baseline_climatology_brier_mean']:.3f}")
    else:
        lines.append("  (no resolutions yet — be explicit that the "
                     "track record is empty so the call carries less "
                     "weight)")
    lines.append("")
    lines.append("Return the JSON object now.")
    return "\n".join(lines)


def _parse_json_loose(raw: str) -> Optional[dict]:
    """Tolerant JSON extraction: handles fenced code blocks, trailing
    text, and the occasional 'here is the JSON:' preamble. Returns
    None if no parseable object is found."""
    import json
    s = (raw or "").strip()
    if not s:
        return None
    # Strip fenced code block if present.
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    # Find the outermost { ... }.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = s[start:end + 1]
    try:
        out = json.loads(candidate)
        if isinstance(out, dict):
            return out
        return None
    except json.JSONDecodeError:
        return None


def _clean(text) -> Optional[str]:
    """Apply voice rules + length cap. Returns None on empty input.
    Banned words are stripped (a clean delete is safer than the LLM
    going back to retry — we keep the rest of the sentence)."""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    # Voice rule: no em-dashes.
    s = s.replace("—", ", ").replace("--", ", ")
    # Drop banned modal verbs in place. We keep the rest of the
    # sentence intact rather than re-prompting; the calibration
    # archive shows whether the judge needed correction.
    s = _BANNED_WORDS.sub("", s)
    s = _BANNED_FUTURE.sub("the call indicates", s)
    # Squeeze double spaces left by the strips.
    s = re.sub(r"\s+", " ", s).strip()
    return s or None
