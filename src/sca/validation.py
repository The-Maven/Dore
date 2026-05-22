"""Deterministic guardrails over the system's critical data.

No LLM. These checks run on the highest-risk values — the LLM-extracted
attestation and the computed reconciliation metrics — and catch errors
(a misread digit, a 100x unit slip, an impossible coverage ratio) before
they reach the analysis. Every check is a structured Check; critical
failures are escalated to gaps by the orchestrator.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from sca.models import (
    Attestation,
    Check,
    CorpusPassage,
    Metrics,
    RedemptionAssessment,
    SanctionsScreen,
    SupplyResult,
)

# A reserve breakdown may legitimately carry rounding and net-timing lines.
_SUM_TOLERANCE = 0.02
# Outside this band a coverage ratio is almost certainly a data error
# (a unit slip, a misread figure) rather than a real reserve position.
_COVERAGE_FLOOR = 0.5
_COVERAGE_CEIL = 2.0
_MIN_CONFIDENCE = 0.6


def _check(name: str, passed: bool, fail_severity: str, detail: str) -> Check:
    return Check(name, passed, "info" if passed else fail_severity, detail)


def _validate_date(value: str) -> tuple[bool, str]:
    if not value:
        return False, "no as_of_date"
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return False, f"unparseable date: {value!r}"
    if parsed > date.today():
        return False, f"date is in the future: {value}"
    return True, value


def validate_attestation(att: Attestation) -> list[Check]:
    checks = [
        _check(
            "reserves_positive", att.total_reserves > 0, "critical",
            f"total_reserves={att.total_reserves:,.0f}",
        ),
        _check(
            "tokens_positive", att.tokens_outstanding > 0, "critical",
            f"tokens_outstanding={att.tokens_outstanding:,.0f}",
        ),
    ]

    if att.breakdown and att.total_reserves:
        total = sum(line.amount for line in att.breakdown)
        drift = abs(total - att.total_reserves) / att.total_reserves
        checks.append(_check(
            "breakdown_sums_to_total", drift <= _SUM_TOLERANCE, "warn",
            f"breakdown sum {total:,.0f} vs stated total "
            f"{att.total_reserves:,.0f} ({drift * 100:.2f}% off)",
        ))

    date_ok, date_detail = _validate_date(att.as_of_date)
    checks.append(_check(
        "attestation_date_valid", date_ok, "critical", date_detail
    ))
    checks.append(_check(
        "extraction_confidence", att.confidence >= _MIN_CONFIDENCE, "warn",
        f"confidence={att.confidence:.2f} (floor {_MIN_CONFIDENCE})",
    ))
    return checks


def validate_metrics(metrics: Metrics) -> list[Check]:
    cov = metrics.attested_coverage
    if cov is None:
        return []
    in_band = _COVERAGE_FLOOR <= cov <= _COVERAGE_CEIL
    return [_check(
        "coverage_plausible", in_band, "critical",
        f"attested_coverage={cov:.4f} — plausible band "
        f"{_COVERAGE_FLOOR}-{_COVERAGE_CEIL}; outside it points to a "
        "data error, not a real position",
    )]


def validate_supply(supply: SupplyResult) -> list[Check]:
    return [_check(
        "supply_resolved", supply.total_supply > 0, "critical",
        f"total_supply={supply.total_supply:,.0f} "
        f"from {len(supply.per_chain)} chain(s)",
    )]


# ── citation verification — the trust mechanism, enforced ─────────────
_TOOL_CITE_RE = re.compile(r"\[tool:([a-z_]+)\]")
_SRC_CITE_RE = re.compile(r"\[([a-z0-9][a-z0-9-]+)\s*§")
# Only large, precisely-grouped figures ($1,000,000+) — the dangerous kind.
_BIG_MONEY_RE = re.compile(r"\$\s?([0-9]{1,3}(?:,[0-9]{3}){2,}(?:\.[0-9]+)?)")
_KNOWN_TOOLS = {
    "onchain_supply", "attestation_extract", "attestation_fetch", "metrics",
    "sanctions", "redemption",
}
_FIGURE_TOLERANCE = 0.005  # 0.5% — tolerates light rounding, catches wrong figures


def _allowed_figures(
    supply: SupplyResult, attestation: Attestation | None
) -> list[float]:
    values = [supply.total_supply, supply.native_supply, supply.bridged_supply]
    values += [c.supply for c in supply.per_chain]
    if attestation is not None:
        values += [attestation.total_reserves, attestation.tokens_outstanding]
        values += [line.amount for line in attestation.breakdown]
    return [v for v in values if v]


def validate_sanctions(screen: SanctionsScreen) -> list[Check]:
    """Guardrails over an OFAC sanctions screen."""
    stale = screen.sdn_staleness_days
    return [
        _check(
            "sdn_list_loaded", screen.sdn_address_count > 0, "critical",
            f"{screen.sdn_address_count} sanctioned crypto addresses loaded "
            f"from the OFAC SDN list",
        ),
        _check(
            "sdn_list_fresh",
            stale is not None and 0 <= stale <= 30, "warn",
            f"OFAC SDN list published {screen.sdn_publish_date or '?'}"
            + (f" — {stale} days ago" if stale is not None else ""),
        ),
        _check(
            "no_sanctioned_addresses", not screen.hits, "critical",
            f"{len(screen.hits)} screened address(es) are on the OFAC SDN "
            "list" if screen.hits
            else "no screened address is OFAC-sanctioned",
        ),
    ]


def validate_redemption(assessment: RedemptionAssessment) -> list[Check]:
    """Guardrails over a redemption-capacity assessment."""
    checks: list[Check] = []
    lc = assessment.liquid_coverage
    if lc is not None:
        checks.append(_check(
            "liquid_coverage_plausible", 0.0 <= lc <= 2.0, "critical",
            f"liquid_coverage={lc:.4f} — plausible band 0.0-2.0",
        ))
    att = assessment.attestation
    if att is not None and att.breakdown:
        classified = sum(t.amount for t in assessment.tiers)
        total = sum(line.amount for line in att.breakdown)
        ok = total == 0 or abs(classified - total) / abs(total) <= 0.01
        checks.append(_check(
            "reserve_classification_complete", ok, "warn",
            f"classified {classified:,.0f} of {total:,.0f} in reserve breakdown",
        ))
    return checks


def verify_citations(
    narrative: str,
    *,
    supply: SupplyResult,
    attestation: Attestation | None,
    passages: list[CorpusPassage],
) -> list[Check]:
    """Verify the narrative's citations resolve and its precise dollar figures
    trace to the facts packet. Makes the SKILL.md citation rule enforced, not
    merely instructed.
    """
    bad_tools = sorted(set(_TOOL_CITE_RE.findall(narrative)) - _KNOWN_TOOLS)
    allowed_sources = {p.source_id for p in passages}
    bad_sources = sorted(set(_SRC_CITE_RE.findall(narrative)) - allowed_sources)

    allowed = _allowed_figures(supply, attestation)
    untraceable = []
    for raw in _BIG_MONEY_RE.findall(narrative):
        value = float(raw.replace(",", ""))
        if not any(
            abs(value - a) <= _FIGURE_TOLERANCE * max(a, 1.0) for a in allowed
        ):
            untraceable.append(f"${raw}")

    return [
        _check(
            "citations_tool_valid", not bad_tools, "warn",
            f"unknown tool citations: {bad_tools}" if bad_tools
            else "tool citations resolve",
        ),
        _check(
            "citations_source_valid", not bad_sources, "critical",
            f"cites non-approved sources: {bad_sources}" if bad_sources
            else "corpus citations resolve to approved passages",
        ),
        _check(
            "figures_traceable", not untraceable, "critical",
            f"figures not traceable to tool outputs: {untraceable}"
            if untraceable else "stated dollar figures trace to tool outputs",
        ),
    ]
