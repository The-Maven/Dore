"""Human decision ledger — the voting mechanism.

Curation decisions — source approvals, address verifications — are recorded
here as an append-only ledger, kept separate from the config files the
agent populates. The config and corpus loaders apply these votes as
overrides.

Why a ledger and not in-place edits: the agent's proposals and the human's
decisions stay cleanly separated, with a dated audit trail of every vote.
Later votes win, so a decision can always be revised.

This module is a thin adapter over the `Store` abstraction: it delegates to
`get_store()` so production can flip to Supabase via config alone. The public
function signatures here are unchanged — callers (CLI `curate`, the config
and corpus loaders) are untouched. The `Store` itself records richer rows
(it needs a `contract` for an address vote and keys overrides by string);
the translation back to this module's historical shapes happens here.
"""
from __future__ import annotations

from pathlib import Path

from sca.store import FileStore, get_store
from sca.store.base import Store

# Kept for backwards compatibility: tests (conftest) isolate the ledger by
# monkeypatching this path. When the active store is file-backed the ledger
# is bound to VOTES_PATH so that isolation keeps working without a database.
VOTES_PATH = Path(__file__).resolve().parents[2] / "votes.yaml"


def _store() -> Store:
    """The store this ledger writes to.

    A `SupabaseStore` (production) is used as-is. A `FileStore` is rebound to
    the current `VOTES_PATH` so per-test isolation via monkeypatch holds.
    """
    store = get_store()
    if isinstance(store, FileStore):
        return FileStore(votes_path=VOTES_PATH)
    return store


def record_source_decision(source_id: str, decision: str) -> None:
    """Record a vote on a corpus source. decision: 'approved' | 'rejected'."""
    _store().record_source_decision(source_id, decision)


def record_address_decision(symbol: str, chain: str, decision: str) -> None:
    """Record a vote on a contract address. decision: 'verified' | 'rejected'.

    The `Store` records the contract address alongside the vote; it is
    resolved here from the stablecoin registry's deployment for `chain`.
    """
    if decision not in ("verified", "rejected"):
        raise ValueError("decision must be 'verified' or 'rejected'")
    contract = _resolve_contract(symbol, chain)
    _store().record_address_decision(symbol, chain, contract, decision)


def _resolve_contract(symbol: str, chain: str) -> str:
    """Find the on-chain contract for (symbol, chain) in the registry."""
    from sca import config

    try:
        coin = config.get_stablecoin(symbol)
    except ValueError:
        return ""
    for dep in coin.deployments:
        if dep.chain == chain:
            return dep.contract
    return ""


def source_status_overrides() -> dict[str, str]:
    """Latest decision per source id (later votes win)."""
    return _store().source_status_overrides()


def address_verified_overrides() -> dict[tuple[str, str], bool]:
    """Latest verification decision per (symbol, chain).

    The `Store` keys overrides by a 'symbol:chain' string; this translates
    back to the (symbol, chain) tuple keys callers expect.
    """
    out: dict[tuple[str, str], bool] = {}
    for key, verified in _store().address_verified_overrides().items():
        symbol, _, chain = key.partition(":")
        out[(symbol, chain)] = verified
    return out


def history() -> dict:
    """The full ledger — for display / audit."""
    return _store().curation_history()
