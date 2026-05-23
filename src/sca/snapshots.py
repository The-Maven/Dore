"""Snapshot store — every URL we successfully fetch, kept as a last-good copy.

Why this exists: external sources move. An issuer redesigns their site, a
regulator re-orgs their CMS, a CDN URL rotates — and our links rot.
Without a fallback, a broken link is a dead-end for the user. With a
snapshot, the user can still open the archived body we last fetched, and
we know exactly when content drifted so we can re-anchor the source.

Each snapshot is keyed by a stable id (the source-id or a URL-derived
slug) and carries:
  - sha256 of the body (drift detection)
  - fetched_at ISO timestamp
  - content_type (so the API can serve it back with the right MIME)
  - status_code (200 the last time we touched it)
  - body_path (where the bytes live)

The snapshot is plain bytes on disk — no DB needed for v1. The web app
serves them through `/api/snapshot/<id>`. Future work: push to durable
object storage (S3) once we have a deploy target.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from sca.config import DATA_DIR
from sca.observability import log_event

SNAPSHOTS_DIR = DATA_DIR / "source_snapshots"


@dataclass
class SnapshotMeta:
    id: str
    url: str
    sha256: str
    fetched_at: str  # ISO 8601 UTC
    content_type: str
    status_code: int
    bytes: int

    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    def is_pdf(self) -> bool:
        return "pdf" in self.content_type.lower()


def _slug(value: str) -> str:
    """Filesystem-safe slug. Used when we don't have an explicit id."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return s[:120] or "untitled"


def _ext_for(content_type: str) -> str:
    """Pick a file extension for the cached body so OS tools recognise it."""
    ct = content_type.lower()
    if "pdf" in ct:
        return ".pdf"
    if "html" in ct or "xml" in ct:
        return ".html" if "html" in ct else ".xml"
    if "json" in ct:
        return ".json"
    if "text" in ct:
        return ".txt"
    return ".bin"


def _dir_for(snapshot_id: str) -> Path:
    return SNAPSHOTS_DIR / _slug(snapshot_id)


def save(
    snapshot_id: str,
    url: str,
    body: bytes,
    *,
    content_type: str = "application/octet-stream",
    status_code: int = 200,
) -> SnapshotMeta:
    """Persist `body` as the latest snapshot for `snapshot_id`.

    Always overwrites the previous body — we keep ONE last-known-good per
    id, not a history. (Versioning is a v2 concern; for now durability
    matters more than archive depth.) Returns the meta record.
    """
    folder = _dir_for(snapshot_id)
    folder.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(body).hexdigest()
    body_path = folder / f"body{_ext_for(content_type)}"
    # Clean up any stale body file with a different extension before writing.
    for f in folder.glob("body.*"):
        if f != body_path:
            try:
                f.unlink()
            except OSError:
                pass
    from sca.persist import atomic_write_bytes, atomic_write_json
    atomic_write_bytes(body_path, body)
    meta = SnapshotMeta(
        id=_slug(snapshot_id),
        url=url,
        sha256=sha,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        content_type=content_type,
        status_code=status_code,
        bytes=len(body),
    )
    atomic_write_json(folder / "meta.json", asdict(meta), sort_keys=False)
    log_event(
        "snapshot.saved", level="info",
        id=meta.id, url=url, sha256=sha, bytes=len(body),
        content_type=content_type,
    )
    return meta


def load_meta(snapshot_id: str) -> SnapshotMeta | None:
    """Return the meta record for a snapshot, or None if not snapshotted."""
    path = _dir_for(snapshot_id) / "meta.json"
    if not path.exists():
        return None
    try:
        return SnapshotMeta(**json.loads(path.read_text()))
    except Exception:  # noqa: BLE001 - corrupt meta is non-fatal
        return None


def load_body(snapshot_id: str) -> tuple[bytes, SnapshotMeta] | None:
    """Return (body_bytes, meta) for the snapshot, or None if missing."""
    meta = load_meta(snapshot_id)
    if meta is None:
        return None
    body_path = _dir_for(snapshot_id) / f"body{_ext_for(meta.content_type)}"
    if not body_path.exists():
        return None
    return body_path.read_bytes(), meta


def list_all() -> list[SnapshotMeta]:
    """Every snapshot we have on disk — used by the source-health UI."""
    out: list[SnapshotMeta] = []
    if not SNAPSHOTS_DIR.exists():
        return out
    for folder in sorted(SNAPSHOTS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        meta = load_meta(folder.name)
        if meta is not None:
            out.append(meta)
    return out


def staleness_days(meta: SnapshotMeta) -> int | None:
    """Days since this snapshot was last refreshed."""
    try:
        fetched = datetime.fromisoformat(meta.fetched_at)
    except ValueError:
        return None
    delta = datetime.now(timezone.utc) - fetched
    return delta.days


@dataclass
class FetchOutcome:
    """The result of fetch_with_snapshot — caller cares about: did we get
    bytes (always, if there's a snapshot), and where did they come from."""
    body: bytes
    meta: SnapshotMeta
    source: str  # "live" — fresh fetch | "snapshot" — served from cache
    error: str = ""  # set when we fell back; empty on a clean live fetch


def fetch_with_snapshot(
    snapshot_id: str,
    url: str,
    *,
    timeout: int = 60,
    user_agent: str = "Mozilla/5.0 (Doré/canary)",
) -> FetchOutcome:
    """Fetch `url`. On success, snapshot it. On failure, serve the last
    snapshot if we have one — and tag the outcome so the UI can show
    "archived copy" and flag the broken live link.

    This is the single chokepoint we want all source fetches to go through
    so we have one place to manage drift, snapshots, and the canary.
    """
    import requests

    headers = {"User-Agent": user_agent}
    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
        resp.raise_for_status()
    except requests.RequestException as exc:
        existing = load_body(snapshot_id)
        log_event(
            "snapshot.live_fetch_failed", level="warn",
            id=snapshot_id, url=url,
            error_class=type(exc).__name__, error_message=str(exc),
            had_snapshot=existing is not None,
        )
        if existing is None:
            raise
        body, meta = existing
        return FetchOutcome(
            body=body, meta=meta, source="snapshot", error=str(exc),
        )

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    meta = save(
        snapshot_id, url, resp.content,
        content_type=content_type, status_code=resp.status_code,
    )
    return FetchOutcome(body=resp.content, meta=meta, source="live")
