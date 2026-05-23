"""Agent orchestration: OFAC sanctions screen for a stablecoin.

Screens the token's on-chain contract addresses against the OFAC SDN list,
applies deterministic guardrails, retrieves the corpus reasoning frame,
and synthesises a cited analysis — the same shape as attestation analysis.
"""
from __future__ import annotations

from sca import config
from sca.agent.synthesis import synthesize_surface
from sca.corpus import retrieve
from sca.llm import LLMClient
from sca.models import AugmentedContext, Gap, SanctionsScreen
from sca.tools import get_onchain_supply
from sca.tools.sanctions import SanctionsUnavailable, load_sdn, screen
from sca.validation import (
    validate_sanctions,
    validate_supply,
    verify_citations,
)

# Past one week the OFAC SDN list is no longer reliable evidence — see
# validate_sanctions(). When we cross that threshold, augment with the
# qualitative context for the operator (where to fetch, what to do next).
_SDN_CRITICAL_STALENESS_DAYS = 7


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
    sdn_failure_reason: str | None = None
    try:
        sdn = load_sdn(refresh=refresh)
        hits = screen(screened)
    except SanctionsUnavailable as exc:
        sdn_failure_reason = f"sdn-list-unavailable: {exc}"
        gaps.append(Gap("warn", "data", f"OFAC screening unavailable: {exc}"))
    except Exception as exc:  # noqa: BLE001 - report, never crash
        sdn_failure_reason = f"sdn-screen-failed: {exc}"
        gaps.append(Gap("warn", "data", f"sanctions screen failed: {exc}"))

    # Augmentation — qualitative context whenever the deterministic
    # screen couldn't run cleanly. Bounded to non-numeric, never claims
    # the token is/isn't sanctioned. Tagged in the UI as 'AI context'.
    #
    # Gap surfaces that trigger augmentation:
    #   - SDN list could not be loaded / parsed at all
    #   - SDN list is critically stale (>7 days) — screening unreliable
    #   - on-chain supply read failed entirely so no addresses to screen
    augmentations: list[AugmentedContext] = []
    aug_reason: str | None = None
    if sdn_failure_reason is not None:
        aug_reason = sdn_failure_reason
    elif (
        sdn is not None
        and sdn.staleness_days is not None
        and sdn.staleness_days > _SDN_CRITICAL_STALENESS_DAYS
    ):
        aug_reason = (
            f"sdn-list-critically-stale: published "
            f"{sdn.publish_date} ({sdn.staleness_days} days old)"
        )
    elif not screened:
        aug_reason = "no-deployment-addresses-to-screen"
    if aug_reason is not None:
        try:
            from sca import augment

            ctx = augment.augment_sanctions_gap(coin, aug_reason, llm=llm)
            if ctx is not None:
                augmentations.append(ctx)
        except Exception as exc:  # noqa: BLE001 - never break the screen
            from sca.observability import log_event
            log_event(
                "augment.sanctions.dispatch_failed", level="warn",
                symbol=symbol,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )

    result = SanctionsScreen(
        symbol=symbol,
        supply=supply,
        screened=screened,
        hits=hits,
        sdn_publish_date=sdn.publish_date if sdn else "",
        sdn_staleness_days=sdn.staleness_days if sdn else None,
        sdn_address_count=len(sdn.addresses) if sdn else 0,
        augmentations=augmentations,
        backing_model=coin.backing_model,
        protocol_url=coin.protocol_url,
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
            "corpus has no ingested source text — sanctions-posture "
            "judgements remain unsupported until source text is staged "
            "and ingested",
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

    # AI Brief — editorial top-of-view synthesis. Best-effort.
    if synthesize_narrative:
        try:
            from sca.brief import generate_brief
            from dataclasses import asdict as _asdict
            brief_obj = generate_brief(
                surface="sanctions", coin=coin, facts=_facts(result),
                news_candidates=passages, llm=llm,
            )
            if brief_obj is not None:
                result.brief = _asdict(brief_obj)
        except Exception:  # noqa: BLE001 - brief is optional
            pass
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
