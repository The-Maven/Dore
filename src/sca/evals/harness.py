"""Eval harness: run analyze() per case, grade structured checks, report.

Grading is deterministic — each `expects` entry carries a `kind` checked by
a function here. That makes the harness a real regression guard: break the
curation gate, the supply tool, or gap reporting, and evals fail.

OPERATOR INTERFACE (annotated for later): natural-language expectations
graded by an LLM are not implemented. When the operator-defined eval set
grows, add an `llm_grader` kind and wire it. The structured `kind` checks
below already cover the regression-critical behaviour.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from sca import config
from sca.agent import analyze
from sca.corpus.sources import included_sources
from sca.models import Analysis, EvalCaseResult, EvalPointResult

_BANNED_ADVICE = ("you should buy", "you should sell", "safe to hold",
                  "price target", "good investment")
_NARRATIVE_KINDS = {"narrative_present", "no_investment_language"}


def _check(kind: str, spec: dict, analysis: Analysis) -> tuple[bool, str]:
    if kind == "supply_resolved":
        return analysis.supply.total_supply > 0, (
            f"total_supply={analysis.supply.total_supply:,.2f}"
        )
    if kind == "metrics_present":
        return analysis.metrics is not None, ""
    if kind == "metrics_absent":
        return analysis.metrics is None, ""
    if kind == "gap_contains":
        needle = str(spec.get("value", "")).lower()
        ok = any(needle in g.message.lower() for g in analysis.gaps)
        return ok, f"needle={needle!r}"
    if kind == "narrative_present":
        return bool(analysis.narrative.strip()), ""
    if kind == "no_investment_language":
        low = analysis.narrative.lower()
        hits = [w for w in _BANNED_ADVICE if w in low]
        return not hits, f"hits={hits}"
    if kind == "passages_included_only":
        included = {s.id for s in included_sources()}
        bad = [p.source_id for p in analysis.passages
               if p.source_id not in included]
        return not bad, f"excluded_cited={bad}"
    if kind == "attestation_confidence_min":
        if analysis.attestation is None:
            return False, "no attestation"
        threshold = float(spec.get("value", 0.6))
        return analysis.attestation.confidence >= threshold, ""
    raise ValueError(f"unknown eval check kind: {kind!r}")


def grade_case(case: dict) -> EvalCaseResult:
    """Run one eval case end-to-end and grade every expected point."""
    need_narrative = any(
        e["kind"] in _NARRATIVE_KINDS for e in case["expects"]
    )
    analysis = analyze(case["symbol"], synthesize_narrative=need_narrative)
    points: list[EvalPointResult] = []
    for exp in case["expects"]:
        kind = exp["kind"]
        passed, detail = _check(kind, exp, analysis)
        label = kind + (f"({exp['value']})" if "value" in exp else "")
        points.append(EvalPointResult(point=label, passed=passed, detail=detail))
    return EvalCaseResult(
        case_id=case["id"], symbol=case["symbol"], points=points
    )


def load_cases(path: Path | None = None) -> list[dict]:
    path = path or (config.EVALS_DIR / "cases.yaml")
    return yaml.safe_load(path.read_text())["cases"]


def run_evals(path: Path | None = None) -> list[EvalCaseResult]:
    """Grade every case. Returns one EvalCaseResult per case."""
    return [grade_case(c) for c in load_cases(path)]
