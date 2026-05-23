"""Daily auto-discovery sweep — six tier1_official pollers.

Hermetic by construction: every test mocks `requests.get` (the only
network call any poller makes — they all flow through
`snapshots.fetch_with_snapshot`). Nothing real is fetched, nothing real
is written to `corpus/sources.yaml` (the source registry is monkey-
patched per test to a tmp file).
"""
from __future__ import annotations

import json

import pytest
import requests

from sca import discovery
from sca import snapshots
from sca.corpus import sources as corpus_sources


# ── shared fixtures ───────────────────────────────────────────────────
class FakeResp:
    """Mirror of tests/unit/test_snapshots.py FakeResp."""

    def __init__(self, content: bytes, *, status: int = 200,
                 content_type: str = "text/html"):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def _index_html(poller_name: str, hrefs: list[str]) -> bytes:
    """An HTML index page with an <a> per href — minimal but real."""
    anchors = "\n".join(
        f'<a href="{h}">{poller_name} item {i}</a>' for i, h in enumerate(hrefs)
    )
    return (
        f"<html><head><title>{poller_name} index</title></head>"
        f"<body><h1>{poller_name}</h1>{anchors}</body></html>"
    ).encode("utf-8")


def _body_html(title: str, paragraphs: list[str]) -> bytes:
    paras = "\n".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><h1>{title}</h1>{paras}</body></html>"
    ).encode("utf-8")


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    """Point the corpus registry at a tmp sources.yaml.

    `propose_source` appends to the file backing `all_sources()`. Tests
    can register sources freely without touching the real registry.
    """
    sources_yaml = tmp_path / "sources.yaml"
    # Block-style sources list with one seed source so propose_source's
    # append (`\n  - id: ...`) produces valid YAML. An inline `sources: []`
    # doesn't accept more items, and a key with no value parses to None.
    sources_yaml.write_text(
        "sources:\n"
        "  - id: __seed__\n"
        "    title: 'seed'\n"
        "    tier: primary\n"
        "    status: excluded\n",
        encoding="utf-8",
    )

    # The corpus/sources module reads its path through config.CORPUS_DIR;
    # easier to swap the helper that exposes the path.
    monkeypatch.setattr(corpus_sources, "_path", lambda: sources_yaml)

    # Redirect the corpus + data dirs so discovery's staging writes and
    # ledger journals land in tmp_path, not the live repo. Without this,
    # every discovery test leaks fixture markdown into corpus/staging/.
    from sca import config
    tmp_corpus = tmp_path / "corpus"
    (tmp_corpus / "staging").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_corpus)
    monkeypatch.setattr(
        discovery, "DISCOVERED_PATH",
        tmp_path / "discovered_sources.json",
    )

    # The Store also reads sources.yaml directly — point it at the same file.
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


# ── URL slug derivation ───────────────────────────────────────────────
def test_url_slug_is_stable_per_url():
    a = discovery._url_slug(
        "https://ofac.treasury.gov/recent-actions/20250523", prefix="ofac",
    )
    b = discovery._url_slug(
        "https://ofac.treasury.gov/recent-actions/20250523", prefix="ofac",
    )
    assert a == b
    assert a.startswith("ofac-")


def test_url_slug_prefix_keeps_pollers_separate():
    # Same path, different regulator → different id (no collision).
    ofac = discovery._url_slug("https://x.gov/foo", prefix="ofac")
    bis = discovery._url_slug("https://x.gov/foo", prefix="bis")
    assert ofac != bis


# ── HTML extractors ───────────────────────────────────────────────────
def test_extract_links_absolutises_relative_urls():
    html = '<a href="/recent-actions/20250101">item</a>'
    links = discovery._extract_links(html, "https://ofac.treasury.gov/recent-actions")
    assert len(links) == 1
    href, text = links[0]
    assert href == "https://ofac.treasury.gov/recent-actions/20250101"
    assert text == "item"


def test_extract_text_strips_scripts_and_styles():
    html = (
        "<html><head><style>p{color:red}</style>"
        "<script>alert(1)</script></head>"
        "<body><p>visible body text</p></body></html>"
    )
    text, _ = discovery._extract_text(html)
    assert "visible body text" in text
    assert "alert(1)" not in text
    assert "color:red" not in text


# ── per-poller robustness ─────────────────────────────────────────────
def test_poll_returns_discovered_sources(monkeypatch, isolated_registry):
    """Happy path: index + body fetch both succeed, we get records."""
    def fake_get(url, *a, **kw):
        if "recent-actions" in url and url.endswith("/recent-actions"):
            return FakeResp(_index_html(
                "ofac",
                ["/recent-actions/20250523_iran"],
            ))
        return FakeResp(_body_html(
            "Iran-related designations",
            ["On May 23, 2025 OFAC designated three new entities."],
        ))

    monkeypatch.setattr(requests, "get", fake_get)

    out = discovery._poll_ofac()
    assert len(out) == 1
    ds = out[0]
    assert ds.poller == "ofac"
    assert ds.tier == "tier1_official"
    assert ds.url.endswith("/recent-actions/20250523_iran")
    assert "Iran-related designations" in ds.title or "designated" in ds.body_excerpt
    assert ds.body_sha256  # populated by compute_hash() in the poller


def test_one_failing_poller_does_not_break_the_sweep(monkeypatch, isolated_registry):
    """OFAC's index 404s; BIS still returns one record. discover_all() returns the BIS one.
    Other pollers also network-block but return [] — and that's fine."""

    bis_index = "https://www.bis.org/list/cpmi/index.htm"
    bis_paper = "https://www.bis.org/cpmi/publ/d999.htm"

    def fake_get(url, *a, **kw):
        if "ofac.treasury.gov" in url:
            # The poller's index fetch fails outright.
            return FakeResp(b"", status=503)
        if url == bis_index:
            return FakeResp(_index_html("bis", [bis_paper]))
        if url == bis_paper:
            return FakeResp(_body_html(
                "CPMI on stablecoin governance",
                ["This paper covers governance frameworks for stablecoins."],
            ))
        # Every other poller's URL: fail. discover_all() must not crash.
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)

    results = discovery.discover_all()
    # We get exactly the BIS record — OFAC and the rest failed cleanly.
    ids = [r.id for r in results]
    assert any(r.poller == "bis" for r in results), \
        "expected at least one BIS record"
    assert not any(r.poller == "ofac" for r in results), \
        "OFAC poller should have yielded nothing on 503"
    # And the sweep returned, didn't crash.
    assert isinstance(results, list)
    assert ids  # at least the BIS record


def test_per_link_failure_does_not_break_a_poller(monkeypatch, isolated_registry):
    """If one body fetch fails, the poller still returns records for the others."""
    bis_index = "https://www.bis.org/list/cpmi/index.htm"
    ok_paper = "https://www.bis.org/cpmi/publ/dgood.htm"
    bad_paper = "https://www.bis.org/cpmi/publ/dbad.htm"

    def fake_get(url, *a, **kw):
        if url == bis_index:
            return FakeResp(_index_html("bis", [ok_paper, bad_paper]))
        if url == ok_paper:
            return FakeResp(_body_html("Good paper", ["readable body"]))
        if url == bad_paper:
            raise requests.ConnectionError("DNS failure")
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    out = discovery._poll_bis()
    # The good one is recorded with a body; the bad one is recorded with
    # an empty body (snapshot fetch failed, no snapshot to fall back to,
    # _fetch_text returned ""). Both records survive.
    assert len(out) == 2
    bodies = {ds.url: ds.body_excerpt for ds in out}
    assert "readable body" in bodies[ok_paper]
    assert bodies[bad_paper] == ""  # graceful empty, not a crash


# ── de-duplication ────────────────────────────────────────────────────
def test_sync_dedupes_by_body_hash(monkeypatch, isolated_registry):
    """Same body twice → first sweep registers, second sweep is a no-op."""
    index = "https://ofac.treasury.gov/recent-actions"
    item = "https://ofac.treasury.gov/recent-actions/20250523"

    def fake_get(url, *a, **kw):
        if url == index:
            return FakeResp(_index_html("ofac", [item]))
        if url == item:
            return FakeResp(_body_html(
                "May 23 OFAC action",
                ["Identical body content for the dedup test."],
            ))
        return FakeResp(b"", status=503)

    # Pin POLLERS to only OFAC so the test isn't sensitive to other
    # pollers' index URLs being reached.
    monkeypatch.setattr(
        discovery, "POLLERS", {"ofac": discovery._poll_ofac},
    )
    monkeypatch.setattr(requests, "get", fake_get)

    first = discovery.sync_discovered()
    assert len(first.new) == 1
    assert first.unchanged == []
    assert first.revised == []
    registered_id = first.new[0]

    # Second sweep — same hash, must be classified unchanged.
    second = discovery.sync_discovered()
    assert second.new == []
    assert second.revised == []
    assert registered_id in second.unchanged


def test_sync_registers_revision_when_body_changes(monkeypatch, isolated_registry):
    """Same URL, different body → registered under <id>_v2 so old citations
    still point at what was originally cited."""
    index = "https://ofac.treasury.gov/recent-actions"
    item = "https://ofac.treasury.gov/recent-actions/20250523"

    monkeypatch.setattr(
        discovery, "POLLERS", {"ofac": discovery._poll_ofac},
    )

    # First sweep — body A.
    def fake_get_a(url, *a, **kw):
        if url == index:
            return FakeResp(_index_html("ofac", [item]))
        if url == item:
            return FakeResp(_body_html(
                "Action", ["Original wording of the action."],
            ))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get_a)
    first = discovery.sync_discovered()
    assert len(first.new) == 1
    base_id = first.new[0]

    # Second sweep — same URL, different body.
    def fake_get_b(url, *a, **kw):
        if url == index:
            return FakeResp(_index_html("ofac", [item]))
        if url == item:
            return FakeResp(_body_html(
                "Action (amended)",
                ["Updated wording amending the original action."],
            ))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get_b)
    second = discovery.sync_discovered()
    assert second.revised, "expected the changed body to register a revision"
    revised_id = second.revised[0]
    assert revised_id.startswith(base_id + "_v")
    # The original is still in the registry untouched.
    registered = {s.id for s in corpus_sources.all_sources()}
    assert base_id in registered
    assert revised_id in registered


def test_discover_all_logs_and_returns_empty_on_total_failure(monkeypatch, isolated_registry):
    """Every poller's network call raises — sweep returns []."""
    def blow_up(*a, **kw):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(requests, "get", blow_up)
    results = discovery.discover_all()
    assert results == []


def test_sync_persists_ledger_to_disk(monkeypatch, isolated_registry):
    """After a successful registration, the JSON ledger contains the source id."""
    index = "https://ofac.treasury.gov/recent-actions"
    item = "https://ofac.treasury.gov/recent-actions/20250523"

    monkeypatch.setattr(
        discovery, "POLLERS", {"ofac": discovery._poll_ofac},
    )

    def fake_get(url, *a, **kw):
        if url == index:
            return FakeResp(_index_html("ofac", [item]))
        if url == item:
            return FakeResp(_body_html("Action", ["body"]))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    report = discovery.sync_discovered()
    assert report.new

    on_disk = json.loads(discovery.DISCOVERED_PATH.read_text())
    assert report.new[0] in on_disk
    entry = on_disk[report.new[0]]
    assert entry["url"] == item
    assert entry["body_sha256"]


def test_sync_stages_markdown_for_each_new_source(monkeypatch, isolated_registry):
    """The body is staged to corpus/staging/<id>.md so sync_staging picks it up."""
    from sca import config

    index = "https://ofac.treasury.gov/recent-actions"
    item = "https://ofac.treasury.gov/recent-actions/20250523"

    monkeypatch.setattr(
        discovery, "POLLERS", {"ofac": discovery._poll_ofac},
    )

    def fake_get(url, *a, **kw):
        if url == index:
            return FakeResp(_index_html("ofac", [item]))
        if url == item:
            return FakeResp(_body_html(
                "Iran designations",
                ["OFAC designated three new entities on this date."],
            ))
        return FakeResp(b"", status=503)

    monkeypatch.setattr(requests, "get", fake_get)
    report = discovery.sync_discovered()
    sid = report.new[0]
    staged = config.CORPUS_DIR / "staging" / f"{sid}.md"
    assert staged.exists()
    body = staged.read_text(encoding="utf-8")
    assert "designated three new entities" in body
    assert sid.startswith("ofac-")
