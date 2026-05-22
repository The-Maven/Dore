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

from sca.agent.synthesis import synthesize
from sca.cache import AnalysisCache
from sca.corpus import retrieve
from sca.corpus.sources import approved_sources
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
    """Identity of the approved-corpus set — changes when curation changes."""
    return ",".join(sorted(s.id for s in approved_sources()))


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
    att = attestation
    if att is None:
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
            gaps.append(Gap("warn", "data", str(exc)))
        except Exception as exc:  # noqa: BLE001 - report, never crash analysis
            gaps.append(
                Gap("warn", "data", f"attestation pipeline failed: {exc}")
            )

    # 3. Facts — metrics.
    metrics = None
    if att is not None:
        metrics = compute_metrics(
            attested_reserves=att.total_reserves,
            attested_tokens=att.tokens_outstanding,
            current_supply=supply.total_supply,
            attestation_date=att.as_of_date,
        )

    # 4. Deterministic guardrails over the critical data.
    checks = list(validate_supply(supply))
    if att is not None:
        checks += validate_attestation(att)
    if metrics is not None:
        checks += validate_metrics(metrics)

    # 5. Reasoning frame — approved corpus only.
    passages = retrieve(
        f"{symbol} reserve composition coverage redemption attestation"
    )
    if not passages:
        gaps.append(Gap(
            "warn", "corpus",
            "corpus has no approved + ingested sources — judgements remain "
            "unsupported until a human approves sources in corpus/sources.yaml",
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

    result = Analysis(
        symbol=symbol,
        supply=supply,
        attestation=att,
        metrics=metrics,
        passages=passages,
        checks=checks,
        narrative=narrative,
        gaps=gaps,
    )
    if fingerprint is not None:
        _analysis_cache.put(fingerprint, result)
        _persist_analysis(symbol, fingerprint, result, user_id)
    return result


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
