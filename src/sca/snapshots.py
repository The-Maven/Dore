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
    fetched_at: str  # ISO 8601 UTC — when we last DOWNLOADED the body
    content_type: str
    status_code: int
    bytes: int
    # Conditional-request fingerprints captured on the most recent
    # successful fetch. Empty strings when the source server didn't
    # provide them. Both load-bearing for the validator: the next
    # fetch sends If-Modified-Since + If-None-Match so the source can
    # answer with a 304 instead of re-shipping the body.
    last_modified: str = ""
    etag: str = ""
    # ISO 8601 UTC — when we last CHECKED with the source whether the
    # snapshot is still current (either a 304, or a 200 with matching
    # sha256, or a full re-fetch that bumped fetched_at). Distinct
    # from fetched_at: a validated_at well past fetched_at means the
    # source has confirmed nothing's changed since.
    validated_at: str = ""

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
    last_modified: str = "",
    etag: str = "",
) -> SnapshotMeta:
    """Persist `body` as the latest snapshot for `snapshot_id`.

    Always overwrites the previous body — we keep ONE last-known-good per
    id, not a history. (Versioning is a v2 concern; for now durability
    matters more than archive depth.) Returns the meta record.

    `last_modified` and `etag` come from the response headers when
    available; persisted so the next fetch can ask conditionally.
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
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = SnapshotMeta(
        id=_slug(snapshot_id),
        url=url,
        sha256=sha,
        fetched_at=now_iso,
        content_type=content_type,
        status_code=status_code,
        bytes=len(body),
        last_modified=last_modified,
        etag=etag,
        validated_at=now_iso,  # a fresh fetch is validated by definition
    )
    atomic_write_json(folder / "meta.json", asdict(meta), sort_keys=False)
    log_event(
        "snapshot.saved", level="info",
        id=meta.id, url=url, sha256=sha, bytes=len(body),
        content_type=content_type,
    )
    return meta


def mark_validated(snapshot_id: str) -> None:
    """Update only the validated_at timestamp on an existing meta —
    used when the source has confirmed the snapshot is still current
    (304 response, or 200 with an unchanged sha256). Cheap: rewrites
    the meta JSON, never touches the body file."""
    meta = load_meta(snapshot_id)
    if meta is None:
        return
    meta.validated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from sca.persist import atomic_write_json
    folder = _dir_for(snapshot_id)
    atomic_write_json(folder / "meta.json", asdict(meta), sort_keys=False)
    log_event(
        "snapshot.validated", level="info",
        id=meta.id, url=meta.url, validated_at=meta.validated_at,
    )


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

    Conditional-fetch path: when we have a prior snapshot with a
    Last-Modified or ETag captured, send If-Modified-Since +
    If-None-Match. The source can answer 304 instead of re-shipping
    the body. Saves bandwidth and gives us a clean "snapshot is still
    current at the source" signal that bumps validated_at without
    rewriting the body.

    This is the single chokepoint we want all source fetches to go through
    so we have one place to manage drift, snapshots, and the canary.
    """
    import requests

    headers = {"User-Agent": user_agent}
    prior = load_meta(snapshot_id)
    if prior is not None:
        if prior.last_modified:
            headers["If-Modified-Since"] = prior.last_modified
        if prior.etag:
            headers["If-None-Match"] = prior.etag

    try:
        resp = requests.get(url, timeout=timeout, headers=headers)
        if resp.status_code != 304:
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

    # 304 — source confirms our snapshot is still current. Bump
    # validated_at and serve the existing body without rewriting it.
    if resp.status_code == 304 and prior is not None:
        existing = load_body(snapshot_id)
        if existing is not None:
            mark_validated(snapshot_id)
            body, meta = existing
            # Re-load meta so the returned outcome carries the fresh
            # validated_at.
            meta = load_meta(snapshot_id) or meta
            return FetchOutcome(body=body, meta=meta, source="snapshot")

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    last_mod = resp.headers.get("Last-Modified", "").strip()
    etag = resp.headers.get("ETag", "").strip()

    # 200 with unchanged body — even when the source doesn't honour
    # conditional headers, if the hash matches we can mark validated
    # without rewriting the body file or bumping fetched_at.
    if prior is not None and prior.sha256 == hashlib.sha256(resp.content).hexdigest():
        mark_validated(snapshot_id)
        body, meta = (resp.content, load_meta(snapshot_id) or prior)
        return FetchOutcome(body=body, meta=meta, source="snapshot")

    meta = save(
        snapshot_id, url, resp.content,
        content_type=content_type, status_code=resp.status_code,
        last_modified=last_mod, etag=etag,
    )
    return FetchOutcome(body=resp.content, meta=meta, source="live")
