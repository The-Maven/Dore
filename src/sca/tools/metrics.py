"""Deterministic tool: attestation-vs-supply metrics.

LAYER: facts. Pure arithmetic — no LLM, no network.

Deliberately does NOT decide whether a number is "good" or "bad". Coverage
near 100% is normal (attestations are point-in-time, supply is live).
Interpretation is the agent's job, against the corpus.
"""
from __future__ import annotations

from datetime import date, datetime

from sca.models import Metrics


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def compute_metrics(
    *,
    attested_reserves: float,
    attested_tokens: float,
    current_supply: float,
    attestation_date,
    as_of: date | None = None,
    provenance: str = "",
) -> Metrics:
    """Compare a point-in-time attestation against live on-chain supply.

    `provenance` (e.g. "1 attestation + 5-chain supply, 4/5 RPCs corroborated")
    is surfaced in the UI so the user sees how every number was sourced.
    """
    as_of = as_of or date.today()
    att_date = _as_date(attestation_date)
    return Metrics(
        # The honest backing ratio — both figures as of the attestation date.
        attested_coverage=(
            attested_reserves / attested_tokens if attested_tokens else None
        ),
        # Reserves vs *live* supply — useful, but contaminated by supply drift.
        live_coverage=(
            attested_reserves / current_supply if current_supply else None
        ),
        staleness_days=(as_of - att_date).days,
        supply_drift=(
            (current_supply - attested_tokens) / attested_tokens
            if attested_tokens
            else None
        ),
        provenance=provenance,
    )
