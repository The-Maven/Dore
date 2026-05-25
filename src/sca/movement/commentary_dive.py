"""Commentary DIVE — structured deeper-layer analysis.

The shallow Commentary card answers "what is this token, and what's
happening right now?" in 2-3 sentences. The DIVE answers the next set
of questions a serious reader has: "what do those technical phrases
actually MEAN, what's the P&L lens in detail, and what's the loss
tail?"

Implementation:
  • Pulls the existing Commentary (headline + body + citations + pl_lens)
    so the dive can quote the original text verbatim and unpack it.
  • LLM is asked to produce STRUCTURED output (JSON schema) so the
    UI can render it as a typed view, not free-text markdown that
    could surface inconsistent formatting.
  • Cached on disk by (symbol, commentary_hash). When the upstream
    commentary changes (regime bucket flip, hand-edited cheat sheet),
    the cache key changes and the LLM is re-asked.
  • Deterministic fallback when the LLM is unavailable — never
    blocks the card.

Structured shape returned:
{
  "symbol": "USDS",
  "intro": "Short paragraph framing the deeper read.",
  "sections": [
    {
      "heading": "Breaking down the concepts",
      "items": [
        {
          "quote": "the bit from the commentary being unpacked",
          "explanations": [
            {"label": "The Token", "body": "..."},
            {"label": "The PSM", "body": "..."}
          ]
        }
      ]
    },
    { "heading": "The P&L lens", ...},
    { "heading": "Loss tail", ...}
  ],
  "citations": [{"n": 1, "url": "...", "label": "..."}],
  "fallback": false,
  "generated_at": "2026-05-25T18:42:00+00:00",
  "inputs_hash": "abcd1234"
}
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sca.config import DATA_DIR
from sca.observability import log_event


DIVE_DIR: Path = DATA_DIR / "commentary_dive"
DIVE_DIR.mkdir(parents=True, exist_ok=True)
_DIVE_LOCK = threading.Lock()


_SYSTEM_PROMPT = (
    "You are a senior stablecoin desk analyst writing a 'deeper read' "
    "card for a professional investor who has already seen the "
    "two-sentence summary. Your job is to UNPACK the dense terms in "
    "that summary (PSM, NAV, cone, watchlist signal, etc.) into "
    "plain-English explanations a smart non-specialist can follow. "
    "Cite the same [n] references the summary cites — do not invent "
    "new ones. Be concrete and specific; quote the original wording "
    "before unpacking it. No hype. No hedge-and-flatter. Return "
    "ONLY valid JSON matching the requested schema."
)


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "quote": {"type": "string"},
                                "explanations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string"},
                                            "body": {"type": "string"},
                                        },
                                        "required": ["label", "body"],
                                    },
                                },
                            },
                            "required": ["explanations"],
                        },
                    },
                },
                "required": ["heading", "items"],
            },
        },
    },
    "required": ["intro", "sections"],
}


@dataclass
class CommentaryDive:
    symbol: str
    intro: str
    sections: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    fallback: bool = False
    generated_at: str = ""
    inputs_hash: str = ""


def _fingerprint(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _dive_path(symbol: str, fp: str) -> Path:
    return DIVE_DIR / f"dive_{symbol.upper()}_{fp}.json"


def _llm_extract_json(prompt: str) -> Optional[dict]:
    """Call the LLM with the dive prompt and parse its JSON response.
    Returns None on any failure — voice never blocks the card."""
    try:
        from sca.llm import get_llm
        client = get_llm(tier="fast")
        data = client.extract_json(
            system=_SYSTEM_PROMPT,
            prompt=prompt,
            schema=_RESPONSE_SCHEMA,
            max_tokens=2500,
        )
        if not isinstance(data, dict):
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        log_event(
            "commentary_dive.llm_failed", level="warn",
            error_class=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        return None


def _build_prompt(*,
                   symbol: str,
                   headline: str,
                   body: str,
                   pl_lens: str,
                   citations: list,
                   live: dict,
                   ctx_extras: dict) -> str:
    cite_lines = []
    for c in citations or []:
        cite_lines.append(
            f"  [{c.get('n')}] {c.get('label', '')} — {c.get('url', '')}"
        )
    cite_block = "\n".join(cite_lines) if cite_lines else "  (none)"

    extras = []
    for k, v in (ctx_extras or {}).items():
        if v not in (None, "", []):
            extras.append(f"  - {k}: {v}")
    extras_block = "\n".join(extras) if extras else "  (none)"

    live_lines = []
    if live.get("current_bps") is not None:
        live_lines.append(
            f"  - current peg deviation: {live['current_bps']:+.2f}bp")
    if live.get("cone_p80_bps") is not None:
        live_lines.append(
            f"  - 80% forecast cone half-width: ±{live['cone_p80_bps']:.2f}bp")
    live_block = "\n".join(live_lines) if live_lines else "  (no live state)"

    return f"""Token under analysis: {symbol}

The shallow commentary card (what a reader sees first):
HEADLINE: {headline}

BODY:
{body}

P&L LENS:
{pl_lens or '(none provided)'}

CITATIONS available (use these [n] markers verbatim; do NOT invent
new numbers):
{cite_block}

Live state right now:
{live_block}

Structural context:
{extras_block}

Produce the deeper-read JSON. Required sections in order:

1. "intro" — 1 short paragraph (2 sentences max) framing what the
   deeper read is going to unpack. Mention the token's status
   (quiet / elevated / disputed) based on the live cone half-width
   (less than 5bp = quiet, 5-15bp = elevated, more than 15bp =
   widened).

2. First section "heading": "Breaking down the concepts".
   Pick the 2-3 most jargon-dense phrases from the BODY (PSM, NAV,
   cone half-width, watchlist signal, regime, etc.) and create one
   item per phrase. Each item:
     - "quote": the EXACT phrase or short clause from the body
     - "explanations": 2-3 labelled bullets like
         {{"label": "The token", "body": "..."}},
         {{"label": "The PSM", "body": "..."}},
       Each body is one tight paragraph (1-3 sentences).
   Preserve [n] citations where they apply.

3. Second section "heading": "The P&L lens".
   "items" has ONE entry whose "quote" is the original pl_lens text.
   Unpack what holders gain or lose: distinguish passive holding from
   yield-bearing wrappers (if any), and name the realistic upside/
   downside. Use specific numbers from the live state when relevant.

4. Third section "heading": "Loss tail".
   "items" has ONE entry. Quote the loss-tail clause from the body
   (or write "absolute worst case for this token:" if no quote
   exists). Then name 2-3 concrete failure modes — protocol risk,
   redemption stress, RWA legal risk, etc. Be specific to this
   token's backing model, not generic.

Hard rules:
- Return ONLY JSON matching the schema. No prose before or after.
- Preserve [n] citation markers exactly as they appear in the source.
- Do not invent figures. If a number isn't in the inputs above, do
  not include one.
- Quote text VERBATIM when using the "quote" field — no paraphrasing.
"""


def _deterministic_fallback(*,
                              symbol: str,
                              headline: str,
                              body: str,
                              pl_lens: str,
                              citations: list,
                              live: dict) -> CommentaryDive:
    """Skeleton output when the LLM is unavailable. Honest about
    being a placeholder so a reader knows to retry rather than
    treating it as the analyst's full read."""
    cone = live.get("cone_p80_bps")
    regime = (
        "quiet" if cone is not None and cone < 5
        else "elevated" if cone is not None and cone < 15
        else "widened" if cone is not None
        else "unknown"
    )
    intro = (
        f"Deeper read on {symbol} unavailable — the analyst model is "
        f"offline. The live regime is {regime}; consult the shallow "
        f"commentary above for the structural framing."
    )
    return CommentaryDive(
        symbol=symbol,
        intro=intro,
        sections=[{
            "heading": "Breaking down the concepts",
            "items": [{
                "quote": (body or "").split(".")[0][:200],
                "explanations": [{
                    "label": "Engine offline",
                    "body": (
                        "The deeper-unpack layer needs the LLM to "
                        "expand the technical phrases in plain English. "
                        "Retry the dive when the model is back."
                    ),
                }],
            }],
        }],
        citations=citations or [],
        fallback=True,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        inputs_hash="",
    )


def generate_dive(*,
                    symbol: str,
                    headline: str,
                    body: str,
                    pl_lens: str,
                    citations: list,
                    live: dict,
                    ctx_extras: dict,
                    force_refresh: bool = False) -> CommentaryDive:
    """Return a cached or freshly-generated dive. Cached by a hash
    over the commentary inputs — when the regime bucket flips or the
    cheat sheet is edited, the cache key changes and the LLM is
    re-asked. Otherwise the cached file is returned in O(1)."""
    fp = _fingerprint({
        "symbol": symbol, "headline": headline, "body": body,
        "pl_lens": pl_lens, "citations": citations,
    })
    path = _dive_path(symbol, fp)
    if not force_refresh:
        # Store first (v5.1 durable cache)
        try:
            from sca.store import get_store
            row = get_store().get_commentary_dive(symbol, fp)
            if row:
                return CommentaryDive(
                    symbol=row.get("symbol", symbol),
                    intro=row.get("intro", "") or "",
                    sections=row.get("sections") or [],
                    citations=row.get("citations") or [],
                    fallback=bool(row.get("fallback", False)),
                    generated_at=str(row.get("generated_at", "")),
                    inputs_hash=fp,
                )
        except Exception:  # noqa: BLE001
            pass
    with _DIVE_LOCK:
        if path.exists() and not force_refresh:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                return CommentaryDive(**cached)
            except (OSError, json.JSONDecodeError, TypeError):
                pass

    prompt = _build_prompt(
        symbol=symbol, headline=headline, body=body, pl_lens=pl_lens,
        citations=citations, live=live, ctx_extras=ctx_extras,
    )
    data = _llm_extract_json(prompt)
    if not data:
        return _deterministic_fallback(
            symbol=symbol, headline=headline, body=body,
            pl_lens=pl_lens, citations=citations, live=live,
        )

    dive = CommentaryDive(
        symbol=symbol,
        intro=str(data.get("intro", "")).strip(),
        sections=data.get("sections", []) or [],
        citations=citations or [],
        fallback=False,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        inputs_hash=fp,
    )
    with _DIVE_LOCK:
        try:
            path.write_text(json.dumps(asdict(dive), indent=2),
                            encoding="utf-8")
        except OSError as exc:
            log_event(
                "commentary_dive.cache_write_failed", level="warn",
                symbol=symbol, error_class=type(exc).__name__,
            )
    # v5.1: durable copy in the store
    try:
        from sca.store import get_store
        get_store().upsert_commentary_dive(asdict(dive))
    except Exception:  # noqa: BLE001
        pass
    return dive
