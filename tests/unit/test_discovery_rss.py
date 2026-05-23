"""RSS / Atom discovery — central-bank feed pollers.

Hermetic by construction: every test mocks `requests.get` (the only
network call any poller makes — feeds and bodies both flow through
`snapshots.fetch_with_snapshot`). Nothing real is fetched, nothing real
is written to disk outside the per-test tmp_path.
"""
from __future__ import annotations

import pytest
import requests

from sca import discovery_rss, snapshots
from sca.discovery import DiscoveredSource
from sca.corpus import sources as corpus_sources


# ── shared fixtures ───────────────────────────────────────────────────
class FakeResp:
    def __init__(self, content: bytes, *, status: int = 200,
                 content_type: str = "application/xml"):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def _rss_feed(items: list[dict]) -> bytes:
    """A minimal but valid RSS 2.0 channel with the given items."""
    parts = ["<rss version='2.0'><channel><title>test</title>"]
    for it in items:
        parts.append("<item>")
        parts.append(f"<title>{it['title']}</title>")
        parts.append(f"<link>{it['link']}</link>")
        if "description" in it:
            parts.append(f"<description>{it['description']}</description>")
        if "pubDate" in it:
            parts.append(f"<pubDate>{it['pubDate']}</pubDate>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


def _atom_feed(items: list[dict]) -> bytes:
    """A minimal Atom feed — namespace-prefixed elements."""
    parts = [
        "<?xml version='1.0' encoding='utf-8'?>",
        "<feed xmlns='http://www.w3.org/2005/Atom'><title>test</title>",
    ]
    for it in items:
        parts.append("<entry>")
        parts.append(f"<title>{it['title']}</title>")
        parts.append(f"<link href='{it['link']}' rel='alternate'/>")
        if "summary" in it:
            parts.append(f"<summary>{it['summary']}</summary>")
        if "updated" in it:
            parts.append(f"<updated>{it['updated']}</updated>")
        parts.append("</entry>")
    parts.append("</feed>")
    return "".join(parts).encode("utf-8")


def _body_html(title: str, paragraphs: list[str]) -> bytes:
    paras = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1>{paras}</body></html>"
    ).encode("utf-8")


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    """Point the corpus registry at a tmp sources.yaml — same shape as
    tests/unit/test_discovery.py uses, so the registration path is
    safely exercised without touching the real file."""
    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        "sources:\n"
        "  - id: __seed__\n"
        "    title: 'seed'\n"
        "    tier: primary\n"
        "    status: excluded\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(corpus_sources, "_path", lambda: sources_yaml)

    from sca.store.file_store import FileStore
    import sca.store as store_mod

    def _fs(**kw):
        kw.setdefault("sources_path", sources_yaml)
        return FileStore(**kw)

    monkeypatch.setattr(store_mod, "FileStore", _fs)
    from sca.store import reset_store
    reset_store()
    corpus_sources.all_sources.cache_clear()
    yield sources_yaml
    corpus_sources.all_sources.cache_clear()
    reset_store()


# ── parse layer — RSS / Atom shape, malformed tolerance ───────────────
def test_parse_rss_extracts_items():
    feed = _rss_feed([
        {"title": "BIS speech on stablecoin frameworks",
         "link": "https://www.bis.org/speeches/sp250523.htm",
         "description": "Remarks on stablecoin oversight.",
         "pubDate": "Fri, 23 May 2025 09:00:00 GMT"},
        {"title": "CPMI release on cross-border payments",
         "link": "https://www.bis.org/cpmi/publ/d250.htm",
         "description": "Working group report."},
    ])
    parsed = discovery_rss._parse_feed(feed)
    assert len(parsed) == 2
    assert parsed[0]["link"] == "https://www.bis.org/speeches/sp250523.htm"
    assert "stablecoin" in parsed[0]["title"]
    assert parsed[0]["published"]  # pubDate flowed through


def test_parse_atom_extracts_items_with_href_link():
    feed = _atom_feed([
        {"title": "FSB update on global stablecoins",
         "link": "https://www.fsb.org/2025/05/update/",
         "summary": "Plenary review.",
         "updated": "2025-05-23T12:00:00Z"},
    ])
    parsed = discovery_rss._parse_feed(feed)
    assert len(parsed) == 1
    # The Atom link uses href= attribute, not text content.
    assert parsed[0]["link"] == "https://www.fsb.org/2025/05/update/"
    assert parsed[0]["title"].startswith("FSB update")


def test_parse_malformed_xml_returns_empty():
    """A broken feed is silent, not fatal — the poller logs and moves on."""
    parsed = discovery_rss._parse_feed(b"<rss><channel><item><not-closed")
    assert parsed == []


def test_parse_item_without_link_is_dropped():
    """An item with no link is useless — we need a citation target."""
    feed = b"<rss version='2.0'><channel><item><title>orphan</title></item></channel></rss>"
    parsed = discovery_rss._parse_feed(feed)
    assert parsed == []


# ── poller layer — happy path + isolation ─────────────────────────────
def test_poll_feed_returns_records(monkeypatch, isolated_registry):
    """Happy path: feed parses, body fetch succeeds, we get a record
    tagged with the right poller name + tier."""
    feed_url = "https://www.bis.org/rss/home.xml"
    item_url = "https://www.bis.org/speeches/sp250523.htm"

    def fake_get(url, *a, **kw):
        if url == feed_url:
            return FakeResp(_rss_feed([
                {"title": "BIS speech on stablecoin frameworks",
                 "link": item_url,
                 "description": "Remarks on oversight."},
            ]))
        if url == item_url:
            return FakeResp(
                _body_html("BIS speech", ["Body text of the speech."]),
                content_type="text/html",
            )
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)

    out = discovery_rss._poll_feed("bis", feed_url)
    assert len(out) == 1
    ds = out[0]
    assert isinstance(ds, DiscoveredSource)
    assert ds.poller == "rss-bis"
    assert ds.tier == "tier1_official"
    assert ds.url == item_url
    assert "Body text of the speech" in ds.body_excerpt
    assert ds.body_sha256


def test_item_filter_drops_off_topic_entries(monkeypatch, isolated_registry):
    """Fed speeches feed filters for digital-asset terms; an unrelated
    speech is dropped before the body-fetch budget is spent on it."""
    feed_url = "https://www.federalreserve.gov/feeds/speeches.xml"
    digital = "https://www.federalreserve.gov/newsevents/speech/x-digital.htm"
    monetary = "https://www.federalreserve.gov/newsevents/speech/x-monetary.htm"

    def fake_get(url, *a, **kw):
        if url == feed_url:
            return FakeResp(_rss_feed([
                {"title": "Stablecoin oversight remarks",
                 "link": digital, "description": "Digital asset frameworks."},
                {"title": "Monetary policy review",
                 "link": monetary, "description": "Interest rates."},
            ]))
        if url == digital:
            return FakeResp(_body_html("Stablecoin", ["Body."]))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)

    out = discovery_rss.POLLERS["rss-fed-speeches"]()
    urls = [ds.url for ds in out]
    assert digital in urls
    assert monetary not in urls  # filtered before body fetch


def test_dedup_via_sync_discovered(monkeypatch, isolated_registry):
    """An RSS-found source flows through the shared dedup ledger just
    like an HTML-found one — second sweep classifies it unchanged."""
    from sca import discovery

    feed_url = "https://www.bis.org/rss/home.xml"
    item_url = "https://www.bis.org/speeches/sp250523.htm"

    def fake_get(url, *a, **kw):
        if url == feed_url:
            return FakeResp(_rss_feed([
                {"title": "BIS speech",
                 "link": item_url,
                 "description": "Body."},
            ]))
        if url == item_url:
            return FakeResp(_body_html("BIS speech", ["Identical body."]))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    # Pin discovery POLLERS to empty so only the RSS surface contributes.
    monkeypatch.setattr(discovery, "POLLERS", {})
    monkeypatch.setattr(
        discovery_rss, "POLLERS",
        {"rss-bis": discovery_rss.POLLERS["rss-bis"]},
    )
    # Drop blog pollers too so nothing else fires.
    from sca import discovery_blogs
    monkeypatch.setattr(discovery_blogs, "POLLERS", {})

    first = discovery.sync_discovered()
    assert len(first.new) == 1
    rss_id = first.new[0]
    assert rss_id.startswith("rss-bis-")

    second = discovery.sync_discovered()
    assert second.new == []
    assert rss_id in second.unchanged


def test_feed_fetch_failure_does_not_crash(monkeypatch, isolated_registry):
    """A 503 on the feed URL → poller returns [], no exception bubbles."""
    def blow_up(*a, **kw):
        raise requests.ConnectionError("DNS failure")

    monkeypatch.setattr(requests, "get", blow_up)
    out = discovery_rss._poll_feed("bis", "https://www.bis.org/rss/home.xml")
    assert out == []


def test_per_item_failure_does_not_break_the_feed(monkeypatch, isolated_registry):
    """One body fetch fails (DNS error) → that item's record falls back
    to the feed summary; the other item still has its full body."""
    feed_url = "https://www.bis.org/rss/home.xml"
    good = "https://www.bis.org/speeches/sp-good.htm"
    bad = "https://www.bis.org/speeches/sp-bad.htm"

    def fake_get(url, *a, **kw):
        if url == feed_url:
            return FakeResp(_rss_feed([
                {"title": "Good speech", "link": good,
                 "description": "good summary"},
                {"title": "Bad speech", "link": bad,
                 "description": "fallback summary text"},
            ]))
        if url == good:
            return FakeResp(_body_html("Good", ["full body of the good one"]))
        if url == bad:
            raise requests.ConnectionError("DNS failure")
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    out = discovery_rss._poll_feed("bis", feed_url)
    assert len(out) == 2
    bodies = {ds.url: ds.body_excerpt for ds in out}
    assert "full body of the good one" in bodies[good]
    # Bad item falls back to the summary text rather than disappearing.
    assert "fallback summary" in bodies[bad]


def test_discover_all_logs_and_returns_empty_on_total_failure(monkeypatch, isolated_registry):
    """Every feed network call raises — sweep returns []."""
    def blow_up(*a, **kw):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(requests, "get", blow_up)
    results = discovery_rss.discover_all()
    assert results == []
