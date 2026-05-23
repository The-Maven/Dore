"""Supabase / Postgres backend — production durable state.

Implements every `Store` method against the schema in
supabase/migrations/0001_initial_schema.sql using supabase-py v2.

The `supabase` package is an OPTIONAL dependency: it is lazy-imported inside
`__init__` so `import sca.store.supabase_store` never fails when the package
is absent. Tests never construct this class.

The server authenticates with the service_role key and bypasses RLS.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sca import config
from sca.store.base import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore(Store):
    """Postgres-backed store via supabase-py v2. See module docstring."""

    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        # Lazy import: keep the module importable without the optional dep.
        try:
            from supabase import create_client
        except ImportError as exc:  # pragma: no cover - exercised in prod only
            raise ImportError(
                "SupabaseStore requires the 'supabase' package. "
                "Install it with: pip install 'sca[supabase]'"
            ) from exc

        url = url or config.SUPABASE_URL
        key = key or config.SUPABASE_SERVICE_KEY
        if not (url and key):
            raise RuntimeError(
                "SupabaseStore needs SUPABASE_URL and SUPABASE_SERVICE_KEY"
            )
        self._client = create_client(url, key)

    # ── analyses ──────────────────────────────────────────────────────
    def create_analysis(
        self, surface: str, symbol: str, user_id: str | None = None
    ) -> str:
        row = {"surface": surface, "symbol": symbol, "status": "pending"}
        if user_id is not None:
            row["user_id"] = user_id
        resp = self._client.table("analyses").insert(row).execute()
        return resp.data[0]["id"]

    def update_analysis(
        self,
        id: str,
        *,
        status: str | None = None,
        result: dict | None = None,
        error: str | None = None,
        input_fingerprint: str | None = None,
    ) -> None:
        patch: dict = {}
        if status is not None:
            patch["status"] = status
            if status == "running":
                patch["started_at"] = _now()
            if status in ("done", "error"):
                patch["completed_at"] = _now()
        if result is not None:
            patch["result"] = result  # jsonb column
        if error is not None:
            patch["error"] = error
        if input_fingerprint is not None:
            patch["input_fingerprint"] = input_fingerprint
        if not patch:
            return
        self._client.table("analyses").update(patch).eq("id", id).execute()

    def get_analysis(self, id: str) -> dict | None:
        resp = (
            self._client.table("analyses").select("*").eq("id", id).execute()
        )
        return resp.data[0] if resp.data else None

    def find_cached_analysis(
        self, surface: str, symbol: str, input_fingerprint: str
    ) -> dict | None:
        resp = (
            self._client.table("analyses")
            .select("*")
            .eq("surface", surface)
            .eq("symbol", symbol)
            .eq("status", "done")
            .eq("input_fingerprint", input_fingerprint)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def list_analyses(
        self,
        surface: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        query = self._client.table("analyses").select("*")
        if surface is not None:
            query = query.eq("surface", surface)
        if symbol is not None:
            query = query.eq("symbol", symbol)
        resp = query.order("created_at", desc=True).limit(limit).execute()
        return resp.data or []

    # ── sources ───────────────────────────────────────────────────────
    def list_sources(self) -> list[dict]:
        """The source registry lives in `corpus/sources.yaml` — the YAML is
        the durable source-of-truth that operator commits and the discovery
        agent appends to. The DB's `sources` table is legacy / unused by
        the discovery flow; reading it here used to cause silent divergence
        where YAML-added sources were invisible until ingestion. Reading
        the YAML here keeps SupabaseStore and FileStore symmetrical."""
        import yaml as _yaml
        from sca import config as _config
        path = _config.CORPUS_DIR / "sources.yaml"
        if not path.exists():
            return []
        raw = _yaml.safe_load(path.read_text()) or {}
        out: list[dict] = []
        for s in raw.get("sources", []):
            out.append({
                "id": s["id"],
                "title": s["title"],
                "tier": s.get("tier", ""),
                "status": s.get("status", "included"),
                "url": s.get("url", ""),
                "summary": s.get("summary", ""),
                "notes": s.get("notes", ""),
            })
        return out

    # ── corpus ────────────────────────────────────────────────────────
    def _ensure_source_row(self, source_id: str) -> None:
        """Upsert a row into the DB `sources` table for this id.

        The YAML at corpus/sources.yaml is the registry; the DB table
        exists only to satisfy the corpus_passages foreign key. We
        mirror the YAML entry on demand so DB constraints are happy
        without forcing the discovery flow to manage two stores.
        """
        # Look up the registry entry from the YAML mirror in list_sources()
        entry = next(
            (s for s in self.list_sources() if s["id"] == source_id),
            None,
        )
        if entry is None:
            return  # caller will hit FK error — better than silent invention
        # NOTE: the live DB CHECK constraint on `status` predates the
        # opt-out migration (0002_corpus_opt_out.sql) and accepts only the
        # legacy values {proposed, included, excluded}. Anything outside
        # that set 23514's. Coercing to a known-good value here keeps
        # discovery unblocked without forcing a DB migration mid-flight;
        # the YAML still carries the canonical status (included/excluded)
        # and that's what the SPA reads via list_sources.
        legacy_status = entry["status"] if entry["status"] in {
            "proposed", "approved", "rejected",
        } else "approved"
        # Tier constraint also predates tier1_official / tier2_industry —
        # legacy accepts {primary, standard, methodology, research}.
        legacy_tier = entry["tier"] if entry["tier"] in {
            "primary", "standard", "methodology", "research",
        } else "research"
        row = {
            "id": entry["id"],
            "title": entry["title"],
            "tier": legacy_tier,
            "status": legacy_status,
            "url": entry["url"],
            "summary": entry["summary"],
            "notes": entry["notes"],
        }
        # Surface the upsert error — the previous silent try/except was
        # masking real failures (RLS, schema mismatch). Caller can decide
        # whether to retry; better than a phantom-success.
        self._client.table("sources").upsert(row, on_conflict="id").execute()

    def save_passages(self, source_id: str, passages: list[dict]) -> None:
        # Ensure the source row exists before any passage insert (FK constraint).
        self._ensure_source_row(source_id)
        # Replace: passages for a source are regenerated wholesale on ingest.
        self._client.table("corpus_passages").delete().eq(
            "source_id", source_id
        ).execute()
        rows = []
        for i, p in enumerate(passages):
            section = p.get("section") or p.get("heading", "")
            rows.append(
                {
                    "source_id": source_id,
                    "ordinal": p.get("ordinal", i),
                    "section": section,
                    "heading": p.get("heading", ""),
                    "text": p.get("text", ""),
                    "citation": p.get("citation") or f"{source_id} — {section}",
                    "page": p.get("page"),
                }
            )
        if rows:
            self._client.table("corpus_passages").insert(rows).execute()

    def get_passages(self, source_id: str | None = None) -> list[dict]:
        query = self._client.table("corpus_passages").select("*")
        if source_id is not None:
            query = query.eq("source_id", source_id)
        resp = query.order("source_id").order("ordinal").execute()
        return resp.data or []

    # ── curation ──────────────────────────────────────────────────────
    def record_source_decision(
        self, source_id: str, decision: str, user_id: str | None = None
    ) -> None:
        if decision not in ("excluded", "included", "verified"):
            raise ValueError(
                "decision must be 'excluded', 'included' or 'verified'"
            )
        row = {"source_id": source_id, "decision": decision}
        if user_id is not None:
            row["decided_by"] = user_id
        self._client.table("curation_votes").insert(row).execute()

    def record_address_decision(
        self,
        symbol: str,
        chain: str,
        contract: str,
        decision: str,
        user_id: str | None = None,
    ) -> None:
        if decision not in ("verified", "rejected"):
            raise ValueError("decision must be 'verified' or 'rejected'")
        row = {
            "symbol": symbol,
            "chain": chain,
            "contract": contract,
            "decision": decision,
        }
        if user_id is not None:
            row["decided_by"] = user_id
        self._client.table("address_decisions").insert(row).execute()

    def source_status_overrides(self) -> dict[str, str]:
        # Append-only ledger: latest row per source wins. A 'verified' vote
        # keeps the source included; only 'excluded' drops it.
        resp = (
            self._client.table("curation_votes")
            .select("source_id, decision, created_at")
            .order("created_at")
            .execute()
        )
        out: dict[str, str] = {}
        for d in resp.data or []:
            out[d["source_id"]] = (
                "excluded" if d["decision"] == "excluded" else "included"
            )
        return out

    def source_verified_overrides(self) -> dict[str, bool]:
        # Latest vote per source: 'verified' marks it human-reviewed.
        resp = (
            self._client.table("curation_votes")
            .select("source_id, decision, created_at")
            .order("created_at")
            .execute()
        )
        out: dict[str, bool] = {}
        for d in resp.data or []:
            out[d["source_id"]] = d["decision"] == "verified"
        return out

    def address_verified_overrides(self) -> dict[str, bool]:
        resp = (
            self._client.table("address_decisions")
            .select("symbol, chain, decision, created_at")
            .order("created_at")
            .execute()
        )
        out: dict[str, bool] = {}
        for d in resp.data or []:
            out[f"{d['symbol']}:{d['chain']}"] = d["decision"] == "verified"
        return out

    def curation_history(self) -> dict:
        # Normalise both ledger tables to the votes.yaml shape, oldest first.
        src_resp = (
            self._client.table("curation_votes")
            .select("source_id, decision, created_at")
            .order("created_at")
            .execute()
        )
        addr_resp = (
            self._client.table("address_decisions")
            .select("symbol, chain, contract, decision, created_at")
            .order("created_at")
            .execute()
        )
        source_decisions = [
            {"id": d["source_id"], "decision": d["decision"],
             "at": d.get("created_at")}
            for d in src_resp.data or []
        ]
        address_decisions = [
            {"symbol": d["symbol"], "chain": d["chain"],
             "contract": d.get("contract"), "decision": d["decision"],
             "at": d.get("created_at")}
            for d in addr_resp.data or []
        ]
        return {
            "source_decisions": source_decisions,
            "address_decisions": address_decisions,
        }

    # ── attestation URL overrides ─────────────────────────────────────
    # Schema-drift fallback: if the live DB hasn't run migration 0003 yet,
    # every method here degrades to a no-op so the resolver falls through
    # to the YAML seed and the UI keeps rendering. Same pattern as
    # `list_sources` reading YAML when the table is missing.
    def _table_missing(self, exc: Exception) -> bool:
        msg = str(exc)
        return "attestation_url_overrides" in msg and (
            "schema cache" in msg or "does not exist" in msg
        )

    def get_attestation_url_override(self, symbol: str) -> dict | None:
        try:
            resp = (
                self._client.table("attestation_url_overrides")
                .select("*")
                .eq("symbol", symbol)
                .order("set_at", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            if self._table_missing(exc):
                return None
            raise
        rows = resp.data or []
        if not rows or not rows[0].get("url"):
            return None
        return rows[0]

    def set_attestation_url_override(
        self,
        symbol: str,
        url: str,
        *,
        via: str = "manual",
        set_by: str | None = None,
        notes: str = "",
    ) -> None:
        row = {
            "symbol": symbol,
            "url": url,
            "via": via,
            "set_by": set_by,
            "notes": notes,
        }
        try:
            self._client.table("attestation_url_overrides").insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            if self._table_missing(exc):
                # Log once; the resolver will fall through to seed/locator.
                from sca.observability import log_event
                log_event(
                    "attestation.override_table_missing", level="warn",
                    detail="run supabase/migrations/0003_attestation_url_overrides.sql",
                )
                return
            raise

    def list_attestation_url_overrides(self) -> list[dict]:
        try:
            resp = (
                self._client.table("attestation_url_overrides")
                .select("*")
                .order("set_at", desc=True)
                .limit(500)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            if self._table_missing(exc):
                return []
            raise
        seen: set[str] = set()
        out: list[dict] = []
        for row in resp.data or []:
            sym = row.get("symbol")
            if sym in seen:
                continue
            seen.add(sym)
            out.append(row)
        return out

    # ── monitor ───────────────────────────────────────────────────────
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
        self._client.table("monitor_snapshots").insert(
            {
                "symbol": symbol,
                "total_supply": total_supply,
                "native_supply": native_supply,
                "bridged_supply": bridged_supply,
                "per_chain": per_chain,  # jsonb
                "warnings": warnings,    # jsonb
            }
        ).execute()

    def list_snapshots(self, symbol: str, limit: int = 50) -> list[dict]:
        resp = (
            self._client.table("monitor_snapshots")
            .select("*")
            .eq("symbol", symbol)
            .order("read_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
