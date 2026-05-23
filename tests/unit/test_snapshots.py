"""Snapshot store + fetch-with-fallback — last-known-good for every URL."""
from __future__ import annotations

import pytest
import requests

from sca import snapshots


class FakeResp:
    def __init__(self, content: bytes, *, status: int = 200,
                 content_type: str = "text/html"):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def test_save_and_load_round_trip():
    body = b"<html>hi</html>"
    meta = snapshots.save("circle-transparency", "https://circle.com/x",
                          body, content_type="text/html")
    assert meta.bytes == len(body)
    assert meta.sha256

    loaded = snapshots.load_body("circle-transparency")
    assert loaded is not None
    body, meta2 = loaded
    assert body == b"<html>hi</html>"
    assert meta2.sha256 == meta.sha256


def test_load_missing_returns_none():
    assert snapshots.load_meta("never-fetched") is None
    assert snapshots.load_body("never-fetched") is None


def test_save_overwrites_previous_body():
    """Snapshot store keeps ONE last-good — not a history. Second save wins."""
    snapshots.save("x", "https://a.com", b"old", content_type="text/plain")
    snapshots.save("x", "https://a.com", b"new", content_type="text/plain")
    body, _ = snapshots.load_body("x")
    assert body == b"new"


def test_save_switches_extension_when_content_type_changes():
    """Body extension follows the content-type so OS tooling recognises it.
    Previous-extension files must not linger and conflict."""
    snapshots.save("rotating", "https://x.com", b"<html/>",
                   content_type="text/html")
    snapshots.save("rotating", "https://x.com", b"%PDF-1.4",
                   content_type="application/pdf")
    body, meta = snapshots.load_body("rotating")
    assert body == b"%PDF-1.4"
    assert meta.is_pdf()
    # No stray .html artefact
    folder = snapshots._dir_for("rotating")
    bodies = sorted(p.name for p in folder.glob("body.*"))
    assert bodies == ["body.pdf"]


def test_list_all_returns_snapshots():
    snapshots.save("a", "https://a.com", b"a", content_type="text/plain")
    snapshots.save("b", "https://b.com", b"b", content_type="text/plain")
    metas = snapshots.list_all()
    ids = sorted(m.id for m in metas)
    assert ids == ["a", "b"]


def test_fetch_with_snapshot_live_success(monkeypatch):
    """On 200, body returns and snapshot is updated."""
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **kw: FakeResp(b"<html>live</html>"),
    )
    out = snapshots.fetch_with_snapshot("ofac", "https://example.gov/sdn")
    assert out.source == "live"
    assert out.body == b"<html>live</html>"
    assert out.error == ""

    # Snapshot persisted
    loaded = snapshots.load_body("ofac")
    assert loaded is not None and loaded[0] == b"<html>live</html>"


def test_fetch_with_snapshot_falls_back_when_live_breaks(monkeypatch):
    """When live URL 5xx's, we serve the last snapshot and flag it."""
    snapshots.save("ofac", "https://example.gov/sdn",
                   b"<html>old</html>", content_type="text/html")

    def blow_up(*a, **kw):
        raise requests.ConnectionError("DNS failure")

    monkeypatch.setattr(
        requests, "get", blow_up,
    )
    out = snapshots.fetch_with_snapshot("ofac", "https://example.gov/sdn")
    assert out.source == "snapshot"
    assert out.body == b"<html>old</html>"
    assert "DNS failure" in out.error


def test_fetch_with_snapshot_no_snapshot_no_fallback_raises(monkeypatch):
    """No live, no snapshot — caller deserves to know."""
    def blow_up(*a, **kw):
        raise requests.ConnectionError("DNS failure")

    monkeypatch.setattr(
        requests, "get", blow_up,
    )
    with pytest.raises(requests.ConnectionError):
        snapshots.fetch_with_snapshot("never-fetched", "https://gone.example/x")
