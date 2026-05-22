"""Corpus source registry + the curation gate.

The agent may PROPOSE sources (status: proposed). Only a human may set
status: approved — and that is a manual edit to corpus/sources.yaml.
Nothing in this module can self-approve. `approved_sources()` is the only
set the retriever is allowed to cite.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sca import config

VALID_TIERS = ("primary", "standard", "methodology", "research", "commentary")


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    tier: str
    status: str
    url: str = ""
    summary: str = ""
    notes: str = ""

    @property
    def approved(self) -> bool:
        return self.status == "approved"


def _path():
    return config.CORPUS_DIR / "sources.yaml"


@lru_cache(maxsize=1)
def all_sources() -> tuple[Source, ...]:
    # Raw source registry comes from the durable store; human approval votes
    # override the registry's `status`.
    from sca.store import get_store
    from sca.votes import source_status_overrides

    overrides = source_status_overrides()
    raw = get_store().list_sources()
    return tuple(
        Source(
            id=s["id"],
            title=s["title"],
            tier=s.get("tier", ""),
            status=overrides.get(s["id"], s.get("status", "proposed")),
            url=s.get("url", ""),
            summary=s.get("summary", ""),
            notes=s.get("notes", ""),
        )
        for s in raw
    )


def approved_sources() -> tuple[Source, ...]:
    """The ONLY sources the agent may cite."""
    return tuple(s for s in all_sources() if s.approved)


def get_source(source_id: str) -> Source:
    for s in all_sources():
        if s.id == source_id:
            return s
    raise KeyError(f"unknown source: {source_id!r}")


def propose_source(
    source_id: str, title: str, tier: str, *, url: str = "", notes: str = ""
) -> Source:
    """Append a new source to the registry as status: proposed.

    OPERATOR INTERFACE (annotated for later): this is the agent's only way
    to add a candidate source. It can NEVER write status: approved — a
    human approves by editing corpus/sources.yaml directly. Keep it that way.
    """
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {VALID_TIERS}")
    if any(s.id == source_id for s in all_sources()):
        raise ValueError(f"source {source_id!r} already exists")
    block = (
        f"\n  - id: {source_id}\n"
        f"    title: {title!r}\n"
        f"    tier: {tier}\n"
        f"    status: proposed\n"
        f"    url: {url!r}\n"
    )
    if notes:
        block += f"    notes: {notes!r}\n"
    with _path().open("a", encoding="utf-8") as fh:
        fh.write(block)
    all_sources.cache_clear()
    return get_source(source_id)
