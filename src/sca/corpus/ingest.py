"""Corpus ingestion: turn a source's text into section-anchored chunks.

Structure is preserved — each chunk carries its section/heading so a
citation can point to a section, not just a document. Every source is
ingestible by default; an explicitly excluded source is refused.

This module also drives auto-ingestion: dropping `<source_id>.md` into
`corpus/staging/` is enough — `sync_staging()` hashes each staged file,
compares it to the last-ingested hash recorded in
`data/corpus_ingest_state.json`, and re-ingests only what changed. The
function is idempotent: a second call with nothing new is a no-op.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from sca import config
from sca.corpus.sources import get_source

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# Module-level so tests can monkey-patch them onto a tmp_path.
STAGING_DIR = config.CORPUS_DIR / "staging"
INGEST_STATE_PATH = config.DATA_DIR / "corpus_ingest_state.json"


@dataclass
class Chunk:
    source_id: str
    section: str
    heading: str
    text: str
    page: int | None = None


def chunk_markdown(source_id: str, text: str) -> list[Chunk]:
    """Split markdown / plain text into section-anchored chunks by heading."""
    chunks: list[Chunk] = []
    heading = "(intro)"
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(source_id, heading, heading, body))

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            buf.clear()
            heading = match.group(2).strip()
            continue
        buf.append(line)
    flush()
    return chunks


def _store_dir(store: Path | None) -> Path:
    return store or (config.CORPUS_DIR / "data")


def ingest_source(
    source_id: str, text: str, *, store: Path | None = None
) -> list[Chunk]:
    """Ingest a source's text into section-anchored chunks.

    Refuses a source a human has explicitly excluded. With `store` omitted,
    chunks are persisted through the durable `Store` (`save_passages`) — so
    production writes to Supabase via config alone. An explicit `store`
    directory keeps the legacy on-disk path (used by hermetic tests).
    """
    src = get_source(source_id)
    if src.excluded:
        raise PermissionError(
            f"source {source_id!r} is status:excluded — a human has opted "
            "it out of the corpus. Re-include it before ingesting."
        )
    chunks = chunk_markdown(source_id, text)
    if store is None:
        from sca.store import get_store

        get_store().save_passages(
            source_id, [asdict(c) for c in chunks]
        )
        return chunks
    out_dir = _store_dir(store)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{source_id}.json").write_text(
        json.dumps([asdict(c) for c in chunks], indent=2)
    )
    return chunks


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_ingest_state() -> dict[str, str]:
    if not INGEST_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(INGEST_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_ingest_state(state: dict[str, str]) -> None:
    from sca.persist import atomic_write_json
    atomic_write_json(INGEST_STATE_PATH, state)


def sync_staging() -> list[str]:
    """Ingest any new-or-changed `corpus/staging/<source_id>.md` files.

    Each staged file is content-hashed; a file is (re)ingested only when its
    hash differs from the one recorded in `data/corpus_ingest_state.json`.
    Files whose stem is not a registered source id are silently skipped (so
    the staging `README.md` is not an error). Excluded sources are skipped
    too — a human has opted them out of the corpus.

    Returns the list of source ids that were actually ingested this call.
    Safe to call repeatedly: with nothing changed, it is a no-op.
    """
    if not STAGING_DIR.exists():
        return []
    state = _load_ingest_state()
    ingested: list[str] = []
    for path in sorted(STAGING_DIR.glob("*.md")):
        source_id = path.stem
        try:
            src = get_source(source_id)
        except KeyError:
            # Not a registered source — e.g. corpus/staging/README.md.
            continue
        if src.excluded:
            # Human has opted this source out of the corpus.
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        digest = _content_hash(text)
        if state.get(source_id) == digest:
            continue  # already ingested at this exact content
        ingest_source(source_id, text)
        state[source_id] = digest
        ingested.append(source_id)
    if ingested:
        _save_ingest_state(state)
    return ingested
