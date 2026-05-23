"""Authoritative blog discovery — tier2_industry scrapers.

Hermetic: every test mocks `requests.get`. The blog scrapers reuse
`sca.discovery._poll_generic`, so we're mostly verifying the link-match
predicates, the tier override, and the registration path. Site
redesigns are the realistic failure mode — one test exercises a
predicate-mismatch shape to confirm we degrade quietly.
"""
from __future__ import annotations

import pytest
import requests

from sca import discovery, discovery_blogs
from sca.corpus import sources as corpus_sources


# ── shared fixtures ───────────────────────────────────────────────────
class FakeResp:
    def __init__(self, content: bytes, *, status: int = 200,
                 content_type: str = "text/html"):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def _index(anchors: list[tuple[str, str]]) -> bytes:
    """An index page — list of (href, text)."""
    inner = "\n".join(f'<a href="{h}">{t}</a>' for h, t in anchors)
    return (
        f"<html><head><title>blog index</title></head>"
        f"<body><h1>Blog</h1>{inner}</body></html>"
    ).encode("utf-8")


def _body(title: str, paragraphs: list[str]) -> bytes:
    paras = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1>{paras}</body></html>"
    ).encode("utf-8")


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
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

    # Redirect corpus + ledger paths so staging writes land in tmp_path
    # instead of leaking fixture markdown into the live corpus/staging/.
    from sca import config
    tmp_corpus = tmp_path / "corpus"
    (tmp_corpus / "staging").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_corpus)
    monkeypatch.setattr(
        discovery, "DISCOVERED_PATH",
        tmp_path / "discovered_sources.json",
    )

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


# ── registry shape ────────────────────────────────────────────────────
def test_blog_pollers_registered():
    """Every blog target from the spec has a poller registered."""
    expected = {
        "blog-circle", "blog-paxos", "blog-tether",
        "blog-chainalysis", "blog-trmlabs", "blog-elliptic",
    }
    assert expected.issubset(discovery_blogs.POLLERS.keys())


def test_tier2_industry_accepted_by_propose_source(isolated_registry):
    """The new tier must be in VALID_TIERS — propose_source rejects unknowns."""
    from sca.corpus.sources import propose_source, VALID_TIERS

    assert "tier2_industry" in VALID_TIERS
    src = propose_source(
        "blog-test", title="t", tier="tier2_industry",
        url="https://example.com",
    )
    assert src.tier == "tier2_industry"


# ── happy path — Circle blog scrape ───────────────────────────────────
def test_circle_blog_returns_tier2_records(monkeypatch, isolated_registry):
    """A Circle blog post is found, fetched, and tagged tier2_industry."""
    index_url = "https://www.circle.com/blog"
    post_url = "https://www.circle.com/blog/usdc-supply-update-q2"

    def fake_get(url, *a, **kw):
        if url == index_url:
            return FakeResp(_index([
                (post_url, "USDC supply update Q2 2025"),
                # Pure /blog index link — must be excluded by predicate.
                ("https://www.circle.com/blog", "Blog home"),
            ]))
        if url == post_url:
            return FakeResp(_body(
                "USDC supply update",
                ["USDC circulation reached new levels in Q2 2025."],
            ))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    out = discovery_blogs.POLLERS["blog-circle"]()
    assert len(out) == 1
    ds = out[0]
    assert ds.tier == "tier2_industry"
    assert ds.poller == "blog-circle"
    assert ds.url == post_url
    assert "USDC circulation" in ds.body_excerpt


def test_chainalysis_blog_returns_tier2_records(monkeypatch, isolated_registry):
    """Smoke-test a second predicate path so a future redesign of one
    site doesn't silently take down two scrapers at once."""
    index_url = "https://www.chainalysis.com/blog/"
    post_url = "https://www.chainalysis.com/blog/stablecoin-flows-2025/"

    def fake_get(url, *a, **kw):
        if url == index_url:
            return FakeResp(_index([
                (post_url, "Stablecoin flows in 2025"),
            ]))
        if url == post_url:
            return FakeResp(_body(
                "Flows",
                ["Quarterly recap of stablecoin transfer volume."],
            ))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    out = discovery_blogs.POLLERS["blog-chainalysis"]()
    assert len(out) == 1
    assert out[0].tier == "tier2_industry"
    assert out[0].poller == "blog-chainalysis"


# ── site-redesign tolerance ───────────────────────────────────────────
def test_site_redesign_yields_zero_records_not_crash(monkeypatch, isolated_registry):
    """A hypothetical Tether redesign moves posts to /press/ — none of
    those match the /news/ predicate, so we get zero records. No crash,
    no exception — just an empty list that a curator can spot."""
    index_url = "https://tether.to/en/news/"

    def fake_get(url, *a, **kw):
        if url == index_url:
            # All links now under /press/ — predicate requires /news/.
            return FakeResp(_index([
                ("https://tether.to/en/press/announcement-1", "Announcement 1"),
                ("https://tether.to/en/press/announcement-2", "Announcement 2"),
            ]))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    out = discovery_blogs.POLLERS["blog-tether"]()
    assert out == []  # graceful empty, no exception


def test_index_fetch_failure_does_not_crash(monkeypatch, isolated_registry):
    """503 on the index URL → poller returns []."""
    def blow_up(*a, **kw):
        raise requests.ConnectionError("DNS failure")

    monkeypatch.setattr(requests, "get", blow_up)
    out = discovery_blogs.POLLERS["blog-circle"]()
    assert out == []


# ── dedup through sync_discovered ─────────────────────────────────────
def test_sync_registers_blog_with_tier2_industry(monkeypatch, isolated_registry):
    """End-to-end: a blog post discovered, registered to the corpus
    with tier=tier2_industry, body staged to corpus/staging."""
    from sca import config, discovery_rss

    index_url = "https://www.circle.com/blog"
    post_url = "https://www.circle.com/blog/usdc-supply-update-q2"

    def fake_get(url, *a, **kw):
        if url == index_url:
            return FakeResp(_index([(post_url, "USDC supply update")]))
        if url == post_url:
            return FakeResp(_body("USDC", ["body text"]))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    # Pin everything else to empty so only Circle fires.
    monkeypatch.setattr(discovery, "POLLERS", {})
    monkeypatch.setattr(discovery_rss, "POLLERS", {})
    monkeypatch.setattr(
        discovery_blogs, "POLLERS",
        {"blog-circle": discovery_blogs.POLLERS["blog-circle"]},
    )

    report = discovery.sync_discovered()
    assert len(report.new) == 1
    sid = report.new[0]
    src = next(s for s in corpus_sources.all_sources() if s.id == sid)
    assert src.tier == "tier2_industry"
    # Body was staged for the ingester.
    staged = config.CORPUS_DIR / "staging" / f"{sid}.md"
    assert staged.exists()
    assert "body text" in staged.read_text(encoding="utf-8")
