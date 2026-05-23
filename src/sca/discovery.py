"""Daily auto-discovery — keep the corpus alive without manual editing.

A nightly sweep of authoritative regulatory index pages (OFAC, BIS, FSB,
EUR-Lex MiCA, NYDFS, IAASB). For each, we list recent publications,
fetch their bodies through `snapshots.fetch_with_snapshot` (so we get
last-known-good fallback + drift detection for free), hash the body, and
register anything new in the corpus registry + stage its text. The
existing `sync_staging()` then picks it up on the next retrieve().

Design rules:

  - **Every fetch goes through `snapshots.fetch_with_snapshot`.** Never
    raw `requests.get` for these. One chokepoint, one archive copy per
    source-id.
  - **Best-effort, never fatal.** If a poller raises, log it and return
    `[]`. A single broken poller never breaks the sweep.
  - **De-duplicated by content hash.** A sha256 of the body text is kept
    per discovered-source-id at `data/discovered_sources.json`. If the
    hash matches a prior discovery, the source is skipped. If the body
    has changed, the source is re-registered with a `_v2`, `_v3` … suffix
    so the original citation never silently drifts under callers.
  - **Tier1 official, included by default.** Every newly registered
    source enters as `tier: tier1_official, status: included` — the
    opt-out model means a human can later exclude one via `sca curate`.

The actual HTTP scraping is intentionally simple stdlib `html.parser` —
no external dependencies, robust to slow site redesigns, and trivial to
mock in tests via the same `FakeResp` / monkeypatch pattern used for
snapshot tests.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

from sca import config, snapshots
from sca.corpus import sources as corpus_sources
from sca.observability import log_event

# ── persistence ───────────────────────────────────────────────────────
# Hash ledger of every source we've ever auto-discovered. Keyed by the
# generated source-id; value carries the sha256 + url + when found. The
# file is the only state the discovery system keeps — everything else
# (snapshots, corpus passages) lives in its own store.
DISCOVERED_PATH = config.DATA_DIR / "discovered_sources.json"


# ── poller registry — module-level so tests can monkey-patch one out ──
# Each poller is a callable: () -> list[DiscoveredSource]. Adding a new
# regulator is one function + one entry in POLLERS.
POLLERS: dict[str, Callable[[], list["DiscoveredSource"]]] = {}


@dataclass
class DiscoveredSource:
    """One publication found by a poller during a sweep.

    `id` is a stable slug derived from the URL so a second sweep against
    the same publication hashes to the same ledger key. `body_excerpt`
    is the plain-text body we'll stage to corpus — capped (~5000 chars)
    because retrieval doesn't need the whole document, just enough
    signal for the BM25 ranker.
    """
    id: str
    title: str
    url: str
    tier: str  # 'tier1_official' for everything this module produces
    published: str  # ISO date if we could extract it from the page
    body_excerpt: str
    discovered_at: str  # ISO 8601 UTC timestamp of this sweep
    poller: str  # which poller found it ('ofac', 'bis', ...)
    body_sha256: str = ""

    def compute_hash(self) -> str:
        """sha256 of the body excerpt — drives de-duplication."""
        return hashlib.sha256(
            self.body_excerpt.encode("utf-8", errors="replace")
        ).hexdigest()


# ── HTML parsing — stdlib only, robust to site redesigns ──────────────
class _LinkExtractor(HTMLParser):
    """Collect every <a href=...> with its visible text on the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []  # (href, text)
        self._in_a: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = ""
        for k, v in attrs:
            if k.lower() == "href" and v:
                href = v
                break
        if href:
            self._in_a.append({"href": href, "text": []})

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._in_a:
            cur = self._in_a.pop()
            text = " ".join(t.strip() for t in cur["text"] if t.strip())
            self.links.append((cur["href"], text))

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._in_a[-1]["text"].append(data)


class _TextExtractor(HTMLParser):
    """Strip <script>/<style>; collapse the rest to readable plain text."""

    _SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self._SKIP:
            self._skip_depth += 1
        elif t == "title":
            self._in_title = True
        elif t in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif t == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title and not self.title:
            self.title = data.strip()
        self.parts.append(data)


def _extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return [(absolute_href, visible_text), ...] from an HTML page."""
    p = _LinkExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must not raise
        pass
    out: list[tuple[str, str]] = []
    for href, text in p.links:
        absolute = urljoin(base_url, href.strip())
        out.append((absolute, text))
    return out


def _extract_text(html: str) -> tuple[str, str]:
    """Return (plain_text, page_title). Whitespace-normalised."""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must not raise
        pass
    text = "".join(p.parts)
    # Collapse runs of whitespace; preserve paragraph breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), p.title.strip()


# ── slug + body fetch helpers ─────────────────────────────────────────
_ID_SAFE_RE = re.compile(r"[^a-z0-9]+")


def _url_slug(url: str, *, prefix: str) -> str:
    """A stable, filesystem-safe id derived from the URL path.

    Discovery runs daily; the same publication must hash to the same id
    on every sweep. The prefix (poller name) keeps the namespace tidy
    and prevents collisions across regulators.
    """
    parsed = urlparse(url)
    raw = (parsed.path + "-" + parsed.query).strip("/-")
    slug = _ID_SAFE_RE.sub("-", raw.lower()).strip("-")
    slug = slug[:80] or "index"
    return f"{prefix}-{slug}"


def _fetch_text(snapshot_id: str, url: str) -> str:
    """Fetch via the snapshot layer and return cleaned plain text.

    Always returns a string. On a hard failure with no snapshot, returns
    `""` — the caller still records a DiscoveredSource so the next sweep
    can retry, but with no body it's effectively a stub.
    """
    try:
        out = snapshots.fetch_with_snapshot(snapshot_id, url)
    except Exception as exc:  # noqa: BLE001 - we never want to break a sweep
        log_event(
            "discovery.body_fetch_failed", level="warn",
            id=snapshot_id, url=url,
            error_class=type(exc).__name__, error_message=str(exc),
        )
        return ""
    try:
        html = out.body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - bytes that aren't text
        return ""
    text, _title = _extract_text(html)
    return text


# ── per-poller link selectors ─────────────────────────────────────────
# Each poller exposes (a) its index URL and (b) a predicate that says
# whether a link on that index looks like a publication we want to
# record. Selectors are intentionally loose — when a site redesigns,
# the worst case is we pull in a few extra links the curator can
# exclude via `sca curate`; the better case is the existing predicate
# still matches because regulators rarely change their URL slugs.
_POLLER_DEFS: list[dict] = [
    {
        "name": "ofac",
        "index_url": "https://ofac.treasury.gov/recent-actions",
        # Recent-actions list entries usually look like /recent-actions/<date>.
        "link_match": lambda href, text: (
            "/recent-actions/" in href.lower()
            and href.rstrip("/").rsplit("/", 1)[-1] != "recent-actions"
        ),
    },
    {
        "name": "bis",
        "index_url": "https://www.bis.org/list/cpmi/index.htm",
        # CPMI papers live under /cpmi/publ/ — that's the durable slug.
        "link_match": lambda href, text: "/cpmi/publ/" in href.lower(),
    },
    {
        "name": "fsb",
        "index_url": (
            "https://www.fsb.org/work-of-the-fsb/"
            "financial-innovation-and-structural-change/"
            "crypto-assets-and-global-stablecoins/"
        ),
        # FSB publications use yyyy/mm/ in the path; recent posts list them.
        "link_match": lambda href, text: (
            "fsb.org/" in href.lower()
            and any(
                seg in href.lower()
                for seg in ("/publications/", "/press/", "/uploads/")
            )
        ),
    },
    {
        "name": "eurlex",
        "index_url": (
            "https://eur-lex.europa.eu/search.html"
            "?DTS_SUBDOM=LEGISLATION&DTS_DOM=MICA"
        ),
        # Legal acts have /legal-content/.../?uri=CELEX:... pattern.
        "link_match": lambda href, text: (
            "/legal-content/" in href.lower()
            and "uri=" in href.lower()
        ),
    },
    {
        "name": "nydfs",
        "index_url": "https://www.dfs.ny.gov/industry_guidance/industry_letters",
        # Industry letters are at /industry_guidance/industry_letters/...
        "link_match": lambda href, text: (
            "/industry_guidance/industry_letters/" in href.lower()
            and href.rstrip("/").rsplit("/", 1)[-1] != "industry_letters"
        ),
    },
    {
        "name": "iaasb",
        "index_url": "https://www.iaasb.org/news-events",
        # News items live under /news-events/<yyyy-mm>/<slug>.
        "link_match": lambda href, text: (
            "/news-events/" in href.lower()
            and href.rstrip("/").rsplit("/", 1)[-1] != "news-events"
        ),
    },
]


# How many links per index we deep-fetch. Above this we trust the index
# order and stop — pages list 50+ items going back years; we only want
# the recent batch each sweep.
_MAX_LINKS_PER_POLLER = 10
# Cap the body excerpt so staged files stay small and BM25 stays fast.
_BODY_EXCERPT_CHARS = 5000


_DATE_PATTERNS = (
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    re.compile(r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})", re.I),
    re.compile(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})", re.I),
)


def _extract_date(text: str) -> str:
    """Best-effort ISO date from a body — empty string if none found."""
    head = text[:2000]
    for pat in _DATE_PATTERNS:
        m = pat.search(head)
        if m:
            raw = m.group(1)
            # Already ISO? Done.
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                return raw
            # Try a few common parses; fall back to the raw string.
            for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%B %d %Y",
                        "%b %d, %Y", "%b %d %Y"):
                try:
                    return datetime.strptime(raw, fmt).date().isoformat()
                except ValueError:
                    continue
            return raw
    return ""


def _poll_generic(name: str, index_url: str,
                  link_match: Callable[[str, str], bool]) -> list[DiscoveredSource]:
    """Shared poller implementation. Every per-source try is isolated."""
    index_id = f"discovery-{name}-index"
    try:
        out = snapshots.fetch_with_snapshot(index_id, index_url)
    except Exception as exc:  # noqa: BLE001 - poller never breaks the sweep
        log_event(
            f"discovery.{name}", level="warn",
            stage="index_fetch", url=index_url,
            error_class=type(exc).__name__, error_message=str(exc),
        )
        return []

    try:
        html = out.body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        log_event(f"discovery.{name}", level="warn",
                  stage="index_decode", url=index_url)
        return []

    links = _extract_links(html, index_url)
    # De-dup by URL so two anchors to the same publication don't double-fetch.
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for href, text in links:
        if href in seen:
            continue
        if not link_match(href, text):
            continue
        seen.add(href)
        candidates.append((href, text))
        if len(candidates) >= _MAX_LINKS_PER_POLLER:
            break

    discovered: list[DiscoveredSource] = []
    for href, link_text in candidates:
        try:
            source_id = _url_slug(href, prefix=name)
            body = _fetch_text(f"discovery-{source_id}", href)
            # Title = the link text if we have it, else the URL's last
            # path segment. Cheap, robust, and good enough for citations.
            title = (
                _html.unescape(link_text).strip()
                or href.rstrip("/").rsplit("/", 1)[-1]
                or href
            )
            excerpt = body[:_BODY_EXCERPT_CHARS]
            published = _extract_date(body) if body else ""
            ds = DiscoveredSource(
                id=source_id,
                title=title[:200],
                url=href,
                tier="tier1_official",
                published=published,
                body_excerpt=excerpt,
                discovered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                poller=name,
            )
            ds.body_sha256 = ds.compute_hash()
            discovered.append(ds)
        except Exception as exc:  # noqa: BLE001 - one bad link doesn't break the poller
            log_event(
                f"discovery.{name}", level="warn",
                stage="per_link", url=href,
                error_class=type(exc).__name__, error_message=str(exc),
            )
            continue

    log_event(
        f"discovery.{name}", level="info",
        stage="done", url=index_url,
        candidates=len(candidates), discovered=len(discovered),
    )
    return discovered


def _make_poller(spec: dict) -> Callable[[], list[DiscoveredSource]]:
    """Bind a poller spec to its name so it shows up cleanly in tracebacks."""
    name = spec["name"]
    index_url = spec["index_url"]
    link_match = spec["link_match"]

    def _run() -> list[DiscoveredSource]:
        return _poll_generic(name, index_url, link_match)

    _run.__name__ = f"_poll_{name}"
    return _run


# Materialise the registry once at import time.
for _spec in _POLLER_DEFS:
    POLLERS[_spec["name"]] = _make_poller(_spec)

# Named bindings so tests can import them directly.
_poll_ofac = POLLERS["ofac"]
_poll_bis = POLLERS["bis"]
_poll_fsb = POLLERS["fsb"]
_poll_eurlex = POLLERS["eurlex"]
_poll_nydfs = POLLERS["nydfs"]
_poll_iaasb = POLLERS["iaasb"]


# ── ledger I/O ────────────────────────────────────────────────────────
def _load_ledger() -> dict[str, dict]:
    """The discovered-sources ledger keyed by source id."""
    if not DISCOVERED_PATH.exists():
        return {}
    try:
        data = json.loads(DISCOVERED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_ledger(ledger: dict[str, dict]) -> None:
    from sca.persist import atomic_write_json
    atomic_write_json(DISCOVERED_PATH, ledger)


def _next_revision_id(base_id: str, ledger: dict[str, dict]) -> str:
    """When a previously discovered URL's body has CHANGED, register the
    new version under `<base>_v2`, `_v3` … — that way old citations to
    `<base>` stay pinned to the body they originally cited, and the
    refreshed version stands on its own.
    """
    n = 2
    while f"{base_id}_v{n}" in ledger:
        n += 1
    return f"{base_id}_v{n}"


# ── public entry points ───────────────────────────────────────────────
def discover_all() -> list[DiscoveredSource]:
    """Run every registered poller. One poller's exception never breaks
    the sweep; it logs and yields no sources for that regulator.

    Three surfaces are merged here:

      - HTML index scrapers in this module (`POLLERS`) — the original
        six tier1_official regulators (OFAC, BIS, FSB, EUR-Lex, NYDFS,
        IAASB).
      - RSS/Atom feed pollers in `sca.discovery_rss` — central-bank
        firehoses and topic-filtered feeds.
      - Issuer / industry blog scrapers in `sca.discovery_blogs` —
        tier2_industry commercial commentary.

    Each surface is imported lazily and isolated: an import error or a
    crashing sub-sweep logs and yields zero records rather than killing
    the whole nightly run. The combined result feeds `sync_discovered()`
    so the ledger / staging / registration path is one chokepoint, not
    three.
    """
    results: list[DiscoveredSource] = []
    for name, poller in POLLERS.items():
        try:
            results.extend(poller())
        except Exception as exc:  # noqa: BLE001 - belt-and-suspenders
            log_event(
                f"discovery.{name}", level="error", stage="poller_crash",
                error_class=type(exc).__name__, error_message=str(exc),
            )

    # Fold in RSS + blog surfaces — each sub-sweep is best-effort and
    # never breaks the run. Imports are deferred so the core module
    # stays usable even if a sub-module has a syntax error in dev.
    for module_name in ("sca.discovery_rss", "sca.discovery_blogs"):
        try:
            import importlib

            submod = importlib.import_module(module_name)
            results.extend(submod.discover_all())
        except Exception as exc:  # noqa: BLE001 - never break the sweep
            log_event(
                "discovery.submodule", level="error",
                module=module_name,
                error_class=type(exc).__name__, error_message=str(exc),
            )

    log_event(
        "discovery.sweep", level="info",
        pollers=len(POLLERS), found=len(results),
    )
    return results


@dataclass
class SyncReport:
    """Summary of one `sync_discovered()` run.

    Fields are list[str] of source ids so the CLI can render them and a
    future Sentry alert can mention exact rows. `errors` is keyed by
    poller name when a poller crashes outright; per-item errors stay in
    the structured log (already emitted by the poller).
    """
    new: list[str] = field(default_factory=list)
    revised: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.new) + len(self.revised) + len(self.unchanged)


def sync_discovered() -> SyncReport:
    """Run the sweep; persist new sources to the corpus.

    For each discovered source:
      - If its sha256 matches the ledger entry → skip (unchanged).
      - If the id is new → register it as `tier1_official, included`,
        stage its body text to `corpus/staging/<id>.md`, record the
        hash.
      - If the id is known but the hash CHANGED → register a
        `<id>_v<n>` revision so old citations don't silently drift.

    Returns a SyncReport for the caller to render.
    """
    report = SyncReport()
    ledger = _load_ledger()
    staging_dir = config.CORPUS_DIR / "staging"

    try:
        found = discover_all()
    except Exception as exc:  # noqa: BLE001 - defensive top-level
        report.errors["__sweep__"] = f"{type(exc).__name__}: {exc}"
        return report

    # Snapshot of currently registered source ids so we don't try to
    # propose one twice in the same sweep (or one that's already in the
    # registry from a previous run).
    try:
        existing_ids = {s.id for s in corpus_sources.all_sources()}
    except Exception:  # noqa: BLE001 - registry corruption shouldn't kill discovery
        existing_ids = set()

    for ds in found:
        prior = ledger.get(ds.id)
        if prior and prior.get("body_sha256") == ds.body_sha256:
            report.unchanged.append(ds.id)
            # Refresh discovered_at so we know we still see it.
            prior["last_seen_at"] = ds.discovered_at
            continue

        if prior:
            # Body changed → register a revision under a new id.
            target_id = _next_revision_id(ds.id, ledger)
        else:
            target_id = ds.id

        if target_id in existing_ids:
            # Already in the registry from an earlier sweep we forgot
            # about — record in the ledger but don't double-register.
            ledger[target_id] = {**asdict(ds), "id": target_id}
            report.unchanged.append(target_id)
            continue

        # Stage body text + propose the source. Both can fail; isolate
        # each so one bad row doesn't break the rest of the sweep.
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged_md = _to_markdown(ds, target_id)
            (staging_dir / f"{target_id}.md").write_text(
                staged_md, encoding="utf-8",
            )
            corpus_sources.propose_source(
                target_id,
                title=ds.title or target_id,
                # Honour the DiscoveredSource's tier — tier1_official for
                # government/regulator pollers, tier2_industry for issuer
                # and analytics blogs. Falling back to tier1 if a poller
                # somehow yielded an empty tier keeps the propose call
                # within VALID_TIERS.
                tier=ds.tier or "tier1_official",
                url=ds.url,
                notes=(
                    f"auto-discovered by {ds.poller} on "
                    f"{ds.discovered_at}"
                ),
            )
            ledger[target_id] = {**asdict(ds), "id": target_id}
            existing_ids.add(target_id)
            if prior:
                report.revised.append(target_id)
            else:
                report.new.append(target_id)
        except Exception as exc:  # noqa: BLE001 - log and continue
            log_event(
                "discovery.register_failed", level="warn",
                id=target_id, url=ds.url,
                error_class=type(exc).__name__, error_message=str(exc),
            )
            report.errors[target_id] = f"{type(exc).__name__}: {exc}"

    _save_ledger(ledger)
    log_event(
        "discovery.sync", level="info",
        new=len(report.new), revised=len(report.revised),
        unchanged=len(report.unchanged), errors=len(report.errors),
    )
    return report


def _to_markdown(ds: DiscoveredSource, source_id: str) -> str:
    """Render a DiscoveredSource as a staging markdown file.

    A single H1 (the title) makes the whole body retrievable as one
    section — good enough for the BM25 ranker. The provenance line on
    top gives the human reviewer the URL and publish date at a glance.
    """
    header = (
        f"# {ds.title or source_id}\n\n"
        f"_source: {ds.url}_  \n"
        f"_published: {ds.published or 'unknown'}_  \n"
        f"_discovered: {ds.discovered_at} via {ds.poller}_\n\n"
    )
    return header + (ds.body_excerpt or "").strip() + "\n"
