"""AI Brief — the editorially-curated top-of-view synthesis.

PRINCIPLE: every view leads with a single editorial brief that names
the bottom line: where the token stands, what's noteworthy in recent
regulatory news, what (if anything) requires the user's attention.

Bounded the same way as augmentation:
  - Numeric figures come from the structured result, NEVER invented
  - Recent-news items come from the actual corpus discovery pollers
    (FSB, OFAC, NYDFS, Chainalysis, ...) — cited URLs, not LLM training
  - LLM weaves the deterministic facts + cited corpus passages into a
    short, scannable editorial brief

Surfaces:
  - analyze: lead with backing + coverage + supply lineage + relevant news
  - sanctions: lead with screening verdict + SDN freshness + relevant news
  - redemption: lead with liquid coverage + redemption mechanism + news

Cached server-side — every user gets the same brief for the same token
within the freshness window. The personal-feel comes from real-time
relevance: the corpus is daily-discovered, so "latest news" is genuinely
fresh.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sca.config import Stablecoin
from sca.llm import LLMClient
from sca.llm.fallback import fallback_llm
from sca.models import CorpusPassage
from sca.observability import log_event, timed


@dataclass
class AiBrief:
    """Editorial brief at the top of a view. Stays distinct from a
    deterministic figure — the UI must render it under a clearly-tagged
    'DORÉ BRIEF' header so an auditor can never confuse it with a check."""
    surface: str            # 'analyze' | 'sanctions' | 'redemption'
    symbol: str
    headline: str           # one-line bottom-line, e.g. "USDC: fully backed, screening clean, multi-chain supply corroborated"
    key_points: list[str] = field(default_factory=list)
    relevant_news: list[dict] = field(default_factory=list)  # [{title, url, source, date}]
    citations: list[str] = field(default_factory=list)
    confidence: str = "training-data-only"
    generated_at: str = ""


_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "relevant_news_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Indices into the news_candidates list (provided in the "
                "prompt) for items the LLM judges relevant to this token; "
                "if none are relevant, return []."
            ),
        },
    },
    "required": ["headline", "key_points"],
}


def _brief_system_prompt(surface: str) -> str:
    surface_label = {
        "analyze": "ATTESTATION ANALYSIS",
        "sanctions": "SANCTIONS SCREEN",
        "redemption": "REDEMPTION CAPACITY",
    }.get(surface, surface.upper())
    return (
        f"You are writing the DORÉ BRIEF — a single editorial paragraph "
        f"at the top of a {surface_label} view for a stablecoin "
        f"compliance tool. Goal: a financial reader skims this in 5 "
        f"seconds and knows where the token stands.\n\n"
        f"STRICT RULES:\n"
        f"  1. NEVER invent numeric figures — use only figures from the "
        f"facts block I give you, verbatim.\n"
        f"  2. NEVER claim a token is 'safe' or 'fully backed' as "
        f"settled fact. Describe what the verification SHOWS; let the "
        f"reader conclude.\n"
        f"  3. The HEADLINE is a single sentence — the bottom line. "
        f"Plain English, no jargon, no engineer slugs. If something is "
        f"missing/uncertain, lead with that. **Do NOT simply restate "
        f"'no attestation available' as the bottom line — that's a "
        f"deterministic field already shown elsewhere. Instead, name "
        f"the backing model, the protocol's transparency mechanism, "
        f"and what a reader should actually look at.** For algorithmic "
        f"or crypto-collateralized tokens, the headline should describe "
        f"the on-chain backing mechanism (e.g. 'USDD: algorithmic peg "
        f"with TRX over-collateralization; reserves visible on the TRON "
        f"DAO Reserve dashboard rather than via a CPA attestation').\n"
        f"  4. KEY_POINTS are 2-4 short bullets (~12 words each) — the "
        f"figures that matter most for THIS surface, with their "
        f"provenance compressed (e.g. 'Coverage 100.1% from Apr 30 "
        f"attestation' or 'Supply $50B across 6 chains, 4 cross-RPC "
        f"corroborated'). For non-fiat-backed tokens, at least one "
        f"bullet should describe the on-chain redemption / collateral "
        f"mechanism with a link to the protocol's dashboard.\n"
        f"  5. RELEVANT_NEWS_INDICES: from the news_candidates list, "
        f"return ONLY indices of items genuinely relevant to this "
        f"token, issuer, or surface (e.g. OFAC action against this "
        f"issuer's stablecoin, FSB guidance affecting this backing "
        f"model). Return [] if none are relevant.\n"
        f"  6. Return strict JSON matching the schema.\n"
        f"  7. Headlines and bullets should be plainly readable to "
        f"non-engineers — financial-product copy quality.\n"
    )


def _backing_model_brief(model: str) -> str:
    """Plain-English description of a backing model. Single source of
    truth shared with `sca.augment._backing_model_brief`."""
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
            "protocol dashboard, not via a CPA attestation.",
        "new_or_unverified":
            "Recently launched. No mature published attestation system "
            "yet; treat any backing claim with caution until an "
            "independent attestation appears.",
    }.get(model, "")


def _brief_news_block(coin: Stablecoin) -> str:
    """Live news snippets for the brief — gated on SCA_AUGMENT_WEB and a
    configured search provider. Empty when off; never raises."""
    import os
    if os.environ.get("SCA_AUGMENT_WEB", "").strip().lower() not in (
        "1", "true", "on",
    ):
        return ""
    try:
        from sca.web_discovery import recent_news_snippets
        snippets = recent_news_snippets(
            coin.symbol, issuer=coin.issuer, kind="general", limit=3,
        )
    except Exception:  # noqa: BLE001 - news is best-effort
        return ""
    if not snippets:
        return ""
    lines = [
        "",
        "## Live news snippets (factual context — cite URLs from here, "
        "never invent them; do NOT state numeric figures from these "
        "snippets):",
    ]
    for i, s in enumerate(snippets, 1):
        lines.append(
            f"  [n{i}] {s.get('title', '')} ({s.get('age', '')})\n"
            f"        {s.get('snippet', '')}\n"
            f"        URL: {s.get('url', '')}"
        )
    return "\n".join(lines)


def _format_news_candidates(passages: list[CorpusPassage]) -> str:
    """Render corpus passages as a numbered candidate list for the LLM."""
    if not passages:
        return "(no news candidates available)"
    lines = []
    for i, p in enumerate(passages):
        snippet = (p.text or "").strip().replace("\n", " ")[:200]
        lines.append(f"[{i}] {p.heading or p.citation}\n    {snippet}")
    return "\n".join(lines)


def generate_brief(
    *,
    surface: str,
    coin: Stablecoin,
    facts: str,
    news_candidates: list[CorpusPassage],
    llm: LLMClient | None = None,
) -> AiBrief | None:
    """Generate a single AI Brief from facts + corpus-recent-news.

    Returns None on any failure — UI omits the brief panel rather than
    rendering placeholder. The pipeline never breaks on brief failure.
    """
    client = llm or fallback_llm()
    if client is None:
        return None

    system = _brief_system_prompt(surface)
    backing_brief = _backing_model_brief(coin.backing_model)
    is_non_fiat = coin.backing_model != "fiat_reserves"
    non_fiat_steer = (
        "\n\nIMPORTANT — this is a NON-FIAT-BACKED token: a CPA-style "
        "attestation is not the right artefact and 'no attestation' is "
        "not a finding. Describe the actual backing mechanism (on-chain "
        "collateral / delta-neutral hedges / algorithmic peg) and point "
        "the reader at the live transparency surface."
        if is_non_fiat else ""
    )
    user = (
        f"Token: {coin.symbol} ({coin.name})\n"
        f"Issuer: {coin.issuer}\n"
        f"Backing model: {coin.backing_model} — {backing_brief}\n"
        f"Protocol URL: {coin.protocol_url or '(none)'}\n"
        f"Transparency URL: {coin.transparency_url or '(none)'}\n"
        f"{non_fiat_steer}\n\n"
        f"## Deterministic facts (verbatim — never invent figures)\n"
        f"{facts}\n\n"
        f"## news_candidates (corpus passages — recently discovered "
        f"regulatory / industry items)\n"
        f"{_format_news_candidates(news_candidates)}"
        f"{_brief_news_block(coin)}\n\n"
        f"Return the editorial brief as strict JSON per the schema."
    )

    try:
        with timed("brief.generate", surface=surface, symbol=coin.symbol):
            raw = client.extract_json(
                system=system, prompt=user, schema=_BRIEF_SCHEMA,
            )
    except Exception as exc:  # noqa: BLE001 - brief is optional, never raises
        log_event("brief.failed", level="warn",
                  surface=surface, symbol=coin.symbol,
                  error_class=type(exc).__name__, error_message=str(exc))
        return None

    data = raw if isinstance(raw, dict) else {}
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

    headline = (data.get("headline") or "").strip()
    if not headline:
        return None
    key_points = [
        p.strip() for p in (data.get("key_points") or [])
        if isinstance(p, str) and p.strip()
    ]
    # The LLM returns indices into news_candidates — resolve to dicts.
    relevant_news: list[dict] = []
    cites: list[str] = []
    for i in (data.get("relevant_news_indices") or []):
        try:
            idx = int(i)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(news_candidates):
            p = news_candidates[idx]
            url = _passage_url(p)
            relevant_news.append({
                "title": p.heading or p.citation,
                "source": p.source_id,
                "url": url,
                "snippet": (p.text or "")[:160],
            })
            if url:
                cites.append(url)

    from datetime import datetime, timezone
    return AiBrief(
        surface=surface,
        symbol=coin.symbol,
        headline=headline,
        key_points=key_points,
        relevant_news=relevant_news,
        citations=cites,
        confidence="training-data-only",
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _passage_url(p: CorpusPassage) -> str:
    """Resolve the source URL for a passage, via the source registry."""
    try:
        from sca.corpus.sources import get_source
        return get_source(p.source_id).url
    except Exception:  # noqa: BLE001 - cite-as-best-effort
        return ""
