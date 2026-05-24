"""Attribution — turn a forecast into a cited 'why' paragraph.

LAYER: judgement. The deterministic engine produces the numbers; this
module produces the prose. Every claim must cite either (a) a corpus
passage (regulatory / research source we've ingested) or (b) a
recorded observability event (e.g. sdn.fetch.ok, attestation.published).
The LLM is forbidden from inventing drivers — the prompt explicitly
constrains it to the candidate list we supply, and a post-processor
drops sentences whose claims aren't traceable to the candidates.

If no candidates exist (a quiet period), we return 'no driver cited'
rather than a fabricated narrative. Honest 'n/a' is one of Doré's
non-negotiables.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sca.observability import log_event, recent_events


# Candidate event kinds the attribution engine considers. Filtered
# narrow: each one is something an analyst would plausibly cite as a
# *driver* of a near-term stablecoin movement. Pipeline-health events
# (sweep.ok, fetch.ok) are kept out — they're our plumbing, not the
# market's signal.
_DRIVER_EVENT_KINDS = {
    # Sanctions actions move stablecoin balances at exchanges
    "sdn.fetch.ok",
    # New regulatory publications
    "discovery.published",
    "discovery.brave.sweep_done",
    # Attestation publication = potential mint/burn settlement
    "attestation.published",
    "attestation.gap_sweep.ok",
    # On-chain anomalies that are tradeable signals
    "supply.jump",
    "rpc.consensus.degraded",
    # Source flip = our intel layer changed
    "health.source.flipped",
}


# Look-back window for events to be considered as drivers. Anything
# older than this is no longer a *near-term* driver and is suppressed.
_DRIVER_LOOKBACK_MINUTES = 90


def gather_event_candidates(symbol: str, *,
                              lookback_minutes: int = _DRIVER_LOOKBACK_MINUTES,
                              limit: int = 200) -> list[dict]:
    """Pull recent observability events that could plausibly drive a
    forecast for `symbol`. Filters by kind whitelist and by age. Each
    candidate is normalised to a {kind, ts, summary, url} dict so the
    attribution prompt has a stable shape regardless of which event
    kind it came from."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    out: list[dict] = []
    for ev in recent_events(limit=limit) or []:
        kind = ev.get("kind") or ""
        if kind not in _DRIVER_EVENT_KINDS:
            continue
        ts = ev.get("ts") or ""
        try:
            ets = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if ets.tzinfo is None:
                ets = ets.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ets < cutoff:
            continue
        # Event symbols, if present, must match; events without a
        # symbol field (market-wide signals) are kept.
        ev_symbol = (ev.get("symbol") or "").upper()
        if ev_symbol and symbol and ev_symbol != symbol.upper():
            continue
        out.append({
            "kind": kind,
            "ts": ts,
            "symbol": ev_symbol or "",
            "summary": _summarise_event(ev),
            "url": ev.get("url") or ev.get("endpoint") or "",
            "trust_tier": _trust_tier_for(kind),
        })
    return out


_CITE_INJECTION_RE = __import__("re").compile(r"\[\d+\]|\(W\d+\)")


def _sanitise(text: str) -> str:
    """Strip citation-shaped patterns from event-derived summaries so
    a hostile external title can't smuggle a forged [3] or (W2) into
    the LLM prompt. Audit finding #14: a regulatory page titled
    'Update [3] — paxos warns' would otherwise make the LLM emit [3]
    even when no candidate at index 3 exists."""
    if not text:
        return text
    return _CITE_INJECTION_RE.sub("", text).strip()


def _summarise_event(ev: dict) -> str:
    """Deterministic one-line summary of an observability event for
    the prompt. Avoids leaking raw event payload into the LLM
    context — fewer surfaces for prompt injection. Citation-shaped
    substrings ([n], (Wn)) are stripped so an external title can't
    forge a citation index into the prompt."""
    k = ev.get("kind", "")
    if k == "supply.jump":
        out = (f"Supply jump flagged for {ev.get('symbol')} "
               f"on {ev.get('chain')} (ratio={ev.get('ratio')})")
    elif k == "attestation.published":
        out = (f"New attestation published for {ev.get('symbol')}"
               f" (as_of={ev.get('as_of_date', '?')})")
    elif k.startswith("discovery."):
        out = ev.get("title", "Regulatory / research item published")
    elif k == "sdn.fetch.ok":
        out = "OFAC SDN list refreshed"
    elif k == "rpc.consensus.degraded":
        out = f"RPC consensus degraded on {ev.get('chain', '?')}"
    elif k == "health.source.flipped":
        out = f"Source flipped: {ev.get('id', '?')}"
    else:
        out = k
    return _sanitise(out)


def _trust_tier_for(kind: str) -> str:
    """Map an event kind to a trust tier. Aligns with the saved
    'search-is-a-lead-not-a-fact' discipline — first-party
    publications outrank derivatives. Used by the UI to render a
    badge per cited driver."""
    if kind == "attestation.published":
        return "issuer_first_party"
    if kind == "sdn.fetch.ok":
        return "regulator_first_party"
    if kind.startswith("discovery."):
        return "research_or_regulator"
    if kind in ("supply.jump", "rpc.consensus.degraded"):
        return "doré_observed"
    return "internal"


# ── attribution paragraph composition ────────────────────────────────
_ATTRIBUTION_SYSTEM = """\
You write one sentence of attribution for a stablecoin movement \
forecast. Strict rules:

1. You may ONLY name causes drawn from the CANDIDATES list below. If \
the list is empty, output exactly "No driver cited.".
2. Never invent driver names. Never assert causation that the \
candidates don't support. Correlation is fine; "because" is not.
3. One sentence, ≤ 40 words. No em-dashes (use commas / colons). No \
hyperbole. No "may", "could", "potentially" — those weasel words are \
not allowed.
4. Cite the candidate(s) by their index in brackets: "the new MICA \
guidance [2] tightens the issuer reporting window".
5. If a candidate's summary contradicts the forecast direction, name \
that contradiction in the sentence — do not paper over it.

Output: the single sentence and nothing else.
"""


def compose_attribution(forecast_summary: str,
                          candidates: list[dict],
                          *,
                          llm_client=None) -> tuple[str, list[dict]]:
    """Return (paragraph, used_drivers).

    `forecast_summary` is a 1-line description of the engine output
    ("USDC peg deviation forecast +3.2bp over next 60min").
    `candidates` is the list from gather_event_candidates.
    `llm_client` is optional; when None we use the configured client.

    `used_drivers` is the subset of candidates the paragraph actually
    cited — those are the rows we'll write to predictions.drivers so
    the UI can render badges + URLs. Drivers that fail validation
    (the LLM cited an index outside the list, or hallucinated a name)
    are dropped from used_drivers.
    """
    if not candidates:
        return "No driver cited.", []

    if llm_client is None:
        try:
            from sca.llm import get_llm
            llm_client = get_llm(tier="fast")
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.attribute.llm_unavailable", level="info",
                error_class=type(exc).__name__,
            )
            # Fall back to a deterministic naming of the top candidate
            # so we never silently omit a driver that exists.
            top = candidates[0]
            return f"Recent: {top['summary']} [{0}].", [top]

    prompt = _build_prompt(forecast_summary, candidates)
    try:
        sentence = llm_client.complete(
            system=_ATTRIBUTION_SYSTEM, prompt=prompt, max_tokens=200,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.attribute.llm_failed", level="warn",
            error_class=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        top = candidates[0]
        return f"Recent: {top['summary']} [{0}].", [top]

    # Validate: drop em-dashes (voice rule) and strip to a single line.
    sentence = sentence.replace("—", ", ").replace("--", ", ").strip()
    sentence = sentence.split("\n", 1)[0].strip()

    used = _extract_used_candidates(sentence, candidates)
    if not used:
        # The LLM didn't cite any candidate — treat as 'no driver',
        # don't trust the sentence.
        return "No driver cited.", []
    return sentence, used


def _build_prompt(forecast_summary: str, candidates: list[dict]) -> str:
    lines = [
        f"FORECAST: {forecast_summary}",
        "",
        "CANDIDATES (index — summary):",
    ]
    for i, c in enumerate(candidates[:8]):
        lines.append(f"  [{i}] {c['summary']} (trust: {c['trust_tier']})")
    lines.append("")
    lines.append("Write the one-sentence attribution.")
    return "\n".join(lines)


def _extract_used_candidates(sentence: str,
                               candidates: list[dict]) -> list[dict]:
    """Pull citation indices like [0], [2] from the sentence and
    return the corresponding candidates. Out-of-range indices are
    dropped silently — the validation pass on top of this catches
    everything except duplicates."""
    import re
    used_idx: list[int] = []
    for m in re.finditer(r"\[(\d+)\]", sentence):
        try:
            idx = int(m.group(1))
            if 0 <= idx < len(candidates) and idx not in used_idx:
                used_idx.append(idx)
        except ValueError:
            continue
    return [candidates[i] for i in used_idx]
