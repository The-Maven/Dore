"""Authoritative blog pollers — third discovery surface (tier2_industry).

Issuer and industry blogs aren't law. They're commercial commentary —
sometimes self-serving, sometimes the only timely take on a fresh event.
Worth indexing as tier2 so the analyst can quote them with the right
weight (cited but not authoritative) and a human can opt any specific
source out via `sca curate`.

Same architecture as `sca.discovery`'s HTML scrapers: each blog has an
index URL and a link-match predicate; the shared `_poll_generic` does
the rest (fetch index, extract candidate links, deep-fetch bodies,
return `DiscoveredSource` records). The only difference is `tier` —
these enter as `tier2_industry`.

Targets (subject to predictable site redesigns):

  - Circle blog              (USDC, EURC issuer)
  - Paxos newsroom           (PYUSD, USDP, USDG issuer)
  - Tether news              (USDT issuer)
  - Chainalysis blog         (analytics / sanctions)
  - TRM Labs blog            (analytics / sanctions)
  - Elliptic blog            (analytics / sanctions)

Predicates are loose — the cost of pulling in a few off-topic posts is
that a curator excludes them once; the cost of being too strict is
silently missing the next stablecoin advisory. Loose wins.
"""
from __future__ import annotations

from typing import Callable

from sca.discovery import DiscoveredSource, _poll_generic
from sca.observability import log_event

# ── blog registry ─────────────────────────────────────────────────────
# Each spec: name (poller slug), index_url, link_match predicate. The
# predicate takes (absolute_href, link_text) and returns True iff the
# link looks like a recent post we want to record.
#
# Naming: poller name doubles as the corpus-id prefix; we use the
# `blog-<issuer>` convention so the namespace makes provenance obvious
# in `sca sources` output (e.g. `blog-circle-...`).
_BLOG_DEFS: list[dict] = [
    {
        "name": "blog-circle",
        "index_url": "https://www.circle.com/blog",
        # Circle's CMS puts posts under /blog/<slug>; the index itself
        # has no slug after /blog so we filter for at least one further
        # path segment.
        "link_match": lambda href, text: (
            "circle.com/blog/" in href.lower()
            and href.rstrip("/").rsplit("/", 1)[-1] != "blog"
        ),
    },
    {
        "name": "blog-paxos",
        "index_url": "https://www.paxos.com/insights/",
        # Paxos uses /insights/<slug> (post) or /press-release/<slug>;
        # both are worth indexing.
        "link_match": lambda href, text: (
            "paxos.com/" in href.lower()
            and (
                "/insights/" in href.lower()
                or "/press-release/" in href.lower()
                or "/blog/" in href.lower()
            )
            and href.rstrip("/").rsplit("/", 1)[-1] not in {
                "insights", "press-release", "blog",
            }
        ),
    },
    {
        "name": "blog-tether",
        "index_url": "https://tether.to/en/news/",
        "link_match": lambda href, text: (
            "tether.to/" in href.lower()
            and "/news/" in href.lower()
            and href.rstrip("/").rsplit("/", 1)[-1] != "news"
        ),
    },
    {
        "name": "blog-chainalysis",
        "index_url": "https://www.chainalysis.com/blog/",
        "link_match": lambda href, text: (
            "chainalysis.com/blog/" in href.lower()
            and href.rstrip("/").rsplit("/", 1)[-1] != "blog"
        ),
    },
    {
        "name": "blog-trmlabs",
        "index_url": "https://www.trmlabs.com/resources/blog",
        "link_match": lambda href, text: (
            "trmlabs.com/" in href.lower()
            and ("/post/" in href.lower() or "/blog/" in href.lower())
            and href.rstrip("/").rsplit("/", 1)[-1] not in {"post", "blog"}
        ),
    },
    {
        "name": "blog-elliptic",
        "index_url": "https://www.elliptic.co/blog",
        "link_match": lambda href, text: (
            "elliptic.co/blog/" in href.lower()
            and href.rstrip("/").rsplit("/", 1)[-1] != "blog"
        ),
    },
]


# ── tier2 wrapper around _poll_generic ────────────────────────────────
def _poll_tier2(
    name: str,
    index_url: str,
    link_match: Callable[[str, str], bool],
) -> list[DiscoveredSource]:
    """Run the shared HTML poller, then re-tier every record as tier2.

    `_poll_generic` hard-codes `tier="tier1_official"` because the
    original six pollers are all government / standards bodies. For
    industry blogs we want `tier2_industry` so curators can filter the
    corpus by tier and the LLM can weigh citations appropriately.
    """
    records = _poll_generic(name, index_url, link_match)
    out: list[DiscoveredSource] = []
    for ds in records:
        # DiscoveredSource is a regular dataclass — mutate in place.
        ds.tier = "tier2_industry"
        # Hash is over body, not tier, so it doesn't need recomputation.
        out.append(ds)
    return out


# ── registry — same shape as discovery.POLLERS so the CLI can iterate ─
POLLERS: dict[str, Callable[[], list[DiscoveredSource]]] = {}


def _make_poller(spec: dict) -> Callable[[], list[DiscoveredSource]]:
    name = spec["name"]
    index_url = spec["index_url"]
    link_match = spec["link_match"]

    def _run() -> list[DiscoveredSource]:
        return _poll_tier2(name, index_url, link_match)

    _run.__name__ = f"_poll_{name.replace('-', '_')}"
    return _run


for _spec in _BLOG_DEFS:
    POLLERS[_spec["name"]] = _make_poller(_spec)


def discover_all() -> list[DiscoveredSource]:
    """Run every blog poller. One poller's exception never breaks the sweep."""
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
        "discovery.blogs.sweep", level="info",
        pollers=len(POLLERS), found=len(results),
    )
    return results
