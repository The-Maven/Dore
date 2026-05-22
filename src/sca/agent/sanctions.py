"""Agent orchestration: OFAC sanctions screen for a stablecoin.

Screens the token's on-chain contract addresses against the OFAC SDN list,
applies deterministic guardrails, retrieves the approved corpus, and
synthesises a cited analysis — the same shape as attestation analysis.
"""
from __future__ import annotations

from sca import config
from sca.agent.synthesis import synthesize_surface
from sca.corpus import retrieve
from sca.llm import LLMClient
from sca.models import Gap, SanctionsScreen
from sca.tools import get_onchain_supply
from sca.tools.sanctions import SanctionsUnavailable, load_sdn, screen
from sca.validation import (
    validate_sanctions,
    validate_supply,
    verify_citations,
)


def _facts(result: SanctionsScreen) -> str:
    lines = [
        "## Tool results — the ONLY figures you may state",
        f"symbol: {result.symbol}",
        f"[tool:sanctions] OFAC SDN list published "
        f"{result.sdn_publish_date or '?'} "
        f"({result.sdn_address_count} sanctioned digital-currency addresses)",
        f"[tool:sanctions] screened {len(result.screened)} contract "
        f"address(es): {', '.join(result.screened) or 'none'}",
    ]
    if result.hits:
        for hit in result.hits:
            lines.append(
                f"[tool:sanctions] MATCH {hit.address} ({hit.currency}) "
                f"— OFAC SDN entry: {hit.sdn_name}"
            )
    else:
        lines.append(
            "[tool:sanctions] no screened address is on the OFAC SDN list"
        )
    lines.append(
        f"[tool:onchain_supply] total_supply={result.supply.total_supply:,.2f}"
    )
    return "\n".join(lines)


def screen_token(
    symbol: str,
    *,
    llm: LLMClient | None = None,
    refresh: bool = False,
    synthesize_narrative: bool = True,
    user_id: str | None = None,
) -> SanctionsScreen:
    """Screen a stablecoin's contract addresses against the OFAC SDN list.

    `user_id` attributes the persisted screen to a user (if known).
    """
    gaps: list[Gap] = []
    coin = config.get_stablecoin(symbol)

    supply = get_onchain_supply(symbol, allow_unverified=True)
    for warning in supply.warnings:
        gaps.append(Gap("warn", "coverage", warning))

    screened = [d.contract for d in coin.deployments]
    hits = []
    sdn = None
    try:
        sdn = load_sdn(refresh=refresh)
        hits = screen(screened)
    except SanctionsUnavailable as exc:
        gaps.append(Gap("warn", "data", f"OFAC screening unavailable: {exc}"))
    except Exception as exc:  # noqa: BLE001 - report, never crash
        gaps.append(Gap("warn", "data", f"sanctions screen failed: {exc}"))

    result = SanctionsScreen(
        symbol=symbol,
        supply=supply,
        screened=screened,
        hits=hits,
        sdn_publish_date=sdn.publish_date if sdn else "",
        sdn_staleness_days=sdn.staleness_days if sdn else None,
        sdn_address_count=len(sdn.addresses) if sdn else 0,
    )

    checks = list(validate_supply(supply))
    if sdn is not None:
        checks += validate_sanctions(result)

    passages = retrieve(
        f"{symbol} OFAC sanctions screening blocked address freeze obligation"
    )
    if not passages:
        gaps.append(Gap(
            "warn", "corpus",
            "corpus has no approved + ingested sources — sanctions-posture "
            "judgements remain unsupported until a human approves sources",
        ))

    narrative = ""
    if synthesize_narrative:
        narrative = synthesize_surface(
            skill="sanctions", facts=_facts(result), passages=passages, llm=llm
        )
        checks += verify_citations(
            narrative, supply=supply, attestation=None, passages=passages
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
        _persist_screen(symbol, result, user_id)
    return result


def _persist_screen(
    symbol: str, result: SanctionsScreen, user_id: str | None
) -> None:
    """Best-effort: record a completed sanctions screen in the durable Store.

    Behaviour-neutral — persistence never affects the returned screen.
    """
    from dataclasses import asdict

    from sca.store import get_store

    try:
        store = get_store()
        analysis_id = store.create_analysis(
            "sanctions", symbol, user_id=user_id
        )
        store.update_analysis(
            analysis_id, status="done", result=asdict(result)
        )
    except Exception:  # noqa: BLE001 - persistence must never break the screen
        pass
