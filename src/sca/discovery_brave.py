"""Brave-backed corpus discovery — finds new regulatory news the RSS /
HTML pollers don't cover.

The existing pollers (`POLLERS` in `sca.discovery`, RSS feeds in
`sca.discovery_rss`, blog scrapers in `sca.discovery_blogs`) cover a
curated set of issuers and regulators. This module complements them:
runs a small set of typed Brave queries against current stablecoin /
regulator / OFAC topics, harvests the candidate URLs, fetches each
page's body, and emits `DiscoveredSource` records the standard
`sync_discovered` path can register.

Why generic Brave queries here rather than per-source pollers: the
pollers are precise but inflexible — they only find what their RSS
feed or HTML structure exposes. A new EU regulator publishes a new
stablecoin policy paper and the existing pollers miss it. A Brave
query for "stablecoin regulation 2026 site:europa.eu" finds it.

Quota discipline: bounded to ~6 queries per sweep (one per topic),
which is ~180/month against Brave's 2k free tier — comfortable
alongside the attestation-discovery quota. Skipped entirely when no
search provider is configured.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sca.observability import log_event

# Module-level constants
_TIER = "tier1_official"
_POLLER = "brave"
_MAX_PER_QUERY = 3


# Curated topical queries — each one fronts a specific intelligence
# gap the existing pollers leave open. Tuned for precision over recall
# (the more specific the query, the less spam in the candidates).
_QUERIES = [
    "stablecoin regulation 2026 site:europa.eu OR site:treasury.gov",
    "stablecoin enforcement action 2026 SEC OR CFTC OR FinCEN",
    "MiCA Title III stablecoin guidance 2026",
    "OFAC SDN list stablecoin sanctioned addresses 2026",
    "Federal Reserve stablecoin payment system 2026",
    "FSB G20 stablecoin recommendations 2026",
]


def _slug_from_url(url: str) -> str:
    """Build a stable ledger slug from the URL."""
    from urllib.parse import urlparse
    p = urlparse(url)
    host = p.netloc.replace(".", "-")
    path = p.path.strip("/").replace("/", "-").replace(".", "-")[:120]
    return f"brave-{host}-{path}"[:180] if path else f"brave-{host}"


def discover_all():
    """Run the Brave-backed regulatory-news sweep.

    Each query runs through the configured search backend (combo by
    default: Brave first, DuckDuckGo fallback). Per-query body fetch
    is bounded; an empty or unreachable page yields zero records for
    that URL but never breaks the sweep. Returns
    list[DiscoveredSource] for `sync_discovered()`.
    """
    # Avoid circular imports.
    from sca.discovery import DiscoveredSource, _fetch_text, _url_slug
    if not os.environ.get("SCA_WEB_SEARCH_PROVIDER", "").strip():
        return []
    try:
        from sca.web_discovery import _BACKENDS, _provider
    except Exception as exc:  # noqa: BLE001
        log_event(
            "discovery.brave.import_failed", level="warn",
            error_class=type(exc).__name__,
        )
        return []
    backend = _BACKENDS.get(_provider())
    if backend is None:
        return []

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[DiscoveredSource] = []
    seen_urls: set[str] = set()

    for query in _QUERIES:
        try:
            urls = backend(query)
        except Exception as exc:  # noqa: BLE001 - never break the sweep
            log_event(
                "discovery.brave.query_failed", level="warn",
                query=query, error_class=type(exc).__name__,
            )
            continue
        log_event(
            "discovery.brave.query", level="info",
            query=query, count=len(urls),
        )
        for url in urls[:_MAX_PER_QUERY]:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            slug = _url_slug(url, prefix="brave")
            try:
                body = _fetch_text(slug, url)
            except Exception as exc:  # noqa: BLE001 - per-URL failure is fine
                log_event(
                    "discovery.brave.fetch_failed", level="info",
                    url=url, error_class=type(exc).__name__,
                )
                continue
            if not body or len(body) < 400:
                # Too thin to be useful as a corpus source.
                continue
            # Title is the URL's last path segment cleaned up; the
            # ingest pipeline derives a better title from the body
            # when it's parsed later.
            title_guess = url.split("/")[-1].split("?")[0]
            title_guess = title_guess.replace("-", " ").replace("_", " ")
            title_guess = title_guess.replace(".html", "").replace(".pdf", "")
            title_guess = title_guess.strip() or url
            out.append(DiscoveredSource(
                id=slug,
                title=title_guess[:200],
                url=url,
                tier=_TIER,
                published="",
                body_excerpt=body[:5000],
                discovered_at=now,
                poller=_POLLER,
            ))

    log_event(
        "discovery.brave.sweep_done", level="info",
        queries=len(_QUERIES), proposed=len(out),
    )
    return out
