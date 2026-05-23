"""Web-search discovery — close attestation gaps the locator can't reach.

When the static-HTML / Gatsby / Next.js harvesters all fail (typical of
JS-rendered transparency pages like TUSD's), we still want to find a
fresh attestation PDF. This module runs a typed web search for the
token and feeds verified PDF candidates back into the resolver chain.

Discovery rule, per Anthony: "do Google searches when you have gaps in
data — they often reveal more data links". Every hit is logged to the
observability ring buffer so the Data Compendium page can show it.

## Backends

`SCA_WEB_SEARCH_PROVIDER` env selects the backend:
  - unset / "off"  → no-op, logs `web_discovery.disabled` once per process
  - "brave"        → Brave Search API (free tier 2k/mo) — needs SCA_WEB_SEARCH_KEY
  - "serper"       → serper.dev (Google SERP proxy) — needs SCA_WEB_SEARCH_KEY

The contract is: `discover_attestation_url(symbol, issuer) -> str | None`.
Whatever backend returns a PDF that HEAD-checks 200 is the winner.

This module never invents results and never speaks to the LLM — it is a
deterministic search-then-HEAD-check pipeline. Every URL is cached so we
don't reburn the search quota on repeated lookups.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import requests

from sca import config
from sca.observability import log_event

_CACHE = config.DATA_DIR / "web_discoveries.json"
# A discovery URL is trusted for the attestation cadence; after that we
# re-search so a freshly-published report is never missed.
_CACHE_MAX_AGE_DAYS = 25
_HTTP_TIMEOUT = 25.0
_MAX_RESULTS = 8
_UA = {"User-Agent": "Mozilla/5.0"}


def _provider() -> str:
    return os.environ.get("SCA_WEB_SEARCH_PROVIDER", "").strip().lower()


def _api_key() -> str:
    return os.environ.get("SCA_WEB_SEARCH_KEY", "").strip()


def _load_cache() -> dict:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    from sca.persist import atomic_write_json
    atomic_write_json(_CACHE, cache, sort_keys=False)


def _cache_fresh(entry: dict) -> bool:
    resolved_at = entry.get("resolved_at")
    if not resolved_at:
        return False
    try:
        when = date.fromisoformat(resolved_at)
    except ValueError:
        return False
    return (date.today() - when).days <= _CACHE_MAX_AGE_DAYS


def _head_is_pdf(url: str) -> bool:
    """A PDF that returns 200 and either declares Content-Type:
    application/pdf or has a .pdf suffix."""
    try:
        resp = requests.head(
            url, allow_redirects=True, timeout=_HTTP_TIMEOUT, headers=_UA,
        )
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False
    ct = resp.headers.get("Content-Type", "").lower()
    return "pdf" in ct or url.lower().endswith(".pdf")


# ── search backends ───────────────────────────────────────────────────
def _search_brave(query: str) -> list[str]:
    """Brave Search API — JSON results, freemium."""
    key = _api_key()
    if not key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": _MAX_RESULTS},
            headers={**_UA, "Accept": "application/json",
                     "X-Subscription-Token": key},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log_event(
            "web_discovery.backend_error", level="warn",
            backend="brave", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    web = data.get("web", {}).get("results", [])
    return [r.get("url", "") for r in web if r.get("url")]


def _search_serper(query: str) -> list[str]:
    """serper.dev — Google SERP proxy."""
    key = _api_key()
    if not key:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": _MAX_RESULTS},
            headers={**_UA, "X-API-KEY": key,
                     "Content-Type": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log_event(
            "web_discovery.backend_error", level="warn",
            backend="serper", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    organic = data.get("organic", [])
    return [r.get("link", "") for r in organic if r.get("link")]


_BACKENDS = {
    "brave": _search_brave,
    "serper": _search_serper,
}


# ── public API ────────────────────────────────────────────────────────
_disabled_logged = False


def _log_disabled_once() -> None:
    global _disabled_logged
    if _disabled_logged:
        return
    _disabled_logged = True
    log_event(
        "web_discovery.disabled", level="info",
        detail=(
            "set SCA_WEB_SEARCH_PROVIDER (brave|serper) and "
            "SCA_WEB_SEARCH_KEY to close attestation gaps automatically"
        ),
    )


def discover_attestation_url(
    symbol: str, issuer: str = "", *, refresh: bool = False,
) -> str | None:
    """Find a fresh attestation PDF URL for `symbol` via web search.

    Returns a HEAD-checked PDF URL or None. Cached per-symbol for the
    attestation cadence so we don't reburn the search quota.
    """
    provider = _provider()
    if not provider or provider == "off":
        _log_disabled_once()
        return None
    backend = _BACKENDS.get(provider)
    if backend is None:
        log_event(
            "web_discovery.unknown_provider", level="warn",
            provider=provider, supported=list(_BACKENDS),
        )
        return None

    cache = _load_cache()
    if not refresh:
        entry = cache.get(symbol, {})
        cached = entry.get("url")
        if cached and _cache_fresh(entry) and _head_is_pdf(cached):
            log_event(
                "web_discovery.cache_hit", level="info",
                symbol=symbol, url=cached,
            )
            return cached

    # Build a typed query that targets reserve-attestation PDFs.
    year = date.today().year
    issuer_term = f' "{issuer}"' if issuer else ""
    query = (
        f'"{symbol}" reserves attestation{issuer_term} {year} filetype:pdf'
    )
    log_event(
        "web_discovery.search.start", level="info",
        symbol=symbol, provider=provider, query=query,
    )
    candidates = backend(query)
    log_event(
        "web_discovery.search.results", level="info",
        symbol=symbol, provider=provider, count=len(candidates),
    )
    if not candidates:
        return None

    # Filter to PDF-looking URLs first (cheap), then HEAD-check.
    pdf_like = [u for u in candidates if u.lower().endswith(".pdf")
                or "/wp-content/" in u.lower()]
    for url in pdf_like or candidates:
        if _head_is_pdf(url):
            cache[symbol] = {
                "url": url,
                "via": "web_search",
                "provider": provider,
                "resolved_at": date.today().isoformat(),
                "query": query,
            }
            _save_cache(cache)
            log_event(
                "web_discovery.hit", level="info",
                symbol=symbol, provider=provider, url=url,
            )
            return url

    log_event(
        "web_discovery.no_pdf", level="warn",
        symbol=symbol, provider=provider,
        candidates_count=len(candidates),
    )
    return None


def recent_discoveries(limit: int = 50) -> list[dict]:
    """Snapshot of the cache (newest first) for the Compendium UI."""
    cache = _load_cache()
    rows = [{"symbol": k, **v} for k, v in cache.items()]
    rows.sort(key=lambda r: r.get("resolved_at", ""), reverse=True)
    return rows[:limit]


# ── live news snippets — augmentation context ──────────────────────────
# When SCA_WEB_SEARCH_PROVIDER is configured, augmentation prompts can
# pull 2-3 recent news snippets about a token to ground the LLM in
# current real-world context (regulatory action, partnerships, depegs,
# governance changes). Strictly qualitative — the LLM never invents
# figures; snippets are factual material the LLM can cite as URLs.

def _search_brave_news(query: str) -> list[dict]:
    key = _api_key()
    if not key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 5, "freshness": "pm"},  # past month
            headers={**_UA, "Accept": "application/json",
                     "X-Subscription-Token": key},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log_event(
            "web_discovery.news_backend_error", level="warn",
            backend="brave", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    out: list[dict] = []
    for r in data.get("web", {}).get("results", [])[:5]:
        out.append({
            "title": r.get("title", "")[:160],
            "url": r.get("url", ""),
            "snippet": r.get("description", "")[:300],
            "age": r.get("age", ""),
        })
    return [s for s in out if s["url"]]


def _search_serper_news(query: str) -> list[dict]:
    key = _api_key()
    if not key:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/news",
            json={"q": query, "num": 5},
            headers={**_UA, "X-API-KEY": key,
                     "Content-Type": "application/json"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log_event(
            "web_discovery.news_backend_error", level="warn",
            backend="serper", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    out: list[dict] = []
    for r in data.get("news", [])[:5]:
        out.append({
            "title": r.get("title", "")[:160],
            "url": r.get("link", ""),
            "snippet": r.get("snippet", "")[:300],
            "age": r.get("date", ""),
        })
    return [s for s in out if s["url"]]


_NEWS_BACKENDS = {
    "brave": _search_brave_news,
    "serper": _search_serper_news,
}


def recent_news_snippets(
    symbol: str, issuer: str = "", *, kind: str = "general",
    limit: int = 3,
) -> list[dict]:
    """Fetch a few recent news snippets for an LLM augmentation prompt.

    `kind` shapes the query: "general" (recent news), "regulatory"
    (compliance / OFAC / regulator), "redemption" (depegs, redemption
    halts), "reserves" (attestation news). Returns up to `limit` items.

    No-op (returns []) when SCA_WEB_SEARCH_PROVIDER isn't set, so the
    rest of the augmentation pipeline keeps working unchanged.
    """
    provider = _provider()
    if not provider or provider == "off":
        return []
    backend = _NEWS_BACKENDS.get(provider)
    if backend is None:
        return []
    issuer_term = f' {issuer}' if issuer else ""
    qualifiers = {
        "general": "stablecoin news",
        "regulatory": "stablecoin regulation OFAC compliance",
        "redemption": "stablecoin redemption depeg liquidity",
        "reserves": "stablecoin reserves attestation auditor",
    }.get(kind, "stablecoin news")
    query = f"{symbol}{issuer_term} {qualifiers}"
    log_event(
        "web_discovery.news.start", level="info",
        symbol=symbol, kind=kind, provider=provider, query=query,
    )
    hits = backend(query)
    log_event(
        "web_discovery.news.results", level="info",
        symbol=symbol, kind=kind, count=len(hits),
    )
    return hits[:limit]
