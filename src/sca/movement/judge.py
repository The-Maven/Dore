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
JUDGE_MODEL_VERSION = "judge_v2"

# Voice rules — applied post-hoc to LLM output. Cheap, defense in depth.
# Expanded after audit P3: the original list was trivially bypassable
# by adjacent weasels (anticipate, project, presumably, etc.).
_BANNED_WORDS = re.compile(
    r"\b(may|could|might|potentially|possibly|perhaps|"
    r"presumably|arguably|plausibly|tentatively|"
    r"appears to|seems to|tends to|looks like)\b",
    re.IGNORECASE,
)
_BANNED_FUTURE = re.compile(
    r"\b(we will|we expect|we are going to|we'll|"
    r"we anticipate|we forecast|we project|"
    r"doré expects|the engine will|the model will)\b",
    re.IGNORECASE,
)

# Strict length caps per field — caps that the system prompt asks for
# but the LLM frequently exceeds. Enforced post-hoc.
_MAX_WORDS = {"synthesis": 80, "insight": 30, "pitch": 25}

# Citation patterns in the LLM output. [n] for verified candidates,
# (Wn) for web context. Validated against the lists we actually
# passed in, so a forged [99] or (W42) gets stripped.
_CITE_VERIFIED_RE = re.compile(r"\[(\d+)\]")
_CITE_WEB_RE = re.compile(r"\(W(\d+)\)")


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

    # Validate citations FIRST so any forged [n]/(Wn) is stripped
    # before length-capping (so the cap counts real text, not
    # placeholder citations). Then voice-rule + length-cap each
    # field with its tag so the audit log can attribute strips.
    syn = _validate_citations(parsed.get("synthesis") or "",
                                attribution_candidates,
                                web_context or [])
    ins = _validate_citations(parsed.get("insight") or "",
                                attribution_candidates,
                                web_context or [])
    pit = _validate_citations(parsed.get("pitch") or "",
                                attribution_candidates,
                                web_context or [])
    return JudgeOutput(
        synthesis=_clean(syn, field="synthesis"),
        insight=_clean(ins, field="insight"),
        pitch=_clean(pit, field="pitch"),
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
            # Age annotation (audit #13): the judge sees how stale
            # each result is so it can hedge — week-old context
            # cited next to a fresh forecast deserves a different
            # voice than a fresh news hit.
            age_s = w.get("fetched_age_s")
            age_tag = ""
            if isinstance(age_s, (int, float)):
                if age_s < 600:
                    age_tag = " · fresh"
                elif age_s < 7200:
                    age_tag = f" · {int(age_s/60)}m old"
                elif age_s < 86400:
                    age_tag = f" · {int(age_s/3600)}h old"
                else:
                    age_tag = f" · {int(age_s/86400)}d old"
            lines.append(f"  (W{i}) [{tier} · {domain}{age_tag}] {title}")
            if snippet:
                lines.append(f"        {snippet}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("CALIBRATION (recent track record for this target):")
    if calibration_record and calibration_record.get("unavailable"):
        # Audit #15: distinguish 'no resolutions yet' from 'backend
        # unavailable' — the LLM must hedge differently in each case.
        lines.append("  (calibration backend unavailable — name this "
                     "in the synthesis as 'track record could not be "
                     "read this cycle' and add extra hedging to the "
                     "pitch)")
    elif calibration_record and calibration_record.get("count", 0) > 0:
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
        lines.append("  (no resolutions yet, be explicit that the "
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


def _clean(text, *, field: str | None = None) -> Optional[str]:
    """Apply voice rules + length cap + unicode normalisation.
    Returns None on empty input.

    Discipline applied (audit P3):
      - NFKC normalisation so zero-width-space / cyrillic-lookalike
        bypasses against the banned-word regex are normalised first.
      - Banned-word and banned-future patterns are STRIPPED but every
        strip event is logged so a curator can see which rows got
        edited. Repeated strips on the same row push the audit notes.
      - Length cap per field (synthesis 80w / insight 30w / pitch 25w).
        Excess words are dropped with an ellipsis so the archive shows
        what we kept.
    """
    if not text:
        return None
    # Unicode normalise BEFORE the regex passes so zero-width-space /
    # NFK-equivalent characters can't sneak past the word-boundary
    # patterns.
    import unicodedata
    s = unicodedata.normalize("NFKC", str(text)).strip()
    # Drop zero-width spaces and similar formatting characters that
    # NFKC preserves but that would still slice a banned word.
    s = re.sub(r"[​‌‍⁠﻿]", "", s)
    if not s:
        return None
    # Voice rule: no em-dashes.
    s = s.replace("—", ", ").replace("--", ", ")

    # Banned-word stripping — note every strip so the audit trail
    # records that the judge needed correction. Cap notes at 5 to
    # avoid log spam.
    stripped: list[str] = []
    def _record(match):
        stripped.append(match.group(0))
        return ""
    s = _BANNED_WORDS.sub(_record, s)
    s = _BANNED_FUTURE.sub("the call indicates", s)
    if stripped and field is not None:
        log_event(
            "movement.judge.voice_rule_triggered", level="info",
            field=field, stripped=stripped[:5],
            stripped_count=len(stripped),
        )

    # Squeeze double spaces from the strips.
    s = re.sub(r"\s+", " ", s).strip()

    # Length cap per field. Truncate cleanly at the limit + ellipsis
    # so the archive shows we cut, not that the LLM stopped early.
    if field and field in _MAX_WORDS:
        words = s.split()
        if len(words) > _MAX_WORDS[field]:
            s = " ".join(words[:_MAX_WORDS[field]]) + "…"
            log_event(
                "movement.judge.length_cap_triggered", level="info",
                field=field, kept_words=_MAX_WORDS[field],
                original_words=len(words),
            )

    return s or None


def _validate_citations(text: str,
                          verified_candidates: list[dict],
                          web_context: list[dict]) -> str:
    """Strip forged citations from the judge output.

    The LLM is allowed to cite by [n] (verified candidates) or (Wn)
    (web context). Citations outside the lists we passed in are
    HALLUCINATIONS and must be removed before the row hits the
    archive — otherwise an investor reading 'the OFAC announcement
    [4]' could be reading a citation to a candidate that doesn't
    exist. Forged citations also flag the row in the audit log so
    curators can see which model versions are inventing references.
    """
    if not text:
        return text
    valid_v = set(range(len(verified_candidates)))
    valid_w = set(range(len(web_context)))
    forged: list[str] = []

    def _strip_v(m):
        idx = int(m.group(1))
        if idx in valid_v:
            return m.group(0)
        forged.append(m.group(0))
        return ""

    def _strip_w(m):
        idx = int(m.group(1))
        if idx in valid_w:
            return m.group(0)
        forged.append(m.group(0))
        return ""

    cleaned = _CITE_VERIFIED_RE.sub(_strip_v, text)
    cleaned = _CITE_WEB_RE.sub(_strip_w, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if forged:
        log_event(
            "movement.judge.citation_forged", level="warn",
            forged=forged[:8],
            forged_count=len(forged),
            valid_verified_count=len(verified_candidates),
            valid_web_count=len(web_context),
        )
    return cleaned
