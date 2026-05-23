"""LLM augmentation — fill context gaps when original sources are missing.

PRINCIPLE: original sources first. We only call the LLM when a
deterministic fetch returned nothing AND there's something meaningful to
say (we know the backing model, we know where the issuer publishes, we
have a known public URL the user can navigate to). The LLM provides
qualitative context — never numeric figures — and cites sources where it
can.

The result is tagged `AugmentedContext` and surfaced in the UI as
"AI context" — visually distinct from a verified deterministic figure,
so a user (or an examiner) can never confuse them.

Surfaces wired today:
  - attestation (fiat reserves PDF couldn't be reached)
  - sanctions   (OFAC SDN list unavailable, critically stale, or no
                 addresses to screen)
  - redemption  (no attestation -> no tiering, OR crypto-collateralized
                 token where the redemption mechanism is on-chain)

Web search is wired but feature-flagged behind `SCA_AUGMENT_WEB=1` — we
ship augmentation today on training-data-only context, and turn on web
search once we've validated the prompt under load.
"""
from __future__ import annotations

import json
import os
from typing import Any

from sca.config import Stablecoin
from sca.llm import LLMClient
from sca.llm.fallback import fallback_llm
from sca.models import AugmentedContext
from sca.observability import log_event, timed


def _backing_model_brief(model: str) -> str:
    """Short, deterministic description of what a backing model means.
    Used in prompts AND the UI badge tooltip — single source of truth."""
    return {
        "fiat_reserves":
            "Backed by off-chain cash, treasuries, or equivalents — an "
            "issuer publishes periodic attestations by an independent CPA.",
        "crypto_collateral":
            "Backed by on-chain collateral managed by a smart-contract "
            "protocol — backing is visible on-chain, not via a PDF.",
        "synthetic_delta_neutral":
            "Backed by delta-neutral positions (e.g. staked ETH + short "
            "perpetuals) — reserves are dynamic and visible on the issuer's "
            "live dashboard, not via a periodic PDF.",
        "algorithmic":
            "Stabilised by an algorithmic mechanism plus partial "
            "collateral — backing composition varies; live data on the "
            "protocol dashboard.",
        "new_or_unverified":
            "Recently launched. No mature published attestation system "
            "yet; treat any backing claim with caution until an "
            "independent attestation appears.",
    }.get(model, "")


def web_search_enabled() -> bool:
    """True whenever a search provider is configured.

    The previous `SCA_AUGMENT_WEB` opt-in flag has been removed:
    augmentation NEVER serves a bare 'n/a' to the user, and live news
    snippets are how the LLM gets current real-world citations. If a
    provider is configured at all, run it.
    """
    return bool(os.environ.get("SCA_WEB_SEARCH_PROVIDER", "").strip())


def _live_news_block(coin: Stablecoin, kind: str) -> str:
    """Fetch a few fresh news snippets and format for the augmentation
    prompt. Empty string when no provider is configured or the search
    returns nothing.

    The snippets are *factual context*, not figures — the LLM is reminded
    again in the prefix that it must cite URLs from these snippets rather
    than inventing them, and must NEVER state numeric figures.
    """
    if not web_search_enabled():
        return ""
    try:
        from sca.web_discovery import recent_news_snippets
        snippets = recent_news_snippets(
            coin.symbol, issuer=coin.issuer, kind=kind, limit=3,
        )
    except Exception as exc:  # noqa: BLE001 - never break augmentation
        log_event(
            "augment.news.fetch_failed", level="warn",
            symbol=coin.symbol, news_kind=kind,
            error_class=type(exc).__name__, error_message=str(exc),
        )
        return ""
    if not snippets:
        return ""
    lines = [
        "",
        "## Recent news context (factual material — cite URLs from here, "
        "never invent them; do NOT state numeric figures from these snippets):",
    ]
    for i, s in enumerate(snippets, 1):
        lines.append(
            f"  [{i}] {s.get('title', '')} ({s.get('age', '')})\n"
            f"      {s.get('snippet', '')}\n"
            f"      URL: {s.get('url', '')}"
        )
    return "\n".join(lines)


def _deterministic_fallback(
    coin: Stablecoin, surface: str, reason: str,
) -> AugmentedContext:
    """Composed-from-facts fallback when the LLM is unavailable.

    Product principle: never lead with "we failed." Lead with what we
    know about the token — issuer, backing model, where to verify
    directly — written for a financial reader, not an operator. The
    technical reason sits silently in the `reason` field for the
    Compendium / logs.
    """
    backing_brief = _backing_model_brief(coin.backing_model)
    parts = [
        f"{coin.symbol} is issued by {coin.issuer} on a {coin.backing_model.replace('_', ' ')} "
        f"model. {backing_brief}",
    ]
    citations: list[str] = []
    if coin.transparency_url:
        parts.append(
            f"The issuer publishes transparency information at "
            f"{coin.transparency_url} — the most reliable place to "
            f"verify current backing directly."
        )
        citations.append(coin.transparency_url)
    if coin.protocol_url and coin.protocol_url not in citations:
        parts.append(
            f"For the live on-chain backing or protocol mechanics, "
            f"see {coin.protocol_url}."
        )
        citations.append(coin.protocol_url)
    return AugmentedContext(
        surface=surface,
        reason=reason,
        text=" ".join(parts),
        citations=citations,
        confidence="deterministic-fallback",
        backing_model=coin.backing_model,
    )


def _augment_prompt(
    coin: Stablecoin, surface: str, reason: str,
) -> tuple[str, str]:
    """Returns (system, user). The system prompt is strict about what the
    LLM is allowed to do — qualitative context only, no figures invented."""
    backing_brief = _backing_model_brief(coin.backing_model)
    system = (
        "You are filling a CONTEXT gap for a stablecoin compliance tool "
        "called Doré. The deterministic fetch could not retrieve an "
        "original source; the UI shows your answer as 'AI context', "
        "clearly distinct from verified figures.\n\n"
        "STRICT RULES:\n"
        "  1. NEVER state numeric reserves, supply, or coverage figures. "
        "Those come from the deterministic pipeline only.\n"
        "  2. ALWAYS open by acknowledging WHY the automated fetch failed "
        "(e.g. 'Paxos publishes monthly attestations but the transparency "
        "page is JavaScript-rendered, so Doré's automated fetcher can't "
        "extract the PDF — manual fetch required'). This is critical: "
        "without the bridge, the UI shows contradictory states — your "
        "context saying 'attestation exists' next to n/a figures with no "
        "explanation.\n"
        "  3. **The reader can already see the n/a fields. Do not restate "
        "'no attestation available' as a finding. Instead, name what we "
        "DO know about this issuer's transparency mechanism, the cadence "
        "the auditor publishes at, the regulatory regime, and where the "
        "reader can look directly.** Add value beyond what the bare "
        "deterministic fields show.\n"
        "  4. Be qualitative: describe the backing model, the issuer's "
        "attestation cadence, the auditor name (e.g. Withum, BPM, Grant "
        "Thornton), the regulatory regime (NYDFS / MTL / MiCA), and "
        "where live data actually lives. Acknowledge your training-data "
        "cutoff.\n"
        "  5. Cite specific URLs where the user can verify — the issuer's "
        "transparency page, a protocol dashboard, a regulator filing.\n"
        "  6. Be short and useful — 3-6 sentences, plain prose, no headers.\n"
        "  7. If you genuinely don't know, say so — silence beats invention.\n"
        "  8. Return strict JSON: "
        "{\"text\": \"...\", \"citations\": [\"url1\", \"url2\"]}\n"
    )
    user = (
        f"Token: {coin.symbol} ({coin.name})\n"
        f"Issuer: {coin.issuer}\n"
        f"Backing model: {coin.backing_model} — {backing_brief}\n"
        f"Known protocol URL: {coin.protocol_url or '(none)'}\n"
        f"Known transparency URL: {coin.transparency_url or '(none)'}\n"
        f"Surface needing context: {surface}\n"
        f"Reason original source unavailable: {reason}\n"
        f"{_live_news_block(coin, kind='reserves')}\n\n"
        "Provide useful context for a financial reader."
    )
    return system, user


_AUGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text"],
}


def augment_attestation_gap(
    coin: Stablecoin, reason: str,
    *, llm: LLMClient | None = None,
) -> AugmentedContext:
    """Augment a missing attestation with LLM context.

    **Never returns None.** When the LLM is unavailable (no key, no
    network, timeout) we still return a clearly-tagged deterministic
    fallback card composed from what the registry already knows about
    the token — backing model, issuer, transparency / protocol URLs.
    The user's rule: never a bare n/a.
    """
    client = llm or fallback_llm()
    if client is None:
        return _deterministic_fallback(coin, "attestation", reason)
    system, user = _augment_prompt(coin, "attestation", reason)
    try:
        with timed("augment.attestation", symbol=coin.symbol,
                   backing_model=coin.backing_model):
            raw = client.extract_json(
                system=system, prompt=user, schema=_AUGMENT_SCHEMA,
            )
    except Exception as exc:  # noqa: BLE001 - augmentation never breaks the run
        log_event("augment.failed", level="warn",
                  symbol=coin.symbol, surface="attestation",
                  error_class=type(exc).__name__, error_message=str(exc))
        return _deterministic_fallback(coin, "attestation", reason)
    ctx = _build_context(coin, "attestation", reason, raw)
    return ctx or _deterministic_fallback(coin, "attestation", reason)


# ── sanctions augmentation ────────────────────────────────────────────
def _sanctions_prompt(
    coin: Stablecoin, reason: str,
) -> tuple[str, str]:
    """Strict, qualitative prompt for the sanctions surface.

    The LLM never claims a token is/isn't sanctioned and never invents
    addresses — that is the deterministic SDN-screen's job. We ask for
    structural context only: where OFAC publishes, what regime the token
    operates under, what the operator can do to recover.
    """
    backing_brief = _backing_model_brief(coin.backing_model)
    system = (
        "You are filling a CONTEXT gap for the SANCTIONS surface of a "
        "stablecoin compliance tool called Doré. The deterministic OFAC "
        "SDN screen could not run cleanly; the UI shows your answer as "
        "'AI context', clearly distinct from a real screening result.\n\n"
        "STRICT RULES:\n"
        "  1. NEVER claim a token, address, issuer or person is on or "
        "off the SDN list. That is the deterministic pipeline's job — "
        "the absence of a real screen is precisely why you're being "
        "called.\n"
        "  2. NEVER invent addresses, sanction case numbers, or "
        "specific OFAC actions you are not certain happened.\n"
        "  3. DO explain: where OFAC publishes the SDN list (the "
        "Treasury feed URLs), how often it updates, what the operator "
        "can do here (run `sca refresh` to re-fetch), and — if you know "
        "it — the regulatory regime the issuer operates under (e.g. "
        "NYDFS BitLicense / limited-purpose trust for Paxos tokens; "
        "Circle's state MTLs; MiCA in the EU).\n"
        "  4. Be short and useful — 3-6 sentences, plain prose, no headers.\n"
        "  5. If you genuinely don't know, say so — silence beats invention.\n"
        "  6. Return strict JSON: "
        "{\"text\": \"...\", \"citations\": [\"url1\", \"url2\"]}\n"
    )
    user = (
        f"Token: {coin.symbol} ({coin.name})\n"
        f"Issuer: {coin.issuer}\n"
        f"Backing model: {coin.backing_model} — {backing_brief}\n"
        f"Known protocol URL: {coin.protocol_url or '(none)'}\n"
        f"Known transparency URL: {coin.transparency_url or '(none)'}\n"
        f"Surface needing context: sanctions\n"
        f"Reason original SDN screen unavailable: {reason}\n"
        f"{_live_news_block(coin, kind='regulatory')}\n\n"
        "Provide qualitative context about the OFAC SDN list itself "
        "and the regulatory regime this issuer operates under. "
        "Do NOT assert anything about whether the token is or isn't "
        "sanctioned."
    )
    return system, user


def augment_sanctions_gap(
    coin: Stablecoin, reason: str,
    *, llm: LLMClient | None = None,
) -> AugmentedContext:
    """Augment a missing or stale OFAC SDN screen with LLM context.

    Fires when:
      - SDN list could not be loaded at all (`SanctionsUnavailable`)
      - SDN list is critically stale (>7 days)
      - on-chain supply read failed entirely so no addresses to screen

    Never returns None — falls back to a deterministic context card.
    """
    client = llm or fallback_llm()
    if client is None:
        return _deterministic_fallback(coin, "sanctions", reason)
    system, user = _sanctions_prompt(coin, reason)
    try:
        with timed("augment.sanctions", symbol=coin.symbol,
                   backing_model=coin.backing_model):
            raw = client.extract_json(
                system=system, prompt=user, schema=_AUGMENT_SCHEMA,
            )
    except Exception as exc:  # noqa: BLE001 - augmentation never breaks the run
        log_event("augment.failed", level="warn",
                  symbol=coin.symbol, surface="sanctions",
                  error_class=type(exc).__name__, error_message=str(exc))
        return _deterministic_fallback(coin, "sanctions", reason)
    ctx = _build_context(coin, "sanctions", reason, raw)
    return ctx or _deterministic_fallback(coin, "sanctions", reason)


# ── redemption augmentation ───────────────────────────────────────────
def _redemption_prompt(
    coin: Stablecoin, reason: str,
) -> tuple[str, str]:
    """Strict, qualitative prompt for the redemption surface.

    Two flavours, both qualitative:
      - fiat-backed without attestation: describe the issuer's redemption
        mechanism (T+0/T+1, KYC required, minimum size), pointing at the
        issuer's redemption-terms page.
      - crypto-collateralized: there is no fiat "tier" decomposition;
        redemption is on-chain via the protocol — burn -> receive
        collateral. Link to the protocol docs.
    """
    backing_brief = _backing_model_brief(coin.backing_model)
    is_crypto = coin.backing_model in (
        "crypto_collateral", "synthetic_delta_neutral", "algorithmic",
    )
    system = (
        "You are filling a CONTEXT gap for the REDEMPTION surface of a "
        "stablecoin compliance tool called Doré. The deterministic "
        "liquidity-tier classification could not run; the UI shows your "
        "answer as 'AI context', clearly distinct from a verified "
        "tier breakdown.\n\n"
        "STRICT RULES:\n"
        "  1. NEVER state numeric reserves, liquid coverage, redemption "
        "limits, or fee figures. Those come from the deterministic "
        "pipeline only.\n"
        "  2. DO describe the redemption MECHANISM qualitatively:\n"
        "     - For fiat-backed tokens without an attestation: explain "
        "the issuer's published redemption process — typical settlement "
        "window (e.g. T+0/T+1), whether KYC / a primary-market account "
        "is required, and that minimum redemption sizes usually apply. "
        "Link the issuer's redemption-terms page.\n"
        "     - For crypto-collateralized / synthetic / algorithmic "
        "tokens: explain that redemption is ON-CHAIN via the protocol "
        "(burn the stablecoin -> receive the underlying collateral or a "
        "claim against it), no off-chain settlement, no fiat tiering. "
        "Link the protocol docs / dashboard.\n"
        "  3. Be short and useful — 3-6 sentences, plain prose, no headers.\n"
        "  4. If you genuinely don't know the issuer's redemption process, "
        "say so — silence beats invention.\n"
        "  5. Return strict JSON: "
        "{\"text\": \"...\", \"citations\": [\"url1\", \"url2\"]}\n"
    )
    flavour = (
        "ON-CHAIN redemption — describe the burn-for-collateral mechanism "
        "and link the protocol docs."
        if is_crypto
        else "FIAT-style redemption — describe the issuer's redemption "
             "process and link the redemption-terms page."
    )
    user = (
        f"Token: {coin.symbol} ({coin.name})\n"
        f"Issuer: {coin.issuer}\n"
        f"Backing model: {coin.backing_model} — {backing_brief}\n"
        f"Known protocol URL: {coin.protocol_url or '(none)'}\n"
        f"Known transparency URL: {coin.transparency_url or '(none)'}\n"
        f"Surface needing context: redemption\n"
        f"Reason original tier breakdown unavailable: {reason}\n"
        f"Requested flavour: {flavour}\n"
        f"{_live_news_block(coin, kind='redemption')}\n\n"
        "Provide useful qualitative context about how a holder actually "
        "redeems this token."
    )
    return system, user


def augment_redemption_gap(
    coin: Stablecoin, reason: str,
    *, llm: LLMClient | None = None,
) -> AugmentedContext:
    """Augment a missing redemption-tier breakdown with LLM context.

    Fires when:
      - no attestation -> no reserve composition -> can't classify tiers
      - coin is crypto-collateralized / synthetic / algorithmic — no
        fiat-style tier decomposition makes sense; redemption is on-chain

    Never returns None — falls back to a deterministic context card.
    """
    client = llm or fallback_llm()
    if client is None:
        return _deterministic_fallback(coin, "redemption", reason)
    system, user = _redemption_prompt(coin, reason)
    try:
        with timed("augment.redemption", symbol=coin.symbol,
                   backing_model=coin.backing_model):
            raw = client.extract_json(
                system=system, prompt=user, schema=_AUGMENT_SCHEMA,
            )
    except Exception as exc:  # noqa: BLE001 - augmentation never breaks the run
        log_event("augment.failed", level="warn",
                  symbol=coin.symbol, surface="redemption",
                  error_class=type(exc).__name__, error_message=str(exc))
        return _deterministic_fallback(coin, "redemption", reason)
    ctx = _build_context(coin, "redemption", reason, raw)
    return ctx or _deterministic_fallback(coin, "redemption", reason)


def _build_context(
    coin: Stablecoin, surface: str, reason: str, raw: Any,
) -> AugmentedContext | None:
    """Parse LLM JSON response into AugmentedContext. Defensive against
    malformed output — drop the augmentation rather than render garbage."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log_event("augment.parse_failed", level="warn",
                      symbol=coin.symbol, surface=surface)
            return None
    elif isinstance(raw, dict):
        data = raw
    else:
        return None
    text = (data.get("text") or "").strip()
    if not text:
        return None
    citations = [c for c in (data.get("citations") or []) if isinstance(c, str)]
    return AugmentedContext(
        surface=surface,
        reason=reason,
        text=text,
        citations=citations,
        confidence=(
            "training-data-only" if not web_search_enabled() else "web-searched"
        ),
        backing_model=coin.backing_model,
    )
