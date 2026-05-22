"""The `Store` interface — durable runtime state, backend-agnostic.

Two backends implement this ABC: `SupabaseStore` (Postgres, production) and
`FileStore` (YAML / JSON / in-memory, offline + tests). Call sites depend
only on this interface so durable state can move to a database without the
test suite ever needing one.

All methods return plain dicts / primitives — never backend objects — so a
caller cannot tell which backend it holds. Dict shapes mirror the columns in
supabase/migrations/0001_initial_schema.sql, which is authoritative.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

VALID_SURFACES = ("attestation", "sanctions", "redemption")
VALID_STATUSES = ("pending", "running", "done", "error")


class Store(ABC):
    """Abstract persistence layer. See module docstring."""

    # ── analyses ──────────────────────────────────────────────────────
    @abstractmethod
    def create_analysis(
        self, surface: str, symbol: str, user_id: str | None = None
    ) -> str:
        """Create a pending analysis row. Returns its id."""

    @abstractmethod
    def update_analysis(
        self,
        id: str,
        *,
        status: str | None = None,
        result: dict | None = None,
        error: str | None = None,
        input_fingerprint: str | None = None,
    ) -> None:
        """Patch an analysis row. Only non-None fields are written."""

    @abstractmethod
    def get_analysis(self, id: str) -> dict | None:
        """Return the analysis row, or None if no such id."""

    @abstractmethod
    def find_cached_analysis(
        self, surface: str, symbol: str, input_fingerprint: str
    ) -> dict | None:
        """Return the most recent done analysis matching the cache key."""

    @abstractmethod
    def list_analyses(
        self,
        surface: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Recent analyses, newest first, optionally filtered."""

    # ── sources ───────────────────────────────────────────────────────
    @abstractmethod
    def list_sources(self) -> list[dict]:
        """The corpus source registry."""

    # ── corpus ────────────────────────────────────────────────────────
    @abstractmethod
    def save_passages(self, source_id: str, passages: list[dict]) -> None:
        """Replace all ingested passages for a source."""

    @abstractmethod
    def get_passages(self, source_id: str | None = None) -> list[dict]:
        """Ingested passages, optionally scoped to one source, ordinal order."""

    # ── curation ──────────────────────────────────────────────────────
    @abstractmethod
    def record_source_decision(
        self, source_id: str, decision: str, user_id: str | None = None
    ) -> None:
        """Append a source-approval vote. decision: 'approved' | 'rejected'."""

    @abstractmethod
    def record_address_decision(
        self,
        symbol: str,
        chain: str,
        contract: str,
        decision: str,
        user_id: str | None = None,
    ) -> None:
        """Append a contract verification vote. decision: 'verified'|'rejected'."""

    @abstractmethod
    def source_status_overrides(self) -> dict[str, str]:
        """Latest decision per source id (later votes win)."""

    @abstractmethod
    def address_verified_overrides(self) -> dict[str, bool]:
        """Latest verification per (symbol, chain), keyed 'symbol:chain'."""

    @abstractmethod
    def curation_history(self) -> dict:
        """The full append-only curation ledger — for display / audit.

        Returns a dict with `source_decisions` and `address_decisions` lists,
        oldest-first, mirroring the votes.yaml shape.
        """

    # ── monitor ───────────────────────────────────────────────────────
    @abstractmethod
    def save_snapshot(
        self,
        *,
        symbol: str,
        total_supply: float,
        native_supply: float | None,
        bridged_supply: float | None,
        per_chain: list | dict | None,
        warnings: list | None,
    ) -> None:
        """Persist one on-chain supply reading."""

    @abstractmethod
    def list_snapshots(self, symbol: str, limit: int = 50) -> list[dict]:
        """Recent supply snapshots for a symbol, newest first."""
