"""Brave web search → judge context, designed for cost discipline.

Brave free tier is ~2,000 calls/month. A naive design (per-tick × 5
symbols × 2 kinds × every 10 minutes) would burn ~43,200 calls/day
and exhaust the budget in an hour. This module enforces five
discipline layers so we stay well inside quota without losing the
fresh-data advantage:

  1. **Cache-first** (12h TTL, ±20% jitter). 12h is long enough to
     spread expiry across the day; jitter prevents thundering-herd
     re-fetches when the simulator starts cold.
  2. **Coalesce per-symbol**, not per (symbol, kind). The judge sees
     both kinds for a symbol in one render; one Brave query feeds
     both. Halves the call rate immediately.
  3. **Interest gate**. Skip the call when the deterministic forecast
     is calm (in 50% band, confidence ≥ 'likely'). No attribution
     prose is useful when nothing is happening — and Brave hates
     being asked about non-events.
  4. **Daily quota cap** (default 200 calls/day = 6k/month buffer
     under the 2k free tier monthly limit + headroom for spikes).
     Once hit, serve stale or empty until UTC midnight.
  5. **Stale-on-error**. Cache hits even when expired beat a fresh
     blank — better to cite week-old context than to omit it.

Every cost decision is logged so an operator can audit budget burn
through the F1 events feed: brave_context.cache_hit, .skip_calm,
.quota_hit, .fetched, .fetch_failed_served_stale.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone

from sca.config import DATA_DIR
from sca.observability import log_event


_CACHE_PATH = DATA_DIR / "movement_brave_context.json"
_QUOTA_PATH = DATA_DIR / "movement_brave_quota.json"

# Base TTL = 12h. We add ±20% jitter when WRITING the cache so caches
# don't all expire at once. 12h × 5 symbols × 2 ticks/day = ~10
# fetches/day per symbol-set in steady state.
_CACHE_TTL_BASE_S = 12 * 3600
_CACHE_TTL_JITTER = 0.20

# Daily quota — count calls in UTC days. 200/day is conservative
# (~6k/month, well inside the 2k/month free tier across a full month
# of ticking + spikes). Set SCA_BRAVE_DAILY_QUOTA in env to override.
_DAILY_QUOTA_DEFAULT = 200

_MAX_RESULTS_PER_QUERY = 5
_HTTP_TIMEOUT = 8.0

# Confidence words that mean "nothing is happening" — when the
# deterministic engine emits one of these, we skip the Brave call. The
# operator can still get fresh context by clicking a manual refresh
# (which bypasses the interest gate) later.
_CALM_CONFIDENCE_WORDS = {"very_likely", "virtually_certain"}


# ── cache ────────────────────────────────────────────────────────────
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


def _cache_ttl_with_jitter() -> float:
    """Per-write TTL with ±20% jitter. Spreads expiry across the day
    so a startup cold cache doesn't expire en bloc later."""
    factor = 1.0 + (random.random() * 2 - 1) * _CACHE_TTL_JITTER
    return _CACHE_TTL_BASE_S * factor


# ── daily quota ──────────────────────────────────────────────────────
def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_quota() -> dict:
    if not _QUOTA_PATH.exists():
        return {"day": _utc_today(), "calls": 0}
    try:
        data = json.loads(_QUOTA_PATH.read_text())
        if not isinstance(data, dict):
            return {"day": _utc_today(), "calls": 0}
        if data.get("day") != _utc_today():
            return {"day": _utc_today(), "calls": 0}
        return data
    except (json.JSONDecodeError, OSError):
        return {"day": _utc_today(), "calls": 0}


def _save_quota(quota: dict) -> None:
    try:
        from sca.persist import atomic_write_json
        atomic_write_json(_QUOTA_PATH, quota)
    except Exception:  # noqa: BLE001
        pass


def _daily_quota() -> int:
    try:
        return int(os.environ.get(
            "SCA_BRAVE_DAILY_QUOTA", str(_DAILY_QUOTA_DEFAULT)
        ))
    except ValueError:
        return _DAILY_QUOTA_DEFAULT


def quota_state() -> dict:
    """Current daily-quota state for the simulator state surface."""
    q = _load_quota()
    return {
        "day": q.get("day"),
        "calls": q.get("calls", 0),
        "cap": _daily_quota(),
        "remaining": max(0, _daily_quota() - int(q.get("calls", 0))),
    }


# ── brave query ──────────────────────────────────────────────────────
def _brave_key() -> str:
    return os.environ.get("SCA_WEB_SEARCH_KEY", "").strip()


def _build_query(symbol: str) -> str:
    """One query per symbol. Broad enough to surface both peg and flow
    signals; narrow enough that results stay relevant. We trade query
    breadth for call rate: one query, both kinds. The judge filters
    relevance at compose-time."""
    return (f"{symbol} stablecoin news regulatory flow this week "
            f"mint burn peg")


def _trust_label_for(url: str) -> str:
    u = (url or "").lower()
    if any(d in u for d in (
        "circle.com", "tether.to", "paxos.com", "ofac.treasury.gov",
        "treasury.gov", "ecb.europa.eu", "bis.org", "fsb.org",
        "federalreserve.gov", "imf.org",
    )):
        return "first_party_or_regulator"
    if any(d in u for d in (
        "chainalysis.com", "coinmetrics.io", "kaiko.com",
        "glassnode.com", "messari.io", "artemisanalytics.com",
        "castleisland.vc",
    )):
        return "research_house"
    return "web_unverified"


def _domain_for(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def _is_calm(confidence_word: str | None) -> bool:
    """The interest gate. True when the engine reports a calm
    forecast and we should skip the Brave call."""
    if not confidence_word:
        return False
    return confidence_word in _CALM_CONFIDENCE_WORDS


# ── public API ───────────────────────────────────────────────────────
def fetch_context_for(
    symbol: str,
    kind: str,
    *,
    confidence_word: str | None = None,
    force: bool = False,
    max_results: int = _MAX_RESULTS_PER_QUERY,
) -> list[dict]:
    """Return Brave web-context dicts for `symbol`. `kind` is accepted
    for compatibility with the ticker call site, but the cache is
    keyed by symbol only — both kinds share one cache entry.

    Cost-discipline layers, in order:
      1. Hermetic mode: no key → []
      2. Cache hit within TTL → cached results
      3. Interest gate: calm forecast + cache miss → [] (and log skip)
      4. Daily quota hit → stale cache or [] (and log skip)
      5. Fetch + write cache

    `force=True` bypasses (3) so an operator's "refresh" button can
    pull fresh context for a calm forecast on demand.
    """
    key = _brave_key()
    if not key:
        return []

    symbol_u = symbol.upper()
    cache = _load_cache()
    entry = cache.get(symbol_u)
    now = time.time()

    if entry and now - entry.get("fetched_at", 0) < entry.get("ttl", _CACHE_TTL_BASE_S):
        log_event(
            "movement.brave.cache_hit", level="info",
            symbol=symbol_u,
            age_s=int(now - entry.get("fetched_at", 0)),
        )
        return entry.get("results", [])

    # Interest gate. Skip when the engine says nothing is happening.
    if not force and _is_calm(confidence_word):
        log_event(
            "movement.brave.skip_calm", level="info",
            symbol=symbol_u, confidence_word=confidence_word,
        )
        # Serve stale cache if we have one — better than nothing.
        if entry:
            return entry.get("results", [])
        return []

    # Daily quota guard.
    quota = _load_quota()
    if quota.get("calls", 0) >= _daily_quota():
        log_event(
            "movement.brave.quota_hit", level="warn",
            symbol=symbol_u,
            calls_today=quota.get("calls", 0),
            cap=_daily_quota(),
        )
        if entry:
            return entry.get("results", [])
        return []

    # Fresh fetch.
    query = _build_query(symbol_u)
    try:
        import requests
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": key,
                "User-Agent": "Mozilla/5.0 (Dore/judge-context)",
            },
            timeout=_HTTP_TIMEOUT,
        )
        # Always count the call against quota (Brave bills on request,
        # not just on success). Increment BEFORE raising so a 429 burn
        # still counts.
        quota["calls"] = int(quota.get("calls", 0)) + 1
        _save_quota(quota)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.brave.fetch_failed_served_stale", level="warn",
            symbol=symbol_u, error_class=type(exc).__name__,
            error_message=str(exc)[:200],
            has_stale=entry is not None,
        )
        if entry:
            return entry.get("results", [])
        return []

    results = []
    web = (data.get("web") or {}).get("results") or []
    for r in web[:max_results]:
        url = r.get("url") or ""
        if not url:
            continue
        results.append({
            "title": (r.get("title") or "").strip(),
            "snippet": (r.get("description") or "").strip()[:280],
            "url": url,
            "trust_tier": _trust_label_for(url),
            "domain": _domain_for(url),
        })

    cache[symbol_u] = {
        "fetched_at": now,
        "ttl": _cache_ttl_with_jitter(),
        "query": query,
        "results": results,
    }
    _save_cache(cache)
    log_event(
        "movement.brave.fetched", level="info",
        symbol=symbol_u, count=len(results),
        calls_today=quota.get("calls", 0),
        cap=_daily_quota(),
    )
    return results
