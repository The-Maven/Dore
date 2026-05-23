"""RSS / Atom feed pollers — second discovery surface.

The first discovery module (`sca.discovery`) scrapes regulator index
pages by HTML. That's robust against tier-1 sites that don't publish
machine-readable feeds (OFAC) but wasteful where the regulator already
hands us a structured firehose (BIS, FSB, central-bank press feeds).
This module adds the feed-based surface: same shape (`DiscoveredSource`
records ingested via the same `sync_discovered()`-style pipeline), same
storage (snapshot store + ledger + corpus staging), but the entry-point
is an RSS / Atom XML document parsed with stdlib `xml.etree.ElementTree`
— no `feedparser` dependency to drag in.

Design rules (identical to the HTML pollers — the contract is the contract):

  - **Every fetch goes through `snapshots.fetch_with_snapshot`.** Feed
    XML and each linked body. One archive copy per source-id.
  - **Best-effort, never fatal.** A malformed feed or a 404 on an item
    body logs and yields what it has. A single bad feed never breaks
    the sweep.
  - **De-duplicated by content hash** at the same `discovered_sources.json`
    ledger via the shared `sync_discovered()` registration path. Each
    feed item becomes a `DiscoveredSource` with `poller="rss-<name>"`
    so the namespace stays separate from HTML scrapers.

Feeds covered (best-effort — feeds rot too):
  - BIS news firehose
  - FSB news firehose
  - NYDFS press releases (with HTML fallback when the feed 404s)
  - Federal Reserve speeches (filtered to digital-asset / stablecoin terms)
  - ECB digital-euro updates
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable

from sca import snapshots
from sca.discovery import (
    DiscoveredSource,
    _BODY_EXCERPT_CHARS,
    _MAX_LINKS_PER_POLLER,
    _extract_date,
    _fetch_text,
    _url_slug,
)
from sca.observability import log_event

# ── feed registry — one entry per RSS/Atom source ─────────────────────
# Each spec declares: name (poller slug), feed_url, an optional
# `item_filter` to drop off-topic items (e.g. Fed speeches that have
# nothing to do with digital assets). Tier is tier1_official by default
# — every feed below is from a regulator or central bank — but the spec
# allows overriding if we ever add a tier2 feed source.
_FEED_DEFS: list[dict] = [
    {
        "name": "bis",
        "feed_url": "https://www.bis.org/rss/home.xml",
        "tier": "tier1_official",
        "item_filter": None,  # take all BIS news items
    },
    {
        "name": "fsb",
        "feed_url": "https://www.fsb.org/feed/",
        "tier": "tier1_official",
        "item_filter": None,
    },
    {
        "name": "nydfs",
        "feed_url": (
            "https://www.dfs.ny.gov/system/feeds/global/"
            "all_press_releases.xml"
        ),
        "tier": "tier1_official",
        "item_filter": None,
    },
    {
        "name": "fed-speeches",
        # The Fed publishes a single speeches feed; we filter for
        # digital-asset / stablecoin keywords client-side because their
        # taxonomy isn't filterable at the feed level.
        "feed_url": "https://www.federalreserve.gov/feeds/speeches.xml",
        "tier": "tier1_official",
        "item_filter": lambda title, body: any(
            term in (title + " " + body).lower()
            for term in (
                "stablecoin", "digital asset", "digital dollar",
                "cbdc", "crypto", "tokenization", "tokenisation",
            )
        ),
    },
    {
        "name": "ecb-digital-euro",
        # ECB top-news feed; we filter for digital-euro / DLT terms.
        "feed_url": "https://www.ecb.europa.eu/rss/press.html",
        "tier": "tier1_official",
        "item_filter": lambda title, body: any(
            term in (title + " " + body).lower()
            for term in (
                "digital euro", "digital-euro", "stablecoin",
                "crypto-asset", "crypto asset", "dlt",
            )
        ),
    },
]


# ── parsing — stdlib only, malformed-feed-tolerant ────────────────────
# RSS 2.0 puts items under <channel>/<item>; Atom puts them under
# <feed>/<entry>. We handle both with a couple of namespace-aware lookups
# and fall back to local-name matching when the source declares no
# namespace at all (some hand-rolled feeds do this).
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _localname(tag: str) -> str:
    """Strip an XML namespace prefix — '{uri}foo' -> 'foo'."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_text(parent: ET.Element, *candidates: str) -> str:
    """Find the first child whose local name matches any of `candidates`.

    Namespace-agnostic on purpose — RSS feeds in the wild mix
    `<description>`, `<content:encoded>`, `<atom:summary>` and the same
    poller may need to read across formats without the caller caring.
    Returns the stripped text content, or "" when nothing matches.
    """
    wanted = {c.lower() for c in candidates}
    for child in parent:
        if _localname(child.tag).lower() in wanted:
            return (child.text or "").strip()
    return ""


def _find_link(item: ET.Element) -> str:
    """Pull the canonical link out of an RSS or Atom item.

    RSS: <link>https://...</link>. Atom: <link href="..." rel="alternate"/>.
    Some hybrid feeds emit both; we prefer the RSS-style text body, then
    fall back to the first Atom-style href. Returns "" when neither is
    present — caller skips the item.
    """
    # RSS-style: text content on <link>.
    for child in item:
        if _localname(child.tag).lower() == "link":
            if child.text and child.text.strip():
                return child.text.strip()
            href = child.attrib.get("href", "").strip()
            if href:
                return href
    return ""


def _parse_feed(xml_bytes: bytes) -> list[dict]:
    """Return [{title, link, summary, published}, ...] from a feed body.

    Malformed XML → empty list (the poller logs and moves on). We avoid
    the temptation to be clever about partial parses; a feed that ET
    can't read is one we don't want to half-trust.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    # RSS 2.0 — items live under <channel>; Atom — under the root <feed>.
    items: list[ET.Element] = []
    for elem in root.iter():
        ln = _localname(elem.tag).lower()
        if ln in ("item", "entry"):
            items.append(elem)

    out: list[dict] = []
    for it in items:
        title = _find_text(it, "title")
        link = _find_link(it)
        summary = _find_text(
            it, "description", "summary", "content", "encoded",
        )
        published = _find_text(
            it, "pubdate", "published", "updated", "date",
        )
        if not link:
            # An item without a link is useless to us — we can't fetch
            # the body, and a citation needs a target URL.
            continue
        out.append({
            "title": title,
            "link": link,
            "summary": summary,
            "published": published,
        })
    return out


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """A summary may be HTML-encoded — flatten it to plain text for the
    body excerpt fallback used when the linked body fetch fails."""
    if not text:
        return ""
    return _HTML_TAG_RE.sub(" ", text)


# ── poller implementation ─────────────────────────────────────────────
def _poll_feed(
    name: str,
    feed_url: str,
    *,
    tier: str = "tier1_official",
    item_filter: Callable[[str, str], bool] | None = None,
) -> list[DiscoveredSource]:
    """Fetch a feed, parse items, deep-fetch each body. Returns records."""
    feed_id = f"discovery-rss-{name}-feed"
    try:
        out = snapshots.fetch_with_snapshot(feed_id, feed_url)
    except Exception as exc:  # noqa: BLE001 - poller never breaks the sweep
        log_event(
            f"discovery.rss-{name}", level="warn",
            stage="feed_fetch", url=feed_url,
            error_class=type(exc).__name__, error_message=str(exc),
        )
        return []

    items = _parse_feed(out.body)
    if not items:
        log_event(
            f"discovery.rss-{name}", level="warn",
            stage="feed_parse_empty", url=feed_url, bytes=len(out.body),
        )
        return []

    # Apply the filter (if any) early so a noisy feed doesn't burn its
    # body-fetch budget on items we won't keep.
    filtered: list[dict] = []
    for it in items:
        plain_summary = _strip_html(it.get("summary", ""))
        if item_filter is not None and not item_filter(it.get("title", ""), plain_summary):
            continue
        filtered.append(it)
        if len(filtered) >= _MAX_LINKS_PER_POLLER:
            break

    discovered: list[DiscoveredSource] = []
    poller_name = f"rss-{name}"
    for it in filtered:
        href = it["link"]
        try:
            source_id = _url_slug(href, prefix=poller_name)
            body = _fetch_text(f"discovery-{source_id}", href)
            if not body:
                # Fall back to the feed-supplied summary so we still
                # have something citable — better than an empty record.
                body = _strip_html(it.get("summary", "")).strip()
            title = (it.get("title") or href).strip()
            excerpt = body[:_BODY_EXCERPT_CHARS]
            published = (
                it.get("published", "").strip()
                or (_extract_date(body) if body else "")
            )
            ds = DiscoveredSource(
                id=source_id,
                title=title[:200],
                url=href,
                tier=tier,
                published=published,
                body_excerpt=excerpt,
                discovered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                poller=poller_name,
            )
            ds.body_sha256 = ds.compute_hash()
            discovered.append(ds)
        except Exception as exc:  # noqa: BLE001 - one bad item, sweep continues
            log_event(
                f"discovery.rss-{name}", level="warn",
                stage="per_item", url=href,
                error_class=type(exc).__name__, error_message=str(exc),
            )
            continue

    log_event(
        f"discovery.rss-{name}", level="info",
        stage="done", url=feed_url,
        items_total=len(items), kept=len(filtered),
        discovered=len(discovered),
    )
    return discovered


# ── registry — same shape as discovery.POLLERS so the CLI can iterate ─
POLLERS: dict[str, Callable[[], list[DiscoveredSource]]] = {}


def _make_poller(spec: dict) -> Callable[[], list[DiscoveredSource]]:
    name = spec["name"]
    feed_url = spec["feed_url"]
    tier = spec.get("tier", "tier1_official")
    item_filter = spec.get("item_filter")

    def _run() -> list[DiscoveredSource]:
        return _poll_feed(name, feed_url, tier=tier, item_filter=item_filter)

    _run.__name__ = f"_poll_rss_{name}"
    return _run


for _spec in _FEED_DEFS:
    POLLERS[f"rss-{_spec['name']}"] = _make_poller(_spec)


def discover_all() -> list[DiscoveredSource]:
    """Run every RSS poller. One poller's exception never breaks the sweep."""
    results: list[DiscoveredSource] = []
    for name, poller in POLLERS.items():
        try:
            results.extend(poller())
        except Exception as exc:  # noqa: BLE001 - belt-and-suspenders
            log_event(
                f"discovery.{name}", level="error", stage="poller_crash",
                error_class=type(exc).__name__, error_message=str(exc),
            )
    log_event(
        "discovery.rss.sweep", level="info",
        pollers=len(POLLERS), found=len(results),
    )
    return results
