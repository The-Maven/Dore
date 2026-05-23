"""Curated corpus layer — the reasoning frame (opt-out model).

Every registered source is included and citable by default. A human may
exclude a source (opt-out) and may mark one explicitly verified. The agent
proposes new sources and cites every included source it has text for.
"""
from __future__ import annotations

from .ingest import Chunk, chunk_markdown, ingest_source, sync_staging
from .retrieve import retrieve
from .sources import (
    Source,
    all_sources,
    get_source,
    included_sources,
    propose_source,
)

__all__ = [
    "Source",
    "all_sources",
    "included_sources",
    "get_source",
    "propose_source",
    "Chunk",
    "chunk_markdown",
    "ingest_source",
    "sync_staging",
    "retrieve",
]
