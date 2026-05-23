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

# ── Authoritative-domain trust tier ──────────────────────────────────
# "Search is a lead, never a fact" — when a search returns results, we
# rank candidates from KNOWN authoritative origins (issuer transparency
# CDNs, regulators, major auditors, explorers) above generic results.
# This is one of two safeguards (the other is the two-source confirmation
# rule) that keep SEO spam and content-farm pages out of the verified-
# fact path. See dore-search-is-a-lead-not-a-fact in memory.
_AUTHORITATIVE_DOMAINS = (
    # Issuer / transparency CDNs (the highest tier of trust for an
    # attestation PDF — these are the issuers' own pages)
    "circle.com", "hubspotusercontent-na1.net",  # USDC + EURC
    "tether.to", "tether.io",  # USDT, EURT, XAU₮
    "paxos.com", "withum.com",  # PYUSD / USDP / USDG + auditor
    "tusd.io",  # TrueUSD
    "gemini.com",  # GUSD
    "firstdigitallabs.com",  # FDUSD
    "ripple.com",  # RLUSD
    "agora.io", "buildwithfern.com",  # AUSD
    "bitgo.com", "worldlibertyfinancial.com",  # USD1
    "mountainprotocol.com",  # USDM
    "makerdao.com", "sky.money",  # DAI / USDS
    "ethena.fi",  # USDe
    "aave.com",  # GHO
    "curve.fi", "curve.finance",  # crvUSD
    "liquity.org",  # LUSD
    "abracadabra.money",  # MIM
    "frax.finance",  # FRAX
    "usdf.com",  # USDf
    # Regulators + standards bodies
    "treasury.gov", "ofac.treasury.gov", "sec.gov", "cftc.gov",
    "federalreserve.gov", "fdic.gov", "occ.treas.gov",
    "europa.eu", "ecb.europa.eu", "esma.europa.eu",
    "fsb.org", "bis.org", "iosco.org", "iaasb.org",
    "dfs.ny.gov",  # NYDFS
    "fca.org.uk", "bankofengland.co.uk",
    "mas.gov.sg", "bma.bm",
    # Major auditors / accounting firms
    "deloitte.com", "kpmg.com", "ey.com", "pwc.com",
    "grantthornton.com", "bdo.com", "moorehk.com.hk",
    "withum.com", "crowe.com", "bpmcpa.com",
    # On-chain explorers (independent verification)
    "etherscan.io", "arbiscan.io", "basescan.org",
    "polygonscan.com", "bscscan.com", "snowtrace.io",
    "optimistic.etherscan.io", "solscan.io", "tronscan.org",
    "data.chain.link",  # Chainlink PoR feeds
)


def _domain_trust_rank(url: str) -> int:
    """Lower number = more trusted. 0 for authoritative-domain matches,
    1 for sub-domains of authoritative origins, 2 for everything else.
    Used to re-rank search results so issuer / regulator / auditor pages
    surface above SEO spam."""
    if not url:
        return 9
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return 9
    for trusted in _AUTHORITATIVE_DOMAINS:
        if host == trusted:
            return 0
        if host.endswith("." + trusted):
            return 1
    return 2


def _rank_by_trust(urls: list[str]) -> list[str]:
    """Stable sort by domain trust rank — authoritative origins first.
    Preserves the underlying search-rank order within each tier so we
    still benefit from Brave/DDG's relevance signal."""
    return sorted(urls, key=lambda u: (_domain_trust_rank(u), urls.index(u)))


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


def _is_acceptable_origin(url: str, known_domain: str, symbol: str) -> bool:
    """Reject PDFs hosted on origins we can't relate to the issuer.

    A HEAD-200 PDF response is necessary but not sufficient. Search
    backends sometimes return PDFs from completely unrelated S3 buckets
    or content farms that happen to have the symbol in their filename.
    A PDF we'd display as "the issuer's attestation" needs at minimum:
      - the issuer's own domain, OR
      - a known authoritative-domain cluster (issuer CDNs, regulators,
        auditors, Protos / news mirrors that re-host real attestations).
    Hosts in neither bucket are rejected — "search is a lead, never a
    fact" enforced at the URL boundary.
    """
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    # Strip leading www. for matching.
    bare = host[4:] if host.startswith("www.") else host
    # Always accept the registry-known issuer domain (and its subdomains).
    if known_domain:
        kd = known_domain.lower()
        if kd.startswith("www."):
            kd = kd[4:]
        if bare == kd or bare.endswith("." + kd):
            return True
    # Always accept any authoritative-domain match (issuer CDNs already
    # in the trust list, regulators, auditors, Chainlink PoR).
    if _domain_trust_rank(url) <= 1:
        return True
    # Accept known re-hosters for archived issuer attestations.
    REHOSTERS = (
        "protos-media.s3.eu-west-2.amazonaws.com",  # Protos mirror
        "cdn.prod.website-files.com",  # Webflow CDN — used by issuers
        "cdn.sanity.io",  # Sanity CDN — Ripple/RLUSD
        "files.buildwithfern.com",  # Fern docs — Agora/AUSD
        "assets.ctfassets.net",  # Contentful — Gemini/USDT
        "landing.bitgo.com",  # BitGo — USD1 mailings
        "ctfassets.net",
        "hubspotusercontent-na1.net",  # HubSpot — Circle
    )
    if any(rh in bare for rh in REHOSTERS):
        return True
    # Otherwise reject — symbol-in-filename on an unknown S3 bucket is
    # exactly the SEO spam / impersonation surface we don't trust.
    log_event(
        "web_discovery.rejected_unknown_origin", level="info",
        symbol=symbol, host=host, url=url,
    )
    return False


def _head_is_pdf_from_trusted(
    url: str, known_domain: str, symbol: str,
) -> bool:
    """Two-gate check: real PDF AND from an origin we can relate to the
    issuer. Used everywhere a search candidate would otherwise become
    the displayed attestation source."""
    return _head_is_pdf(url) and _is_acceptable_origin(
        url, known_domain, symbol,
    )


# ── search backends ───────────────────────────────────────────────────
# Transient retry policy: Brave's free tier rate-limits at 1 query / 1s.
# DuckDuckGo's HTML endpoint occasionally returns CAPTCHA HTML when
# scraped too fast. Both deserve a short exponential backoff before we
# give up — a single 429 or read-timeout shouldn't blank a discovery
# cycle that the canary won't retry for 6h.
_RETRY_DELAYS = (1.2, 3.5, 7.0)  # ~12s total, well under HTTP_TIMEOUT
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _retryable_get(method: str, *args, **kwargs):
    """requests.{get,post} with exponential backoff on transient failures.

    Returns the final Response (or raises). Retries on:
      - connection errors / read timeouts
      - HTTP 408 / 425 / 429 / 500–504
    Other 4xx are returned as-is so the caller can decide.
    """
    import time as _t
    fn = getattr(requests, method)
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            resp = fn(*args, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == len(_RETRY_DELAYS):
                raise
            log_event(
                "web_discovery.retry", level="info",
                method=method, attempt=attempt + 1,
                error_class=type(exc).__name__,
            )
            _t.sleep(_RETRY_DELAYS[attempt])
            continue
        if resp.status_code in _RETRYABLE_STATUS \
                and attempt < len(_RETRY_DELAYS):
            log_event(
                "web_discovery.retry", level="info",
                method=method, attempt=attempt + 1,
                status_code=resp.status_code,
            )
            _t.sleep(_RETRY_DELAYS[attempt])
            continue
        return resp
    if last_exc:
        raise last_exc
    return resp  # last response after final retry — caller checks status


def _search_brave(query: str) -> list[str]:
    """Brave Search API — JSON results, freemium. Retries transient
    failures (429 / timeouts) so a momentary rate-limit doesn't blank
    the discovery cycle."""
    key = _api_key()
    if not key:
        return []
    try:
        resp = _retryable_get(
            "get",
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


def _search_anthropic(query: str) -> list[str]:
    """Anthropic's native web_search tool — uses ANTHROPIC_API_KEY.

    Lets the LLM do the searching itself, no separate Brave/Serper key
    needed. Returns a list of URLs Claude found relevant to the query,
    extracted from the tool's citation blocks.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        log_event(
            "web_discovery.anthropic_no_key", level="warn",
            detail="ANTHROPIC_API_KEY required for the anthropic backend",
        )
        return []
    try:
        import anthropic
    except ImportError:
        log_event(
            "web_discovery.anthropic_no_sdk", level="warn",
            detail="pip install '.[llm]' to enable the anthropic backend",
        )
        return []
    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=os.environ.get(
                "SCA_WEB_SEARCH_MODEL", "claude-haiku-4-5-20251001",
            ),
            max_tokens=1024,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Search the web for: {query}\n\n"
                    "Return only the URLs of the most relevant results. "
                    "One URL per line. No commentary."
                ),
            }],
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "web_discovery.backend_error", level="warn",
            backend="anthropic", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    # Anthropic returns citation blocks + text blocks; harvest both.
    urls: list[str] = []
    for block in msg.content:
        # Text block — parse line-by-line for URLs.
        if getattr(block, "type", "") == "text":
            for line in (block.text or "").splitlines():
                line = line.strip().strip("•-* ")
                if line.startswith(("http://", "https://")):
                    urls.append(line.split()[0])
        # Web-search tool result block — its citations carry source URLs.
        if getattr(block, "type", "") == "server_tool_use":
            continue  # the tool invocation, not the results
        results = getattr(block, "content", None)
        if isinstance(results, list):
            for r in results:
                u = (
                    getattr(r, "url", "")
                    or (r.get("url") if isinstance(r, dict) else "")
                )
                if u and u.startswith(("http://", "https://")):
                    urls.append(u)
    # Dedup while preserving order.
    seen = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:_MAX_RESULTS]


def _search_duckduckgo(query: str) -> list[str]:
    """DuckDuckGo HTML search — no API key required, no signup, free.

    Scrapes the lightweight HTML endpoint at html.duckduckgo.com.
    Returns ordered result URLs. Rate-limited by DuckDuckGo (a few
    requests per second tops) so the discovery thread doesn't hammer
    it: every hit is cached per-symbol for 25 days.

    This is the default backend so the system works out of the box
    without the user needing to sign up for Brave / Serper / Anthropic.
    """
    import html as _html
    import re as _re
    from urllib.parse import unquote as _unquote
    try:
        resp = _retryable_get(
            "post",
            "https://html.duckduckgo.com/html/",
            data={"q": query},  # `kl` locale filter empties the response set
            headers={**_UA, "Accept": "text/html"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_event(
            "web_discovery.backend_error", level="warn",
            backend="duckduckgo", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    # Result anchors have class="result__a" and href="...".
    # DuckDuckGo sometimes wraps URLs through their redirector
    # ("/l/?uddg=...") — decode if so.
    out: list[str] = []
    seen: set[str] = set()
    pattern = _re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"',
        _re.IGNORECASE,
    )
    for raw_href in pattern.findall(resp.text)[:_MAX_RESULTS * 2]:
        href = _html.unescape(raw_href)
        if "/l/?" in href and "uddg=" in href:
            # Pull the underlying URL out of the redirector.
            m = _re.search(r"uddg=([^&]+)", href)
            if m:
                href = _unquote(m.group(1))
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith(("http://", "https://")):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= _MAX_RESULTS:
            break
    return out


def _search_llm(query: str) -> list[str]:
    """LLM-as-search — uses the configured LLM (DeepSeek by default).

    No separate Brave/Serper key needed. The model proposes URLs it
    knows for the query and we HEAD-check each one before accepting.
    Bounded by the model's training cutoff, so genuinely-recent items
    may need a real search backend. Returns at most _MAX_RESULTS URLs.
    """
    try:
        from sca.llm import get_llm
        client = get_llm()
    except Exception as exc:  # noqa: BLE001
        log_event(
            "web_discovery.llm_unavailable", level="warn",
            error_class=type(exc).__name__, error_message=str(exc),
        )
        return []
    system = (
        "You are a URL-recall tool. Given a search query, return a JSON "
        "array of the most relevant URLs you know that would answer "
        "it — issuer PDFs, official transparency pages, regulator "
        "filings, mainstream financial press articles. Be conservative: "
        "if you are not confident a URL exists exactly as you remember "
        "it, omit it. Do NOT invent URL paths. Cap the list at 8."
    )
    prompt = (
        f"Query: {query}\n\n"
        "Return strict JSON: [\"https://...\", \"https://...\", ...]\n"
        "No commentary, just the JSON array. Use [] if you have no "
        "confident recall."
    )
    try:
        raw = client.extract_json(
            system=system, prompt=prompt,
            schema={"type": "array", "items": {"type": "string"}},
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "web_discovery.backend_error", level="warn",
            backend="llm", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    if isinstance(raw, dict):
        raw = raw.get("urls") or raw.get("items") or []
    urls: list[str] = []
    for u in (raw if isinstance(raw, list) else []):
        s = str(u).strip()
        if s.startswith(("http://", "https://")):
            urls.append(s)
    return urls[:_MAX_RESULTS]


def _search_combo(query: str) -> list[str]:
    """Brave first (better relevance, paid free-tier-friendly), fall back
    to DuckDuckGo if Brave returns nothing or errors. Results are then
    re-ranked by authoritative-domain trust tier — issuer transparency
    CDNs, regulators, and major auditors surface above generic results.

    Why this order: Brave's results are typically more useful for our
    typed-attestation-PDF queries, and the free 2k/month is plenty given
    our discovery thread caches every hit for 25 days. DDG is the safety
    net for the queries Brave misses (less common but happens for
    obscure tokens) or for the day a Brave outage or quota exhaustion
    would otherwise blank the page. The trust-tier re-rank is the
    "source-quality filtering" half of the search-is-a-lead discipline.
    """
    results = _search_brave(query)
    if results:
        log_event(
            "web_discovery.combo.brave_hit", level="info",
            query=query, count=len(results),
        )
        return _rank_by_trust(results)
    results = _search_duckduckgo(query)
    log_event(
        "web_discovery.combo.ddg_fallback", level="info",
        query=query, count=len(results),
    )
    return _rank_by_trust(results)


_BACKENDS = {
    "duckduckgo": _search_duckduckgo,
    "brave": _search_brave,
    "serper": _search_serper,
    "anthropic": _search_anthropic,
    "llm": _search_llm,
    "combo": _search_combo,
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
    known_domain: str = "",
) -> str | None:
    """Find a fresh attestation PDF URL for `symbol` via web search.

    Returns a HEAD-checked PDF URL or None. Cached per-symbol for the
    attestation cadence so we don't reburn the search quota.

    `known_domain` is the issuer's authoritative domain (typically
    derived by the caller from `token.transparency_url`). When supplied
    it becomes the FIRST third-hop `site:` query — preventing the
    discovery from drifting onto an unrelated issuer's pages when
    Brave's first round doesn't surface the real issuer site.
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

    # Build a natural-language query — Brave's relevance is much
    # stronger on conversational phrasing than on keyword soup. We
    # leave the `filetype:pdf` operator off because DDG sometimes
    # empties the result set for it; the downstream HEAD-check is
    # the actual PDF filter.
    year = date.today().year
    issuer_term = f' issued by {issuer}' if issuer else ""
    query = (
        f"What is the most recent reserve attestation report for "
        f"{symbol} stablecoin{issuer_term}? Looking for the {year} PDF."
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

    # Filter to PDF-looking URLs first (cheap), then HEAD-check + origin
    # check. Origin check rejects PDFs from unknown S3 buckets etc. that
    # happen to have the symbol in the filename — see _is_acceptable_origin.
    pdf_like = [u for u in candidates if u.lower().endswith(".pdf")
                or "/wp-content/" in u.lower()]
    for url in pdf_like or candidates:
        if _head_is_pdf_from_trusted(url, known_domain, symbol):
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

    # Second hop: many issuer attestations live one click below an
    # index page (Mountain Protocol, Curve, Aave style — docs subsites
    # that link the PDF). When no direct PDF lands in search results,
    # walk the top transparency-looking candidates through the
    # attestation_locator (static HTML scrape + LLM rank) which knows
    # how to find PDF anchors inside a docs page.
    transparency_signals = (
        "transparency", "attestation", "reserves", "proof-of-reserves",
        "audit", "report", "/docs/", "docs.",
    )
    locator_candidates = [
        u for u in candidates
        if any(sig in u.lower() for sig in transparency_signals)
        and not u.lower().endswith(".pdf")
    ][:3]  # top 3 transparency-looking pages
    if locator_candidates:
        try:
            from sca.tools.attestation_locator import (
                LocatorUnavailable,
                resolve_attestation_url as _locate,
            )
        except Exception:  # noqa: BLE001
            locator_candidates = []
    for page_url in locator_candidates:
        try:
            found = _locate(page_url, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 - any locator failure
            log_event(
                "web_discovery.second_hop_failed", level="info",
                symbol=symbol, page_url=page_url,
                error_class=type(exc).__name__,
            )
            continue
        pdf_url = found.get("url") if isinstance(found, dict) else None
        if pdf_url and _head_is_pdf_from_trusted(
                pdf_url, known_domain, symbol):
            cache[symbol] = {
                "url": pdf_url,
                "via": "web_search+locator",
                "provider": provider,
                "resolved_at": date.today().isoformat(),
                "query": query,
                "source_page": page_url,
            }
            _save_cache(cache)
            log_event(
                "web_discovery.hit", level="info",
                symbol=symbol, provider=provider, url=pdf_url,
                source_page=page_url,
            )
            return pdf_url

    # Third hop: domain-scoped follow-up search. When the top candidates
    # were SPAs the locator couldn't scrape (Anchored Coins, Paxos,
    # Mountain Protocol style), ask Brave again with a `site:` operator
    # constrained to the issuer's domain. Brave indexes per-token landing
    # pages that the static scraper can't reach.
    #
    # The KNOWN issuer domain (passed in from the registry's
    # transparency_url) is always queried first — first-round search
    # results sometimes don't surface the real issuer at all (a Circle
    # blog about USDC ranking #1 for an AEUR query, etc), and trusting
    # the search-ranked top would drift onto the wrong issuer's pages.
    from urllib.parse import urlparse as _urlparse
    seen_domains: set[str] = set()
    issuer_domains: list[str] = []

    def _push_domain(host: str) -> None:
        if not host or host in seen_domains:
            return
        seen_domains.add(host)
        if any(skip in host for skip in (
            "coinmarketcap.", "coingecko.", "messari.", "mexc.",
            "binance.", "coinbase.", "kraken.", "tokeninsight.",
            "stableregistry.", "coinlaw.", "stablecoininsider.",
            "defillama.", "eco.com", "bpm.com",
        )):
            return
        issuer_domains.append(host)

    # 1. Known issuer domain from the registry — most trusted.
    if known_domain:
        kd = known_domain.lower()
        if kd.startswith("www."):
            kd = kd[4:]
        _push_domain(kd)

    # 2. Top search-ranked domains as fallback.
    for u in candidates[:6]:
        if len(issuer_domains) >= 3:
            break
        try:
            host = (_urlparse(u).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
        except Exception:  # noqa: BLE001
            continue
        _push_domain(host)
    for domain in issuer_domains:
        follow_query = (
            f"site:{domain} {symbol} attestation report OR reserves PDF"
        )
        log_event(
            "web_discovery.third_hop.start", level="info",
            symbol=symbol, query=follow_query,
        )
        try:
            domain_results = backend(follow_query)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "web_discovery.third_hop.error", level="warn",
                symbol=symbol, domain=domain,
                error_class=type(exc).__name__,
            )
            continue
        log_event(
            "web_discovery.third_hop.results", level="info",
            symbol=symbol, domain=domain, count=len(domain_results),
        )
        # Same accept-PDF discipline + origin check.
        for url in domain_results:
            if _head_is_pdf_from_trusted(url, known_domain, symbol):
                cache[symbol] = {
                    "url": url,
                    "via": "web_search+domain_scoped",
                    "provider": provider,
                    "resolved_at": date.today().isoformat(),
                    "query": follow_query,
                    "source_domain": domain,
                }
                _save_cache(cache)
                log_event(
                    "web_discovery.hit", level="info",
                    symbol=symbol, provider=provider, url=url,
                    source_domain=domain, via="domain_scoped",
                )
                return url
        # No direct PDF — try the locator on the top non-PDF result
        # from this domain-scoped query.
        for page_url in domain_results[:2]:
            if page_url.lower().endswith(".pdf"):
                continue
            try:
                from sca.tools.attestation_locator import (
                    LocatorUnavailable, resolve_attestation_url as _locate,
                )
                found = _locate(page_url, symbol=symbol)
            except (LocatorUnavailable, requests.RequestException, Exception) as exc:  # noqa: BLE001
                log_event(
                    "web_discovery.third_hop.locator_failed", level="info",
                    symbol=symbol, page_url=page_url,
                    error_class=type(exc).__name__,
                )
                continue
            pdf_url = found.get("url") if isinstance(found, dict) else None
            if pdf_url and _head_is_pdf_from_trusted(
                    pdf_url, known_domain, symbol):
                cache[symbol] = {
                    "url": pdf_url,
                    "via": "web_search+domain_scoped+locator",
                    "provider": provider,
                    "resolved_at": date.today().isoformat(),
                    "query": follow_query,
                    "source_page": page_url,
                }
                _save_cache(cache)
                log_event(
                    "web_discovery.hit", level="info",
                    symbol=symbol, provider=provider, url=pdf_url,
                    source_page=page_url, via="domain_scoped+locator",
                )
                return pdf_url

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


# ── Issuer status Q&A — for the brief + augmentation prompts ──────────
# Same Brave/DDG combo backend, but asks broader open-ended questions
# about the issuer (wind-down? recent regulatory action? depeg events?
# leadership changes? auditor swap?) so the LLM can fold richer context
# into the brief. Generic across every token — same plumbing for USDM
# (winding down), TUSD (Justin Sun controversy), FDUSD (Sun fraud
# accusation), and any future issuer. No token-specific code.

_ISSUER_QA_CACHE = config.DATA_DIR / "issuer_qa_cache.json"
_ISSUER_QA_TTL_DAYS = 7  # status context is more volatile than URLs


def _load_qa_cache() -> dict:
    if _ISSUER_QA_CACHE.exists():
        try:
            return json.loads(_ISSUER_QA_CACHE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_qa_cache(cache: dict) -> None:
    from sca.persist import atomic_write_json
    atomic_write_json(_ISSUER_QA_CACHE, cache, sort_keys=False)


def _qa_fresh(entry: dict) -> bool:
    when = entry.get("resolved_at", "")
    if not when:
        return False
    try:
        d = date.fromisoformat(when)
    except ValueError:
        return False
    return (date.today() - d).days <= _ISSUER_QA_TTL_DAYS


def find_replacement_source(
    source_id: str, broken_url: str, *, title: str = "",
) -> str | None:
    """Search for a replacement URL when a canary source flips broken.

    Used by `health_thread` on `health.source.flipped` (broken). Builds
    a query from the source title + slug, runs the configured search
    backend, and returns the first HEAD-checked candidate that's NOT
    the broken URL. Returns None if nothing better than the original
    is found.

    Same primitives as attestation discovery: searches once, HEAD-
    checks every candidate, caches the winner. Result is best-effort;
    a curator should still confirm before relying on it.
    """
    if not _provider() or _provider() == "off":
        return None
    backend = _BACKENDS.get(_provider())
    if backend is None:
        return None
    # Build a query: prefer the title, fall back to the slug.
    base = title or source_id.replace("-", " ").replace("_", " ")
    query = f"{base} stablecoin regulation 2026 site"
    log_event(
        "canary.recovery.start", level="info",
        source_id=source_id, broken_url=broken_url, query=query,
    )
    try:
        candidates = backend(query)
    except Exception as exc:  # noqa: BLE001
        log_event(
            "canary.recovery.failed", level="warn",
            source_id=source_id, error_class=type(exc).__name__,
        )
        return None
    for url in candidates:
        if url == broken_url:
            continue
        # Light HEAD check — accept anything that returns 200.
        try:
            r = requests.head(
                url, allow_redirects=True, timeout=_HTTP_TIMEOUT, headers=_UA,
            )
            if r.status_code == 200:
                log_event(
                    "canary.recovery.hit", level="info",
                    source_id=source_id, replacement_url=url,
                )
                return url
        except requests.RequestException:
            continue
    log_event(
        "canary.recovery.no_match", level="info",
        source_id=source_id, candidates_count=len(candidates),
    )
    return None


def issuer_status_context(
    symbol: str, issuer: str = "", *, refresh: bool = False, limit: int = 4,
) -> list[dict]:
    """Search-backed status snippets for `(symbol, issuer)`.

    Asks open-ended questions specifically designed to surface the
    "is this issuer healthy" signal — wind-down announcements,
    regulatory action, depeg events, leadership changes. Returns up
    to `limit` snippets in the same {title, url, snippet, age} shape
    as recent_news_snippets, suitable for direct inclusion in an LLM
    prompt.

    Cached per (symbol) for 7 days (shorter than the attestation URL
    cache because issuer status is more volatile). Shared across all
    users — only refresh / re-run triggers a fresh search per the
    standing cache discipline.

    No-op when no search provider is configured, so the pipeline
    keeps working unchanged.
    """
    if not _provider() or _provider() == "off":
        return []
    cache = _load_qa_cache()
    if not refresh:
        entry = cache.get(symbol)
        if entry and _qa_fresh(entry):
            return entry.get("snippets", [])[:limit]

    # Natural-language queries — Brave's AI-summary mode picks up
    # conversational phrasing much better than keyword strings.
    # Three angles: notable events, current operational status, and
    # any recent regulatory action.
    issuer_term = f' issued by {issuer}' if issuer else ""
    queries = [
        f"What is notable about {symbol} stablecoin{issuer_term} "
        f"right now? Wind-down, depeg, regulatory action, or auditor "
        f"changes?",
        f"What is the current operational status of {symbol} "
        f"stablecoin{issuer_term} as of {date.today().year}? Reserves, "
        f"issuance, redemption?",
        f"Has there been recent enforcement action, sanctions, or "
        f"legal news related to {symbol}{issuer_term}?",
    ]
    seen_urls: set[str] = set()
    snippets: list[dict] = []
    for query in queries:
        try:
            from sca.web_discovery import recent_news_snippets as _news
            for s in _news(symbol, issuer=issuer, kind="general", limit=4):
                if s.get("url") and s["url"] not in seen_urls:
                    seen_urls.add(s["url"])
                    snippets.append(s)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "issuer_status.failed", level="warn",
                symbol=symbol, error_class=type(exc).__name__,
            )
            continue
        # Once we have enough, stop spending search quota.
        if len(snippets) >= limit * 2:
            break

    snippets = snippets[:limit]
    cache[symbol] = {
        "snippets": snippets,
        "resolved_at": date.today().isoformat(),
        "issuer": issuer,
    }
    _save_qa_cache(cache)
    log_event(
        "issuer_status.refreshed", level="info",
        symbol=symbol, count=len(snippets),
    )
    return snippets


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


def _search_duckduckgo_news(query: str) -> list[dict]:
    """DuckDuckGo HTML — pull recent results with their snippet text.

    Same scraping path as `_search_duckduckgo` but harvests the title,
    URL, and result snippet so the LLM augmentation gets useful
    context. No API key required.
    """
    import html as _html
    import re as _re
    from urllib.parse import unquote as _unquote
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "df": "m"},  # past month, no locale lock
            headers={**_UA, "Accept": "text/html"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_event(
            "web_discovery.news_backend_error", level="warn",
            backend="duckduckgo", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    # Each result block contains a result__a (URL + title) and
    # result__snippet sibling. Parse pairs in document order.
    block_re = _re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>'
        r'(.*?)</a>.*?'
        r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
        _re.IGNORECASE | _re.DOTALL,
    )
    tag_re = _re.compile(r"<[^>]+>")
    out: list[dict] = []
    for href, title_html, snip_html in block_re.findall(resp.text)[:5]:
        href = _html.unescape(href)
        if "/l/?" in href and "uddg=" in href:
            m = _re.search(r"uddg=([^&]+)", href)
            if m:
                href = _unquote(m.group(1))
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith(("http://", "https://")):
            continue
        title = _html.unescape(tag_re.sub("", title_html)).strip()
        snippet = _html.unescape(tag_re.sub("", snip_html)).strip()
        out.append({
            "title": title[:160],
            "url": href,
            "snippet": snippet[:300],
            "age": "recent",
        })
    return out


def _search_anthropic_news(query: str) -> list[dict]:
    """Anthropic native search for recent news snippets.

    Asks Claude to search the web and return structured news items.
    Same shape as the brave/serper news returns. Uses ANTHROPIC_API_KEY.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return []
    try:
        import anthropic
        import json as _json
    except ImportError:
        return []
    try:
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=os.environ.get(
                "SCA_WEB_SEARCH_MODEL", "claude-haiku-4-5-20251001",
            ),
            max_tokens=2048,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }],
            messages=[{
                "role": "user",
                "content": (
                    f"Search the web for recent news on: {query}\n\n"
                    "Return a JSON array of up to 5 items, each with: "
                    "{title, url, snippet, age}. Age is a short relative "
                    "phrase like 'last week' or 'today'. Only items from "
                    "the past month. No commentary, just the JSON."
                ),
            }],
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "web_discovery.news_backend_error", level="warn",
            backend="anthropic", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    # The last text block should be the JSON array.
    text_parts = []
    for block in msg.content:
        if getattr(block, "type", "") == "text":
            text_parts.append(block.text or "")
    raw = "\n".join(text_parts).strip()
    # Strip a markdown fence if Claude added one.
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        items = _json.loads(raw)
    except _json.JSONDecodeError:
        return []
    out: list[dict] = []
    for r in (items if isinstance(items, list) else [])[:5]:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        out.append({
            "title": str(r.get("title", ""))[:160],
            "url": str(r.get("url", "")),
            "snippet": str(r.get("snippet", ""))[:300],
            "age": str(r.get("age", "")),
        })
    return out


def _search_llm_news(query: str) -> list[dict]:
    """LLM-as-news-search — uses the configured LLM (DeepSeek).

    Same caveat as `_search_llm`: bounded by the model's training
    cutoff. Better than nothing — the LLM can recall well-known
    regulatory events, depegs, partnerships, etc. We trust the URLs
    only insofar as they're plausible; downstream code may HEAD-check.
    """
    try:
        from sca.llm import get_llm
        client = get_llm()
    except Exception:  # noqa: BLE001
        return []
    system = (
        "You are a recent-news recall tool. Given a query, return up "
        "to 3 well-known news items relevant to it from your training "
        "knowledge — regulatory actions, market events, partnerships, "
        "depegs, attestations. Be conservative: never invent stories. "
        "If you don't have high-confidence recall, return [] honestly."
    )
    prompt = (
        f"Query: {query}\n\n"
        "Return strict JSON array of items: "
        "[{\"title\": \"...\", \"url\": \"https://...\", "
        "\"snippet\": \"one-line summary\", \"age\": \"approx date\"}].\n"
        "No commentary."
    )
    try:
        raw = client.extract_json(
            system=system, prompt=prompt,
            schema={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "snippet": {"type": "string"},
                        "age": {"type": "string"},
                    },
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "web_discovery.news_backend_error", level="warn",
            backend="llm", error_class=type(exc).__name__,
            error_message=str(exc),
        )
        return []
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("news") or []
    out: list[dict] = []
    for r in (raw if isinstance(raw, list) else [])[:5]:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        u = str(r.get("url", "")).strip()
        if not u.startswith(("http://", "https://")):
            continue
        out.append({
            "title": str(r.get("title", ""))[:160],
            "url": u,
            "snippet": str(r.get("snippet", ""))[:300],
            "age": str(r.get("age", "")),
        })
    return out


_NEWS_BACKENDS = {
    "duckduckgo": _search_duckduckgo_news,
    "brave": _search_brave_news,
    "serper": _search_serper_news,
    "anthropic": _search_anthropic_news,
    "llm": _search_llm_news,
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
        symbol=symbol, news_kind=kind, provider=provider, query=query,
    )
    hits = backend(query)
    log_event(
        "web_discovery.news.results", level="info",
        symbol=symbol, news_kind=kind, count=len(hits),
    )
    return hits[:limit]
