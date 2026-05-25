"""Per-token AI Commentary — structural cheat sheet + live read.

The AI Judge synthesises the latest forecast (insight + pitch).
The Commentary card is the COMPLEMENT — a structural read explaining
what this stablecoin IS, what its peg dynamics LOOK LIKE, and how the
current delta fits the normal envelope. Updates only when the user
changes the focused token; cached 1 hour per (symbol, day) because
the structural facts barely move.

Discipline (per the SF research):
  - Verb-named disclosure: 'AI Commentary, grounded in cited sources'
  - Inline [1] [2] citations the UI renders as links
  - Hedged language ('appears to', 'suggests', NOT 'will')
  - Honest 'n/a' when the cone is too wide for narrative signal
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from sca.config import DATA_DIR
from sca.movement.token_context import get_context
from sca.observability import log_event


COMMENTARY_MODEL_VERSION = "commentary_v1"

# 1 hour TTL — structural commentary barely changes. The
# investor-relevant fields (current bps, cone width) DO update on
# every tick, but those are passed as live state into the prompt;
# the cached body is the structural part.
_CACHE_TTL_S = 3600
_CACHE_PATH = DATA_DIR / "movement_commentary_cache.json"


@dataclass
class Commentary:
    """The rendered card payload. Body is markdown-ish prose with
    [1] [2] citations; citations is the parallel list the UI uses
    to render hyperlinks under the body."""
    symbol: str
    headline: str             # one bold line: 'USDC · tight cone'
    body: str                 # one paragraph, inline-cited
    citations: list[dict]     # [{n: 1, label: '...', url: '...'}]
    structural_one_liner: str
    cone_normal_bps: float
    cone_alert_bps: float
    model: str = COMMENTARY_MODEL_VERSION


# ── cache ───────────────────────────────────────────────────────────
def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        from sca.persist import atomic_write_json
        atomic_write_json(_CACHE_PATH, cache)
    except Exception:  # noqa: BLE001
        pass


# ── prompt ──────────────────────────────────────────────────────────
_COMMENTARY_SYSTEM = """\
You write the 'AI Commentary' card for one stablecoin. The card
sits next to live peg data; it teaches a sophisticated investor
how to read that token's peg behaviour.

STRICT RULES:
  1. Output EXACTLY this JSON, no preamble:
     {"headline":"<= 8 words, bold takeaway>", \
      "body":"<= 90 words, ONE paragraph, plain English>"}
  2. Cite ONLY the URLs in CITATIONS below, by index in brackets
     like [1]. Do NOT invent sources.
  3. Use hedged language: 'appears to', 'suggests', 'historically'.
     NEVER 'will', 'guaranteed', 'certain'.
  4. Reference the structural facts AND the live cone width.
     Name a specific number when relevant ('cone at ±X bp').
  5. If the live cone is wider than the token's alert threshold,
     name the watchlist signal explicitly.
  6. No em-dashes. No 'may', 'could', 'might'.
  7. End the body with a half-sentence pointing the reader at one
     watchlist signal worth monitoring next.
"""


def _build_prompt(ctx, live: dict) -> tuple[str, list[dict]]:
    cone_w = live.get("cone_p80_bps")
    cone_str = f"±{cone_w:.1f} bp" if cone_w is not None else "n/a"
    current = live.get("current_bps")
    current_str = (f"{current:+.2f} bp" if current is not None
                   else "n/a")
    delta_1h = live.get("d1h")
    delta_str = (f"{delta_1h:+.2f} bp over 1h" if delta_1h is not None
                 else "no 1h delta available")

    citations = [
        {"n": 1, "label": f"{ctx.issuer} transparency",
         "url": ctx.transparency_url or ""},
    ]
    # Add auditor name as a second citation if available — informational
    if ctx.auditor and ctx.auditor != "—":
        citations.append({
            "n": 2, "label": f"Audited by {ctx.auditor}",
            "url": ctx.transparency_url or "",
        })

    lines = [
        f"TOKEN: {ctx.symbol} ({ctx.issuer})",
        f"BACKING: {ctx.backing_short}",
        f"BACKING MODEL: {ctx.backing_model}",
        f"CADENCE: {ctx.cadence}",
        f"AUDITOR: {ctx.auditor}",
        f"STRUCTURAL ONE-LINER (use as scaffold): {ctx.structural_one_liner}",
        f"WATCHLIST SIGNAL: {ctx.watchlist_signal}",
        f"NORMAL CONE THRESHOLD (alert ABOVE): ±{ctx.cone_thresholds_bps[0]:.1f} bp",
        f"ALERT CONE THRESHOLD: ±{ctx.cone_thresholds_bps[1]:.1f} bp",
        "",
        "LIVE READ (do not invent — cite verbatim):",
        f"  current peg deviation: {current_str}",
        f"  forecast cone (80%): {cone_str}",
        f"  movement: {delta_str}",
        "",
        "CITATIONS available:",
    ]
    for c in citations:
        lines.append(f"  [{c['n']}] {c['label']} — {c['url']}")
    lines.append("")
    lines.append("Return the JSON object now.")
    return "\n".join(lines), citations


def _parse_json_loose(raw: str) -> dict | None:
    """Tolerant JSON extraction (shared with judge.py pattern).
    Strips fences, finds outermost { ... }, returns dict or None."""
    import re
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        out = json.loads(s[start:end + 1])
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def _clean(text: str | None) -> str | None:
    """Strip em-dashes + voice rules, NFKC-normalise. Same discipline
    as judge.py — tests for these patterns in this module."""
    if not text:
        return None
    import re
    import unicodedata
    s = unicodedata.normalize("NFKC", str(text)).strip()
    s = s.replace("—", ", ").replace("--", ", ")
    s = re.sub(r"\b(may|could|might|potentially|perhaps)\b", "",
                s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ── public API ──────────────────────────────────────────────────────
def get_commentary(symbol: str, live: dict, *,
                    llm_client=None, force: bool = False) -> Optional[Commentary]:
    """Return a Commentary for `symbol`. `live` is the current
    forecast snippet: current_bps, cone_p80_bps, d1h.

    Cache key includes the symbol AND a coarse bucket of the live
    cone width so the cached body stays valid as long as the regime
    hasn't shifted. Forces a fresh LLM call when `force=True`.
    """
    ctx = get_context(symbol)
    if ctx is None:
        return None

    # Coarse cache key: symbol + regime bucket.
    cone_w = live.get("cone_p80_bps") or 0
    regime = ("tight" if cone_w < ctx.cone_thresholds_bps[0]
              else "alert" if cone_w > ctx.cone_thresholds_bps[1]
              else "normal")
    cache_key = f"{symbol}::{regime}"
    cache = _load_cache()
    now = time.time()
    entry = cache.get(cache_key)
    if entry and not force and (now - entry.get("ts", 0)) < _CACHE_TTL_S:
        try:
            cached = Commentary(**entry["payload"])
            log_event("movement.commentary.cache_hit", level="info",
                      symbol=symbol, regime=regime)
            return cached
        except Exception:  # noqa: BLE001
            pass

    # Fresh LLM call.
    if llm_client is None:
        try:
            from sca.llm import get_llm
            llm_client = get_llm(tier="fast")
        except Exception as exc:  # noqa: BLE001
            log_event("movement.commentary.llm_unavailable", level="info",
                      symbol=symbol, error_class=type(exc).__name__)
            return _deterministic_fallback(ctx, live)

    prompt, citations = _build_prompt(ctx, live)
    raw = ""
    try:
        raw = llm_client.complete(
            system=_COMMENTARY_SYSTEM, prompt=prompt, max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("movement.commentary.llm_failed", level="warn",
                  symbol=symbol, error_class=type(exc).__name__,
                  error_message=str(exc)[:200])
        return _deterministic_fallback(ctx, live)

    parsed = _parse_json_loose(raw)
    if parsed is None:
        log_event("movement.commentary.parse_failed", level="warn",
                  symbol=symbol, raw_head=str(raw)[:160])
        return _deterministic_fallback(ctx, live)

    out = Commentary(
        symbol=symbol,
        headline=_clean(parsed.get("headline")) or f"{symbol} · live read",
        body=_clean(parsed.get("body")) or ctx.structural_one_liner,
        citations=citations,
        structural_one_liner=ctx.structural_one_liner,
        cone_normal_bps=ctx.cone_thresholds_bps[0],
        cone_alert_bps=ctx.cone_thresholds_bps[1],
    )
    cache[cache_key] = {"ts": now, "payload": asdict(out)}
    _save_cache(cache)
    log_event("movement.commentary.generated", level="info",
              symbol=symbol, regime=regime, headline=out.headline)
    return out


def _deterministic_fallback(ctx, live: dict) -> Commentary:
    """Honest n/a path — the LLM is unavailable, parsing failed, or
    we want to ship cold without the LLM. Builds the card from the
    cheat sheet alone, names the gap clearly."""
    cone_w = live.get("cone_p80_bps")
    cone_part = (f"current 80% cone {cone_w:.1f}bp" if cone_w is not None
                 else "live cone unavailable")
    body = (
        f"{ctx.structural_one_liner} "
        f"Today's read: {cone_part}. "
        f"Watchlist signal: {ctx.watchlist_signal}. "
        f"For details, see [1]."
    )
    citations = [{
        "n": 1, "label": f"{ctx.issuer} transparency",
        "url": ctx.transparency_url or "",
    }]
    return Commentary(
        symbol=ctx.symbol,
        headline=f"{ctx.symbol} · {ctx.backing_model.replace('_', ' ')}",
        body=body,
        citations=citations,
        structural_one_liner=ctx.structural_one_liner,
        cone_normal_bps=ctx.cone_thresholds_bps[0],
        cone_alert_bps=ctx.cone_thresholds_bps[1],
    )
