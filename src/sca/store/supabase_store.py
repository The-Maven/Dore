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
        resp = (
            self._client.table("sources")
            .select("*")
            .order("created_at")
            .execute()
        )
        return resp.data or []

    # ── corpus ────────────────────────────────────────────────────────
    def save_passages(self, source_id: str, passages: list[dict]) -> None:
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
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
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
        # Append-only ledger: latest row per source wins.
        resp = (
            self._client.table("curation_votes")
            .select("source_id, decision, created_at")
            .order("created_at")
            .execute()
        )
        out: dict[str, str] = {}
        for d in resp.data or []:
            out[d["source_id"]] = d["decision"]
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
