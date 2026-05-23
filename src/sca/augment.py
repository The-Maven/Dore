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
    """Feature flag for web search — off by default, opt in for live runs."""
    return os.environ.get("SCA_AUGMENT_WEB", "").strip().lower() in (
        "1", "true", "on"
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
        "  3. Be qualitative: describe the backing model, the issuer's "
        "attestation cadence, the auditor name (e.g. Withum, BPM, Grant "
        "Thornton), the regulatory regime (NYDFS / MTL / MiCA), and "
        "where live data actually lives. Acknowledge your training-data "
        "cutoff.\n"
        "  4. Cite specific URLs where the user can verify — the issuer's "
        "transparency page, a protocol dashboard, a regulator filing.\n"
        "  5. Be short and useful — 3-6 sentences, plain prose, no headers.\n"
        "  6. If you genuinely don't know, say so — silence beats invention.\n"
        "  7. Return strict JSON: "
        "{\"text\": \"...\", \"citations\": [\"url1\", \"url2\"]}\n"
    )
    user = (
        f"Token: {coin.symbol} ({coin.name})\n"
        f"Issuer: {coin.issuer}\n"
        f"Backing model: {coin.backing_model} — {backing_brief}\n"
        f"Known protocol URL: {coin.protocol_url or '(none)'}\n"
        f"Known transparency URL: {coin.transparency_url or '(none)'}\n"
        f"Surface needing context: {surface}\n"
        f"Reason original source unavailable: {reason}\n\n"
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
) -> AugmentedContext | None:
    """Augment a missing attestation with LLM context. Returns None on any
    error (augmentation is best-effort; never breaks the analysis)."""
    client = llm or fallback_llm()
    if client is None:
        return None
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
        return None
    return _build_context(coin, "attestation", reason, raw)


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
        f"Reason original SDN screen unavailable: {reason}\n\n"
        "Provide qualitative context about the OFAC SDN list itself "
        "and the regulatory regime this issuer operates under. "
        "Do NOT assert anything about whether the token is or isn't "
        "sanctioned."
    )
    return system, user


def augment_sanctions_gap(
    coin: Stablecoin, reason: str,
    *, llm: LLMClient | None = None,
) -> AugmentedContext | None:
    """Augment a missing or stale OFAC SDN screen with LLM context.

    Fires when:
      - SDN list could not be loaded at all (`SanctionsUnavailable`)
      - SDN list is critically stale (>7 days)
      - on-chain supply read failed entirely so no addresses to screen

    Returns None on any error — augmentation is best-effort and never
    breaks the sanctions surface.
    """
    client = llm or fallback_llm()
    if client is None:
        return None
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
        return None
    return _build_context(coin, "sanctions", reason, raw)


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
        f"Requested flavour: {flavour}\n\n"
        "Provide useful qualitative context about how a holder actually "
        "redeems this token."
    )
    return system, user


def augment_redemption_gap(
    coin: Stablecoin, reason: str,
    *, llm: LLMClient | None = None,
) -> AugmentedContext | None:
    """Augment a missing redemption-tier breakdown with LLM context.

    Fires when:
      - no attestation -> no reserve composition -> can't classify tiers
      - coin is crypto-collateralized / synthetic / algorithmic — no
        fiat-style tier decomposition makes sense; redemption is on-chain

    Returns None on any error — augmentation is best-effort and never
    breaks the redemption surface.
    """
    client = llm or fallback_llm()
    if client is None:
        return None
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
        return None
    return _build_context(coin, "redemption", reason, raw)


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
