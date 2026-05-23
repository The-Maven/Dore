"""Agent orchestration: compose tools + corpus + synthesis into an Analysis.

Mirrors the procedure in skill/attestation-analysis/SKILL.md. The deployed
hermes-agent runs that procedure via the skill; this is the same flow in
plain Python — used by the CLI and the eval harness.

Completed analyses are cached by an input fingerprint (see AnalysisCache):
an unchanged analysis is not recomputed, so a live monitor can poll freely
without re-running LLM extraction and synthesis. `refresh=True` forces a
fresh computation.
"""
from __future__ import annotations

from pathlib import Path

from sca import config
from sca.agent.synthesis import synthesize
from sca.cache import AnalysisCache
from sca.corpus import retrieve
from sca.corpus.sources import included_sources
from sca.llm import LLMClient
from sca.models import Analysis, Attestation, Gap
from sca.tools import (
    AttestationUnavailable,
    compute_metrics,
    extract_attestation,
    extract_pdf_text,
    fetch_latest_attestation,
    get_onchain_supply,
)
from sca.validation import (
    validate_attestation,
    validate_metrics,
    validate_supply,
    verify_citations,
)

_analysis_cache = AnalysisCache(ttl=3600.0)


def _corpus_version() -> str:
    """Identity of the included-corpus set — changes when curation changes."""
    return ",".join(sorted(s.id for s in included_sources()))


def analyze(
    symbol: str,
    *,
    allow_unverified: bool = True,
    llm: LLMClient | None = None,
    attestation: Attestation | None = None,
    synthesize_narrative: bool = True,
    refresh: bool = False,
    user_id: str | None = None,
) -> Analysis:
    """Run the full attestation-analysis procedure for `symbol`.

    `attestation` may be supplied directly (skips fetch/extract).
    `refresh=True` bypasses the analysis cache and recomputes from scratch.
    `user_id` attributes the persisted analysis row to a user (if known).

    The in-memory `AnalysisCache` is the hot-path speed cache. Completed
    canonical runs are ADDITIVELY persisted through the durable `Store`, so
    production accrues an analysis history without changing any behaviour.
    """
    gaps: list[Gap] = []

    # 1. Facts — live supply (itself TTL-cached for ~60s).
    supply = get_onchain_supply(symbol, allow_unverified=allow_unverified)

    # The result cache covers only the canonical full analysis. A
    # caller-injected attestation or a no-narrative run is always computed.
    fingerprint = None
    if attestation is None and synthesize_narrative:
        fingerprint = AnalysisCache.fingerprint(
            symbol, supply.total_supply, _corpus_version()
        )
        if not refresh:
            cached = _analysis_cache.get(fingerprint)
            if cached is not None:
                return cached

    for warning in supply.warnings:
        gaps.append(Gap("warn", "coverage", warning))

    # 2. Facts — latest attestation (seed-gated; see tools/attestation_fetch).
    # If the token's backing model isn't fiat_reserves, there is no fiat
    # attestation by design — skip the fetch and let augmentation fill the
    # context block with "what we know about the on-chain backing model".
    att = attestation
    coin = config.get_stablecoin(symbol)
    augmentations: list = []
    if att is None and coin.backing_model == "fiat_reserves":
        try:
            fetched = fetch_latest_attestation(symbol)
            text = extract_pdf_text(Path(fetched["local_path"]))
            att = extract_attestation(
                symbol=symbol,
                document_text=text,
                source_url=fetched["source_url"],
                llm=llm,
            )
        except AttestationUnavailable as exc:
            gaps.append(Gap("warn", "data", _friendly_attestation_gap(exc, coin)))
        except Exception as exc:  # noqa: BLE001 - report, never crash analysis
            # Log the technical detail for ops / future Sentry; show the
            # user a clean human-readable explanation.
            from sca.observability import log_event
            log_event(
                "attestation.pipeline_failed", level="warn",
                symbol=symbol,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
            gaps.append(
                Gap("warn", "data", _friendly_attestation_gap(exc, coin))
            )

    # Augmentation — if no attestation could be resolved AND we have
    # something meaningful to say, ask the LLM to fill the context block.
    # Bounded to qualitative information; never invents numbers. The
    # block is tagged in the UI as 'AI context' and never replaces a
    # deterministic figure.
    if att is None:
        from sca import augment

        reason = (
            "no-fiat-attestation-by-design"
            if coin.backing_model != "fiat_reserves"
            else "live-attestation-fetch-failed"
        )
        ctx = augment.augment_attestation_gap(coin, reason, llm=llm)
        if ctx is not None:
            augmentations.append(ctx)

    # 3. Facts — metrics. Compose provenance from the supply result's
    # per-chain consensus so the UI can show "computed across 5 chains,
    # all RPCs corroborated" rather than a bare ratio with no audit trail.
    metrics = None
    if att is not None:
        agree = sum(1 for c in supply.per_chain
                    if "agree" in (c.consensus or "").lower())
        prov_bits = [
            f"1 issuer attestation",
            f"{supply.chains_read}/{supply.chains_expected} chains read",
        ]
        if agree:
            prov_bits.append(f"{agree} cross-RPC corroborated")
        if not supply.complete:
            prov_bits.append("PARTIAL TOTAL")
        provenance = " · ".join(prov_bits)
        metrics = compute_metrics(
            attested_reserves=att.total_reserves,
            attested_tokens=att.tokens_outstanding,
            current_supply=supply.total_supply,
            attestation_date=att.as_of_date,
            provenance=provenance,
        )

    # 4. Deterministic guardrails over the critical data.
    checks = list(validate_supply(supply))
    if att is not None:
        checks += validate_attestation(att)
    if metrics is not None:
        checks += validate_metrics(metrics)

    # 5. Reasoning frame — every included source the corpus has text for.
    passages = retrieve(
        f"{symbol} reserve composition coverage redemption attestation"
    )
    if not passages:
        gaps.append(Gap(
            "warn", "corpus",
            "corpus has no ingested source text — judgements remain "
            "unsupported until source text is staged and ingested",
        ))

    # 6. Synthesis, then verify the narrative actually cites its work.
    narrative = ""
    if synthesize_narrative:
        narrative = synthesize(
            symbol=symbol,
            supply=supply,
            attestation=att,
            metrics=metrics,
            passages=passages,
            llm=llm,
        )
        checks += verify_citations(
            narrative, supply=supply, attestation=att, passages=passages
        )

    # 7. Escalate every critical guardrail / citation failure to a gap.
    for check in checks:
        if check.passed or check.severity != "critical":
            continue
        category = (
            "citation"
            if check.name.startswith(("citations", "figures"))
            else "guardrail"
        )
        gaps.append(Gap("critical", category, f"{check.name}: {check.detail}"))

    # AI Brief — editorial top-of-view synthesis. Best-effort, never
    # blocks the result. Built from the same facts the deterministic
    # pipeline already computed + recent corpus passages so "relevant news"
    # is real (and cited), not invented.
    brief_dict = None
    if synthesize_narrative:
        try:
            from sca.brief import generate_brief
            from sca.agent.synthesis import _facts_block
            facts = _facts_block(symbol, supply, att, metrics)
            brief_obj = generate_brief(
                surface="analyze", coin=coin, facts=facts,
                news_candidates=passages, llm=llm,
            )
            if brief_obj is not None:
                from dataclasses import asdict as _asdict
                brief_dict = _asdict(brief_obj)
        except Exception:  # noqa: BLE001 - brief is optional
            pass

    result = Analysis(
        symbol=symbol,
        supply=supply,
        attestation=att,
        metrics=metrics,
        passages=passages,
        checks=checks,
        narrative=narrative,
        gaps=gaps,
        augmentations=augmentations,
        backing_model=coin.backing_model,
        protocol_url=coin.protocol_url,
        brief=brief_dict,
    )
    if fingerprint is not None:
        _analysis_cache.put(fingerprint, result)
        _persist_analysis(symbol, fingerprint, result, user_id)
    return result


def _friendly_attestation_gap(exc: Exception, coin) -> str:
    """Translate a raw exception into a user-readable gap message.

    Engineers see the original exception via the structured log event;
    end users see plain English about WHY the attestation is missing and
    what they can do about it. Technical strings (URLs, HTTP codes,
    stack traces) belong in observability, not the UI.
    """
    msg = str(exc).lower()
    if "404" in msg or "not found" in msg:
        return (
            f"The latest {coin.symbol} attestation document is no longer at "
            "the URL we had on file — the issuer has likely rotated it. "
            "On-chain supply figures are unaffected. The system will look "
            "for a new location on the next refresh; you can also open "
            f"{coin.transparency_url or 'the issuer site'} directly."
        )
    if "javascript" in msg or "js" in msg or "no machine" in msg:
        return (
            f"The {coin.issuer} transparency page renders its attestation "
            "via JavaScript, which can't be extracted automatically. "
            "On-chain supply figures are unaffected; only attestation-derived "
            f"figures are missing. Open {coin.transparency_url or 'the issuer site'} "
            "to view the current report."
        )
    if "no transparency" in msg or "no seed" in msg or "no source" in msg:
        return (
            f"No transparency source has been configured for {coin.symbol} "
            "yet. On-chain supply figures are unaffected."
        )
    if "timeout" in msg or "connection" in msg:
        return (
            f"The {coin.issuer} attestation source could not be reached "
            "(network timeout). On-chain supply figures are unaffected; "
            "the system will retry on the next refresh."
        )
    # Generic fallback — still no raw exception text.
    return (
        f"The latest {coin.symbol} attestation could not be retrieved. "
        "On-chain supply figures are unaffected; only attestation-derived "
        "figures are missing. The system will retry on the next refresh."
    )


def _persist_analysis(
    symbol: str,
    fingerprint: str,
    result: Analysis,
    user_id: str | None,
) -> None:
    """Additively record a completed canonical run in the durable Store.

    Behaviour-neutral: persistence is best-effort and never affects the
    returned Analysis. The in-memory AnalysisCache remains the speed cache.
    """
    from dataclasses import asdict

    from sca.store import get_store

    try:
        store = get_store()
        analysis_id = store.create_analysis(
            "attestation", symbol, user_id=user_id
        )
        store.update_analysis(
            analysis_id,
            status="done",
            result=asdict(result),
            input_fingerprint=fingerprint,
        )
    except Exception:  # noqa: BLE001 - persistence must never break analysis
        pass
