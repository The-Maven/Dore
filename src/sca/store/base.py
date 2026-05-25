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
from typing import Optional

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
        """Append a source curation vote (opt-out model).

        decision: 'excluded' (drop from the citable set), 'included' (reverse
        an exclusion), or 'verified' (mark human-reviewed; stays included).
        """

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
        """Latest inclusion status per source id ('included'|'excluded')."""

    @abstractmethod
    def source_verified_overrides(self) -> dict[str, bool]:
        """Whether each source has been explicitly human-verified."""

    @abstractmethod
    def address_verified_overrides(self) -> dict[str, bool]:
        """Latest verification per (symbol, chain), keyed 'symbol:chain'."""

    @abstractmethod
    def curation_history(self) -> dict:
        """The full append-only curation ledger — for display / audit.

        Returns a dict with `source_decisions` and `address_decisions` lists,
        oldest-first, mirroring the votes.yaml shape.
        """

    # ── attestation URL overrides ─────────────────────────────────────
    # YAML's `latest_attestation_url` is a bootstrap seed. In production
    # the operational source of truth is the store, so freshly-discovered
    # URLs survive redeploys and curators can manage them live.
    @abstractmethod
    def get_attestation_url_override(self, symbol: str) -> dict | None:
        """Return the current store-side URL override for `symbol`, or None.

        Shape: {symbol, url, via, set_by (user_id or 'auto'), set_at, notes}.
        The override wins over the YAML seed; falsy `url` means "no override
        set", treat as no row.
        """

    @abstractmethod
    def set_attestation_url_override(
        self,
        symbol: str,
        url: str,
        *,
        via: str = "manual",
        set_by: str | None = None,
        notes: str = "",
    ) -> None:
        """Set the operational attestation URL for `symbol`.

        `via` is the discovery channel ('manual' | 'web_search' |
        'locator' | 'paxos_resolver' | ...) and `set_by` is the user_id
        for manual sets or None for automatic ones. Last-write-wins.
        """

    @abstractmethod
    def list_attestation_url_overrides(self) -> list[dict]:
        """All current overrides, newest first — for the Compendium UI."""

    # ── verified facts (immutable, time-series, object-agnostic) ─────
    # The audit trail. `analyses` and `monitor_snapshots` are the
    # operational cache; this is the immutable record. Lens 2 will use
    # the same table — `claim_type` is the discriminator. Append-only.
    @abstractmethod
    def record_verified_fact(
        self,
        *,
        claim_type: str,
        subject: str,
        value: dict,
        sources: list,
        status: str,
        block_number: int | None = None,
        chain: str | None = None,
        as_of: str | None = None,
        notes: str = "",
    ) -> str:
        """Append one verified fact. Returns its id.

        Content-hashes (value || sources) so a re-confirmation that
        produces an identical row is skipped at the writer side. Status
        must be 'verified' | 'unverified' | 'assumed'.
        """

    @abstractmethod
    def latest_verified_fact(
        self,
        claim_type: str,
        subject: str,
        *,
        as_of_lte: str | None = None,
    ) -> dict | None:
        """Most recent fact for (claim_type, subject). Optional point-in-
        time: only consider rows with `observed_at <= as_of_lte`."""

    @abstractmethod
    def list_verified_facts(
        self,
        *,
        claim_type: str | None = None,
        subject: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Recent facts, newest first, optionally filtered."""

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

    # ── movement simulator: peg ticks ─────────────────────────────────
    # The intraday spot/peg series. Cheap inserts, indexed by symbol and
    # read_at. None on optional fields is "we didn't observe it" — not
    # "we observed zero".
    def insert_peg_tick(
        self, *, symbol: str, source: str,
        price: float, deviation_bps: float,
        consensus_kind: str | None = None,
        sources: list | None = None,
        max_disagreement_bps: float | None = None,
    ) -> None:
        """Persist one peg-price tick. The multi-source orchestrator
        passes consensus_kind ('single' | 'agreed' | 'disputed'),
        the per-source sources array, and the max-disagreement-in-bps
        spread; backends that haven't yet implemented those columns
        (migration 0008 not applied) ignore the extra kwargs.

        Default no-op so backends that haven't implemented the
        simulator schema don't crash callers — the ticker logs the
        persist failure and keeps going."""
        return None

    def list_peg_ticks(
        self, symbol: str, *, limit: int = 200,
    ) -> list[dict]:
        """Recent peg ticks for `symbol`, newest first. Empty list when
        unsupported by the backend."""
        return []

    # ── movement simulator: predictions ───────────────────────────────
    # Immutable archive. The resolver writes a paired resolutions row;
    # nothing else modifies a row once inserted.
    def insert_prediction(self, prediction: dict) -> str:
        """Insert one prediction row. Returns the row id. `prediction`
        keys mirror the predictions table columns. Default no-op
        returns empty string so callers can detect unsupported."""
        return ""

    def list_predictions(
        self, *, symbol: str | None = None, kind: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Recent predictions, newest first. Optional symbol / kind
        filters. Returns plain dicts (column names verbatim)."""
        return []

    def unresolved_predictions(self, *, before_ts: str | None = None,
                                limit: int = 500) -> list[dict]:
        """Predictions whose resolves_at has passed but which lack a
        paired resolution row. The resolver thread reads from here on
        each cycle."""
        return []

    # ── movement simulator: resolutions ───────────────────────────────
    def insert_resolution(self, resolution: dict) -> str:
        """Insert one resolution row. Returns the row id. `resolution`
        keys mirror the resolutions table columns including
        prediction_id (the foreign key)."""
        return ""

    def list_resolutions(
        self, *, symbol: str | None = None, kind: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Recent resolved predictions, newest first. Joins predictions
        on the foreign key so each row carries enough context for the
        calibration UI (symbol, kind, made_at, point, bands)."""
        return []

    def calibration_summary(
        self, *, symbol: str | None = None, kind: str | None = None,
        horizon_minutes: int | None = None,
    ) -> dict:
        """Aggregate calibration metrics — Brier mean, CRPS mean,
        outcome-kind histogram, reliability bins. Optional filters
        narrow the slice. Empty struct when no resolutions exist."""
        return {
            "count": 0, "brier_mean": None, "crps_mean": None,
            "outcome_histogram": {}, "reliability_bins": [],
            "baseline_persistence_brier_mean": None,
            "baseline_climatology_brier_mean": None,
        }

    # ── trader: simulated trade ledger ────────────────────────────────
    # The Discipline Trader persists its trade history here so it joins
    # the same snapshot archive as peg_ticks, predictions, and
    # resolutions. The local JSON file (data/discipline_trader.json)
    # stays as a fast in-process cache, but every write also hits the
    # store so the ledger survives restarts AND can be queried
    # alongside the other simulation history.
    def insert_trade(self, trade: dict) -> str:
        """Insert one trade row at open. `trade` keys mirror the
        Trade dataclass. Returns the row id (or empty string when
        the backend doesn't support trades yet)."""
        return ""

    def update_trade_resolution(self, trade_id: str, fields: dict) -> None:
        """Update a trade row when it resolves — patches the
        resolution-side fields (exit_bps, outcome, pnl_usd, etc).
        No-op on backends without trade support."""
        return None

    def list_trades(self, *, limit: int = 500) -> list[dict]:
        """All trades (open + resolved), newest first. Empty list
        when the backend doesn't support trades yet."""
        return []

    # ── trader voice + commentary dive caches ─────────────────────────
    # v5.1: moved off local disk into the same archive as the rest of
    # the simulation history. Local JSON cache remains as a hot-path
    # fallback; the store is the durable source of truth.
    def upsert_voice_brief(self, brief: dict) -> None:
        return None

    def get_voice_brief(self, day_utc: str) -> Optional[dict]:
        return None

    def upsert_voice_reflection(self, reflection: dict) -> None:
        return None

    def get_voice_reflection(self, day_utc: str) -> Optional[dict]:
        return None

    def upsert_voice_narration(
        self, trade_id: str, body: str) -> None:
        return None

    def get_voice_narration(self, trade_id: str) -> Optional[str]:
        return None

    def upsert_commentary_dive(self, dive: dict) -> None:
        return None

    def get_commentary_dive(
        self, symbol: str, inputs_hash: str) -> Optional[dict]:
        return None
