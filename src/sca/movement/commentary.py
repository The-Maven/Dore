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
                    llm_client=None, force: bool = False,
                    wait_for_llm: bool = True) -> Optional[Commentary]:
    """Return a Commentary for `symbol`. `live` is the current
    forecast snippet: current_bps, cone_p80_bps, d1h.

    Cache key includes the symbol AND a coarse bucket of the live
    cone width so the cached body stays valid as long as the regime
    hasn't shifted. Forces a fresh LLM call when `force=True`.

    `wait_for_llm=False` (the UX-fast path): return the cached entry
    if ANY exists for this symbol (regardless of regime), else the
    deterministic fallback — and schedule a background LLM refresh
    so the next read is properly warm. This kills the "loading…"
    UX without sacrificing the LLM-grounded body.
    """
    ctx = get_context(symbol)
    if ctx is None:
        return None

    # Defensive: a malformed registry entry could leave
    # cone_thresholds_bps as None or a single-element tuple. The old
    # code indexed [0] and [1] unguarded and crashed inside a bare
    # except clause, treating the failure as a cache miss. Fail
    # closed: log + return None so the caller can fall through to
    # the deterministic message rather than burn an LLM call.
    cone_thr = getattr(ctx, "cone_thresholds_bps", None)
    if not cone_thr or len(cone_thr) < 2:
        log_event(
            "movement.commentary.malformed_context", level="warn",
            symbol=symbol,
            thresholds=str(cone_thr)[:80] if cone_thr else "None",
        )
        return None

    # Coarse cache key: symbol + regime bucket.
    cone_w = live.get("cone_p80_bps") or 0
    regime = ("tight" if cone_w < cone_thr[0]
              else "alert" if cone_w > cone_thr[1]
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

    # UX-fast path: serve any-regime cached entry instantly + schedule
    # a background refresh for the right regime.
    if not wait_for_llm and not force:
        any_entry = _find_any_cached(cache, symbol)
        if any_entry is not None:
            try:
                cached = Commentary(**any_entry["payload"])
                log_event(
                    "movement.commentary.served_stale", level="info",
                    symbol=symbol, target_regime=regime,
                    age_s=int(now - any_entry.get("ts", 0)),
                )
                _schedule_background_refresh(symbol, live)
                return cached
            except Exception:  # noqa: BLE001
                pass
        # No cache at all — return deterministic immediately + schedule
        # a real LLM warm-up for the next focus.
        _schedule_background_refresh(symbol, live)
        log_event(
            "movement.commentary.served_deterministic", level="info",
            symbol=symbol, regime=regime,
        )
        return _deterministic_fallback(ctx, live)

    # Fresh LLM call (slow path, retained for force=True and tests).
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
    cheat sheet alone, names the gap clearly.

    Includes the P&L lens (pl_lens) when set — short hedged framing
    on what holders gain or lose, so the deterministic card is still
    useful for first-time readers."""
    cone_w = live.get("cone_p80_bps")
    cone_part = (f"current 80% cone {cone_w:.1f}bp" if cone_w is not None
                 else "live cone unavailable")
    yld_tag = ""
    if getattr(ctx, "yield_bearing", False):
        yld_tag = (" Note: this token is yield-bearing and drifts above "
                   "$1.00 by design — the peg-deviation lens does not "
                   "apply.")
    pl = getattr(ctx, "pl_lens", "") or ""
    pl_part = f" P&L lens: {pl}" if pl else ""
    body = (
        f"{ctx.structural_one_liner} "
        f"Today's read: {cone_part}.{yld_tag} "
        f"Watchlist signal: {ctx.watchlist_signal}.{pl_part} "
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


# ── UX-fast helpers ─────────────────────────────────────────────────
def _find_any_cached(cache: dict, symbol: str) -> dict | None:
    """Find any cached entry for `symbol` regardless of regime bucket.
    Returns the most-recent entry; used for stale-while-revalidate."""
    sym_l = symbol.lower()
    best = None
    best_ts = 0.0
    for k, v in cache.items():
        # key shape is 'SYMBOL::regime'
        key_sym = k.split("::", 1)[0]
        if key_sym == symbol or key_sym.lower() == sym_l:
            ts = v.get("ts") or 0.0
            if ts > best_ts:
                best_ts = ts
                best = v
    return best


_REFRESH_INFLIGHT: set[str] = set()
_REFRESH_LOCK = None
# Exponential back-off on background refresh per symbol. When the LLM
# is unavailable or returns empty repeatedly (e.g. no API key, rate-
# limit, model server hiccup), we'd otherwise re-fire `parse_failed`
# every chip-click — wasted compute + log noise. After a failure we
# refuse to schedule another refresh for this symbol until a cool-down
# elapses; the deterministic fallback continues to serve the UI in
# the meantime.
#
# Bounded growth discipline: these dicts are keyed by symbol. A
# config refactor or transient typo could leak entries that never
# clear. We evict any entry whose next_ok_at is more than 2× the
# max backoff window in the past — at that point the entry is stale
# observability noise, no longer governing any decision.
_REFRESH_NEXT_OK_AT: dict[str, float] = {}
_REFRESH_BACKOFF_FAILURES: dict[str, int] = {}
_REFRESH_BASE_BACKOFF_S = 30.0     # first failure: 30s of quiet
_REFRESH_MAX_BACKOFF_S = 600.0     # cap at 10 minutes
_REFRESH_STATE_MAX_SYMBOLS = 200   # hard cap as a second-line bound


def _evict_stale_backoff_entries(now: float) -> None:
    """Remove backoff entries whose cooldown elapsed long enough ago
    that they no longer govern decisions. Caller holds _REFRESH_LOCK.
    Also enforces a hard symbol cap so a misconfiguration can't bloat
    these dicts in production."""
    stale_after = now - (2 * _REFRESH_MAX_BACKOFF_S)
    stale = [
        sym for sym, t in _REFRESH_NEXT_OK_AT.items()
        if t < stale_after
    ]
    for sym in stale:
        _REFRESH_NEXT_OK_AT.pop(sym, None)
        _REFRESH_BACKOFF_FAILURES.pop(sym, None)
    # Hard cap. Drop the oldest entries if we somehow grew past the
    # bound (e.g. a flood of one-off failures from many symbols).
    if len(_REFRESH_NEXT_OK_AT) > _REFRESH_STATE_MAX_SYMBOLS:
        ordered = sorted(_REFRESH_NEXT_OK_AT.items(), key=lambda kv: kv[1])
        for sym, _ in ordered[:-_REFRESH_STATE_MAX_SYMBOLS]:
            _REFRESH_NEXT_OK_AT.pop(sym, None)
            _REFRESH_BACKOFF_FAILURES.pop(sym, None)


def _schedule_background_refresh(symbol: str, live: dict) -> None:
    """Kick off a background thread to populate the proper-regime
    cache entry. Idempotent — if a refresh is already in flight for
    this symbol, do nothing. Also honours an exponential back-off
    when the previous refresh failed, so a misconfigured LLM never
    spams the observability surface with retry noise.

    The thread is daemon so it does not prevent process exit during
    tests / shutdown."""
    import threading
    import time as _t
    global _REFRESH_LOCK
    if _REFRESH_LOCK is None:
        _REFRESH_LOCK = threading.Lock()
    now = _t.time()
    with _REFRESH_LOCK:
        # Opportunistic eviction — cheap dict scan, only runs when a
        # refresh is requested. Keeps the backoff state bounded
        # without a separate cleanup thread.
        _evict_stale_backoff_entries(now)
        if symbol in _REFRESH_INFLIGHT:
            return
        next_ok = _REFRESH_NEXT_OK_AT.get(symbol, 0.0)
        if now < next_ok:
            # Still in back-off. Don't schedule and don't log — the
            # caller is the foreground UX-fast path which has already
            # served a usable result.
            return
        _REFRESH_INFLIGHT.add(symbol)

    def _worker():
        ok = False
        try:
            # Call the synchronous LLM path; populates the cache on success.
            ctx = get_context(symbol)
            if ctx is None:
                return
            cone_w = live.get("cone_p80_bps") or 0
            regime = ("tight" if cone_w < ctx.cone_thresholds_bps[0]
                      else "alert" if cone_w > ctx.cone_thresholds_bps[1]
                      else "normal")
            cache_key = f"{symbol}::{regime}"
            cache_before = _load_cache()
            entry_before = cache_before.get(cache_key)
            ts_before = (entry_before or {}).get("ts", 0)
            get_commentary(symbol, live, force=True, wait_for_llm=True)
            cache_after = _load_cache()
            entry_after = cache_after.get(cache_key)
            ts_after = (entry_after or {}).get("ts", 0)
            # If a new cache entry landed in the right regime bucket,
            # the LLM call succeeded. Otherwise the request returned a
            # deterministic fallback (no cache write).
            ok = ts_after > ts_before
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.commentary.background_refresh_failed",
                level="warn", symbol=symbol,
                error_class=type(exc).__name__,
            )
            ok = False
        finally:
            with _REFRESH_LOCK:
                _REFRESH_INFLIGHT.discard(symbol)
                if ok:
                    _REFRESH_BACKOFF_FAILURES.pop(symbol, None)
                    _REFRESH_NEXT_OK_AT.pop(symbol, None)
                else:
                    n = _REFRESH_BACKOFF_FAILURES.get(symbol, 0) + 1
                    _REFRESH_BACKOFF_FAILURES[symbol] = n
                    delay = min(
                        _REFRESH_BASE_BACKOFF_S * (2 ** (n - 1)),
                        _REFRESH_MAX_BACKOFF_S,
                    )
                    _REFRESH_NEXT_OK_AT[symbol] = _t.time() + delay

    t = threading.Thread(
        target=_worker, name=f"commentary-refresh-{symbol}",
        daemon=True,
    )
    t.start()
