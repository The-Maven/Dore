"""Corpus ingestion: turn an approved source into section-anchored chunks.

Structure is preserved — each chunk carries its section/heading so a
citation can point to a section, not just a document. Only sources with
status: approved may be ingested.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from sca import config
from sca.corpus.sources import get_source

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


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
    """Ingest an APPROVED source's text into section-anchored chunks.

    Refuses non-approved sources. With `store` omitted, chunks are persisted
    through the durable `Store` (`save_passages`) — so production writes to
    Supabase via config alone. An explicit `store` directory keeps the
    legacy on-disk path (used by hermetic tests).
    """
    src = get_source(source_id)
    if not src.approved:
        raise PermissionError(
            f"source {source_id!r} is status:{src.status} — only approved "
            "sources may be ingested. A human approves in corpus/sources.yaml."
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
