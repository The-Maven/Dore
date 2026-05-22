"""Agent orchestration: redemption-capacity assessment for a stablecoin.

Builds ON the attestation analysis — reusing its supply, attestation and
metrics (cached, guardrailed) — then classifies reserve liquidity, computes
liquid coverage and net redemption flow, applies guardrails, and synthesises
a cited analysis against the redemption-rights corpus.
"""
from __future__ import annotations

from sca.agent.analyze import analyze
from sca.agent.synthesis import synthesize_surface
from sca.corpus import retrieve
from sca.llm import LLMClient
from sca.models import Gap, RedemptionAssessment
from sca.tools.redemption import classify_reserves, liquid_reserves
from sca.validation import validate_redemption, verify_citations


def _facts(a: RedemptionAssessment) -> str:
    lines = [
        "## Tool results — the ONLY figures you may state",
        f"symbol: {a.symbol}",
        f"[tool:onchain_supply] total_supply={a.supply.total_supply:,.2f}",
    ]
    if a.attestation is not None:
        lines.append(
            f"[tool:attestation_extract] attested_reserves="
            f"{a.attestation.total_reserves:,.2f} "
            f"tokens_outstanding={a.attestation.tokens_outstanding:,.2f} "
            f"as_of={a.attestation.as_of_date}"
        )
    if a.metrics is not None and a.metrics.attested_coverage is not None:
        lines.append(
            f"[tool:metrics] attested_coverage="
            f"{a.metrics.attested_coverage:.4f}"
        )
    lines.append(f"[tool:redemption] liquid_reserves={a.liquid_reserves:,.2f}")
    lines.append(
        "[tool:redemption] liquid_coverage="
        + (f"{a.liquid_coverage:.4f}" if a.liquid_coverage is not None
           else "n/a")
    )
    if a.net_redemption_flow is not None:
        lines.append(
            f"[tool:redemption] net_redemption_flow="
            f"{a.net_redemption_flow:+,.2f} "
            "(on-chain supply minus attested tokens)"
        )
    for tier in a.tiers:
        lines.append(
            f"[tool:redemption]   {tier.tier}: {tier.asset_class} "
            f"{tier.amount:,.2f}"
        )
    return "\n".join(lines)


def assess_redemption(
    symbol: str,
    *,
    llm: LLMClient | None = None,
    refresh: bool = False,
    synthesize_narrative: bool = True,
    user_id: str | None = None,
) -> RedemptionAssessment:
    """Assess a stablecoin's redemption capacity.

    `user_id` attributes the persisted assessment to a user (if known).
    """
    # Reuse the attestation analysis — supply, attestation, metrics (cached).
    base = analyze(symbol, llm=llm, refresh=refresh, synthesize_narrative=False)
    # Carry forward the supply / data gaps as context; corpus + redemption
    # guardrail gaps are added fresh below.
    gaps: list[Gap] = [
        g for g in base.gaps if g.category in ("coverage", "data")
    ]

    tiers = []
    liquid = 0.0
    liquid_cov = None
    net_flow = None
    if base.attestation is not None:
        tiers = classify_reserves(base.attestation)
        liquid = liquid_reserves(tiers)
        if base.supply.total_supply:
            liquid_cov = liquid / base.supply.total_supply
        net_flow = (
            base.supply.total_supply - base.attestation.tokens_outstanding
        )

    result = RedemptionAssessment(
        symbol=symbol,
        supply=base.supply,
        attestation=base.attestation,
        metrics=base.metrics,
        tiers=tiers,
        liquid_reserves=liquid,
        liquid_coverage=liquid_cov,
        net_redemption_flow=net_flow,
    )

    checks = validate_redemption(result)

    passages = retrieve(
        f"{symbol} redemption right reserve liquidity coverage par"
    )
    if not passages:
        gaps.append(Gap(
            "warn", "corpus",
            "corpus has no approved + ingested sources — redemption "
            "judgements remain unsupported until a human approves sources",
        ))

    narrative = ""
    if synthesize_narrative:
        narrative = synthesize_surface(
            skill="redemption", facts=_facts(result), passages=passages,
            llm=llm,
        )
        checks += verify_citations(
            narrative, supply=base.supply,
            attestation=base.attestation, passages=passages,
        )

    for check in checks:
        if check.passed or check.severity != "critical":
            continue
        category = (
            "citation"
            if check.name.startswith(("citations", "figures"))
            else "guardrail"
        )
        gaps.append(Gap("critical", category, f"{check.name}: {check.detail}"))

    result.checks = checks
    result.passages = passages
    result.narrative = narrative
    result.gaps = gaps
    if synthesize_narrative:
        _persist_redemption(symbol, result, user_id)
    return result


def _persist_redemption(
    symbol: str, result: RedemptionAssessment, user_id: str | None
) -> None:
    """Best-effort: record a completed redemption assessment in the Store.

    Behaviour-neutral — persistence never affects the returned assessment.
    """
    from dataclasses import asdict

    from sca.store import get_store

    try:
        store = get_store()
        analysis_id = store.create_analysis(
            "redemption", symbol, user_id=user_id
        )
        store.update_analysis(
            analysis_id, status="done", result=asdict(result)
        )
    except Exception:  # noqa: BLE001 - persistence must never break the run
        pass
