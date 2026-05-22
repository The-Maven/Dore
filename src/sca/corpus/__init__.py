"""Curated corpus layer — the human-gated reasoning frame.

Agent proposes sources; a human approves them in corpus/sources.yaml;
only approved sources are ingested and retrievable.
"""
from __future__ import annotations

from .ingest import Chunk, chunk_markdown, ingest_source
from .retrieve import retrieve
from .sources import (
    Source,
    all_sources,
    approved_sources,
    get_source,
    propose_source,
)

__all__ = [
    "Source",
    "all_sources",
    "approved_sources",
    "get_source",
    "propose_source",
    "Chunk",
    "chunk_markdown",
    "ingest_source",
    "retrieve",
]
