"""Corpus source registry — the reasoning frame (opt-out model).

Every registered source is INCLUDED and citable by default. The human
override is a single lever: a human may EXCLUDE a source (reversible to
included). A human may also mark a source explicitly VERIFIED — a quality
signal surfaced on citations; it does not gate whether the source is used.

`included_sources()` is the set the retriever cites. The agent may still
PROPOSE new sources (they enter as included, like any other).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sca import config

VALID_TIERS = (
    "primary", "standard", "methodology", "research", "commentary",
    # `tier1_official` is the bucket for auto-discovered government /
    # standards-body publications (OFAC actions, BIS/CPMI papers, FSB
    # updates, EUR-Lex MiCA RTS, NYDFS industry letters, IAASB news).
    # See sca.discovery — each enters as included, opt-out via curate.
    "tier1_official",
    # `tier2_industry` is the bucket for auto-discovered commercial
    # publications: issuer blogs (Circle, Paxos, Tether) and the major
    # blockchain-analytics shops (Chainalysis, TRM, Elliptic). Not
    # authoritative law — useful colour, sometimes the only timely
    # commentary on a fresh event. Same opt-out model as tier1.
    "tier2_industry",
)


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    tier: str
    status: str = "included"
    url: str = ""
    summary: str = ""
    notes: str = ""
    verified: bool = False

    @property
    def included(self) -> bool:
        """True unless a human has explicitly excluded this source."""
        return self.status != "excluded"

    @property
    def excluded(self) -> bool:
        return self.status == "excluded"


def _path():
    return config.CORPUS_DIR / "sources.yaml"


@lru_cache(maxsize=1)
def all_sources() -> tuple[Source, ...]:
    # Raw source registry comes from the durable store; human exclude /
    # verify votes override the registry's `status` and `verified` signal.
    from sca.store import get_store
    from sca.votes import source_status_overrides, source_verified_overrides

    overrides = source_status_overrides()
    verified = source_verified_overrides()
    raw = get_store().list_sources()
    return tuple(
        Source(
            id=s["id"],
            title=s["title"],
            tier=s.get("tier", ""),
            status=overrides.get(s["id"], s.get("status", "included")),
            url=s.get("url", ""),
            summary=s.get("summary", ""),
            notes=s.get("notes", ""),
            verified=verified.get(s["id"], False),
        )
        for s in raw
    )


def included_sources() -> tuple[Source, ...]:
    """The sources the agent may cite — everything not human-excluded."""
    return tuple(s for s in all_sources() if s.included)


def get_source(source_id: str) -> Source:
    for s in all_sources():
        if s.id == source_id:
            return s
    raise KeyError(f"unknown source: {source_id!r}")


def propose_source(
    source_id: str, title: str, tier: str, *, url: str = "", notes: str = ""
) -> Source:
    """Append a new source to the registry — included by default.

    OPERATOR INTERFACE: this is the agent's way to add a candidate source.
    New sources enter as `included` (the opt-out default); a human may later
    `exclude` one via `sca curate` or the web Corpus view.
    """
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {VALID_TIERS}")
    if any(s.id == source_id for s in all_sources()):
        raise ValueError(f"source {source_id!r} already exists")
    block = (
        f"\n  - id: {source_id}\n"
        f"    title: {title!r}\n"
        f"    tier: {tier}\n"
        f"    status: included\n"
        f"    url: {url!r}\n"
    )
    if notes:
        block += f"    notes: {notes!r}\n"
    with _path().open("a", encoding="utf-8") as fh:
        fh.write(block)
    all_sources.cache_clear()
    return get_source(source_id)
