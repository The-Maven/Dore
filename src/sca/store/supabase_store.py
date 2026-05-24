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


# Schema-missing detection — pushed into the store layer so the audit
# trail records WHICH table is missing, and callers don't have to
# rebuild this logic. PostgREST 11+ returns code PGRST205 with a
# 'Could not find the table' message; older versions return PGRST204
# or '42P01' from PostgreSQL. We accept all three.
_SCHEMA_MISSING_TOKENS = (
    "pgrst205", "pgrst204", "42p01",
    "could not find the table", "relation does not exist",
)


def _is_schema_missing_error(exc: BaseException) -> bool:
    """True if the exception looks like a missing-table error.
    Matches by message substring (case-insensitive) because the
    APIError object's structure varies across supabase-py versions."""
    msg = str(exc).lower()
    return any(token in msg for token in _SCHEMA_MISSING_TOKENS)


def _log_schema_missing(table: str, exc: BaseException) -> None:
    """Emit a single observability event for a missing-table state.
    Callers fall through to an empty result; the event is the trail."""
    from sca.observability import log_event
    log_event(
        "store.schema_missing", level="warn",
        table=table, error_class=type(exc).__name__,
        error_message=str(exc)[:200],
    )


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
        # Post-0002 (status: included/excluded) + post-0005 (tier
        # widened to the full VALID_TIERS vocabulary), both fields
        # pass through verbatim from the YAML registry. The legacy
        # coercion shims this used to carry are gone.
        row = {
            "id": entry["id"],
            "title": entry["title"],
            "tier": entry["tier"],
            "status": entry["status"],
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

    # ── verified facts (immutable audit trail) ────────────────────────
    # Schema-drift fallback identical to attestation_url_overrides above:
    # if migration 0004 hasn't been applied yet, methods degrade so the
    # resolver / store callers keep working.
    def _verified_facts_missing(self, exc: Exception) -> bool:
        msg = str(exc)
        return "verified_facts" in msg and (
            "schema cache" in msg or "does not exist" in msg
        )

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
        import hashlib
        import json as _json
        if status not in ("verified", "unverified", "assumed"):
            raise ValueError(
                f"status must be verified|unverified|assumed, got {status!r}"
            )
        canonical = _json.dumps(
            {"value": value, "sources": sources},
            sort_keys=True, separators=(",", ":"),
        )
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        row = {
            "claim_type": claim_type,
            "subject": subject,
            "value": value,
            "sources": sources,
            "block_number": block_number,
            "chain": chain,
            "as_of": as_of,
            "status": status,
            "content_hash": content_hash,
            "notes": notes,
        }
        try:
            resp = (
                self._client.table("verified_facts").insert(row).execute()
            )
        except Exception as exc:  # noqa: BLE001
            if self._verified_facts_missing(exc):
                from sca.observability import log_event
                log_event(
                    "verified_facts.table_missing", level="warn",
                    detail="run supabase/migrations/0004_verified_facts.sql",
                )
                return ""
            raise
        return resp.data[0]["id"] if resp.data else ""

    def latest_verified_fact(
        self,
        claim_type: str,
        subject: str,
        *,
        as_of_lte: str | None = None,
    ) -> dict | None:
        try:
            q = (
                self._client.table("verified_facts")
                .select("*")
                .eq("claim_type", claim_type)
                .eq("subject", subject)
                .order("observed_at", desc=True)
                .limit(1)
            )
            if as_of_lte:
                q = q.lte("observed_at", as_of_lte)
            resp = q.execute()
        except Exception as exc:  # noqa: BLE001
            if self._verified_facts_missing(exc):
                return None
            raise
        rows = resp.data or []
        return rows[0] if rows else None

    def list_verified_facts(
        self,
        *,
        claim_type: str | None = None,
        subject: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        try:
            q = (
                self._client.table("verified_facts")
                .select("*")
                .order("observed_at", desc=True)
                .limit(limit)
            )
            if claim_type:
                q = q.eq("claim_type", claim_type)
            if subject:
                q = q.eq("subject", subject)
            resp = q.execute()
        except Exception as exc:  # noqa: BLE001
            if self._verified_facts_missing(exc):
                return []
            raise
        return resp.data or []

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

    # ── movement simulator: peg ticks ─────────────────────────────────
    def insert_peg_tick(
        self, *, symbol: str, source: str,
        price: float, deviation_bps: float,
    ) -> None:
        # numeric columns accept Python float; Supabase serialises to
        # JSON with full precision so a sub-bp price doesn't round.
        self._client.table("peg_ticks").insert({
            "symbol": symbol,
            "source": source,
            "price": price,
            "deviation_bps": deviation_bps,
        }).execute()

    def list_peg_ticks(
        self, symbol: str, *, limit: int = 200,
    ) -> list[dict]:
        resp = (
            self._client.table("peg_ticks")
            .select("*")
            .eq("symbol", symbol)
            .order("read_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []

    # ── movement simulator: predictions ───────────────────────────────
    def insert_prediction(self, prediction: dict) -> str:
        # Forward only the columns the table declares. Drivers must be
        # a list (jsonb default '[]') — even an empty list is honest;
        # null would mean "we don't track this", which is wrong.
        row = {
            "symbol": prediction["symbol"],
            "kind": prediction["kind"],
            "horizon_minutes": int(prediction["horizon_minutes"]),
            "resolves_at": prediction["resolves_at"],
            "point": prediction["point"],
            "p50_low": prediction.get("p50_low"),
            "p50_high": prediction.get("p50_high"),
            "p80_low": prediction.get("p80_low"),
            "p80_high": prediction.get("p80_high"),
            "p95_low": prediction.get("p95_low"),
            "p95_high": prediction.get("p95_high"),
            "prob_positive": prediction.get("prob_positive"),
            "confidence_word": prediction.get("confidence_word"),
            "drivers": prediction.get("drivers", []),
            "model": prediction["model"],
            "notes": prediction.get("notes", ""),
            "judge_synthesis": prediction.get("judge_synthesis"),
            "judge_insight": prediction.get("judge_insight"),
            "judge_pitch": prediction.get("judge_pitch"),
            "judge_model": prediction.get("judge_model"),
        }
        if "made_at" in prediction:
            row["made_at"] = prediction["made_at"]
        resp = (
            self._client.table("predictions")
            .insert(row)
            .execute()
        )
        data = resp.data or []
        return (data[0].get("id") if data else "") or ""

    def list_predictions(
        self, *, symbol: str | None = None, kind: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        try:
            q = self._client.table("predictions").select("*")
            if symbol is not None:
                q = q.eq("symbol", symbol)
            if kind is not None:
                q = q.eq("kind", kind)
            resp = q.order("made_at", desc=True).limit(limit).execute()
            return resp.data or []
        except Exception as exc:  # noqa: BLE001
            # Audit #18: detect the predictions-table-missing error
            # at the store layer so a direct caller (agent bridge,
            # future tools) gets the same honest 'no data because
            # the schema isn't provisioned yet' signal the API
            # already provides. PGRST205 = relation does not exist
            # in PostgREST 11+. Other errors re-raise so genuine
            # store failures aren't silently swallowed.
            if not _is_schema_missing_error(exc):
                raise
            _log_schema_missing("predictions", exc)
            return []

    def unresolved_predictions(
        self, *, before_ts: str | None = None, limit: int = 500,
    ) -> list[dict]:
        # The resolver thread runs every minute and pulls predictions
        # whose resolves_at has passed but which lack a paired
        # resolutions row. PostgREST doesn't natively express an
        # anti-join, so we fetch a candidate window then drop the
        # already-resolved ids in Python. The window is bounded by
        # `limit` so a backlog drains across cycles.
        from datetime import datetime, timezone
        cutoff = before_ts or datetime.now(timezone.utc).isoformat()
        q = (
            self._client.table("predictions")
            .select("*")
            .lte("resolves_at", cutoff)
            .order("resolves_at")
            .limit(limit)
        )
        candidates = (q.execute().data or [])
        if not candidates:
            return []
        ids = [c["id"] for c in candidates]
        resolved_resp = (
            self._client.table("resolutions")
            .select("prediction_id")
            .in_("prediction_id", ids)
            .execute()
        )
        resolved_ids = {r["prediction_id"]
                        for r in (resolved_resp.data or [])}
        return [c for c in candidates if c["id"] not in resolved_ids]

    # ── movement simulator: resolutions ───────────────────────────────
    def insert_resolution(self, resolution: dict) -> str:
        row = {
            "prediction_id": resolution["prediction_id"],
            "actual_value": resolution["actual_value"],
            "brier_score": resolution.get("brier_score"),
            "crps_score": resolution.get("crps_score"),
            "outcome_kind": resolution["outcome_kind"],
            "narrative": resolution.get("narrative", ""),
            "baseline_persistence_brier":
                resolution.get("baseline_persistence_brier"),
            "baseline_climatology_brier":
                resolution.get("baseline_climatology_brier"),
        }
        try:
            resp = (
                self._client.table("resolutions")
                .insert(row)
                .execute()
            )
        except Exception:  # noqa: BLE001
            # Unique-constraint violation on prediction_id (already
            # resolved) is benign — the resolver is idempotent across
            # retries. Other errors are re-raised so the caller logs.
            return ""
        data = resp.data or []
        return (data[0].get("id") if data else "") or ""

    def list_resolutions(
        self, *, symbol: str | None = None, kind: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        # PostgREST embedded resource: pull resolutions and embed the
        # parent prediction row. Then flatten into the dict shape the
        # FileStore returns so callers stay backend-agnostic.
        try:
            q = self._client.table("resolutions").select(
                "*, prediction:predictions(*)"
            )
            resp = q.order("resolved_at", desc=True).limit(limit).execute()
        except Exception as exc:  # noqa: BLE001
            if not _is_schema_missing_error(exc):
                raise
            _log_schema_missing("resolutions", exc)
            return []
        out = []
        for row in (resp.data or []):
            pred = row.get("prediction") or {}
            if symbol is not None and pred.get("symbol") != symbol:
                continue
            if kind is not None and pred.get("kind") != kind:
                continue
            flat = dict(pred)
            flat.update({
                "resolution_id": row.get("id"),
                "resolved_at": row.get("resolved_at"),
                "actual_value": row.get("actual_value"),
                "brier_score": row.get("brier_score"),
                "crps_score": row.get("crps_score"),
                "outcome_kind": row.get("outcome_kind"),
                "narrative": row.get("narrative", ""),
                "baseline_persistence_brier":
                    row.get("baseline_persistence_brier"),
                "baseline_climatology_brier":
                    row.get("baseline_climatology_brier"),
            })
            out.append(flat)
        return out

    def calibration_summary(
        self, *, symbol: str | None = None, kind: str | None = None,
        horizon_minutes: int | None = None,
    ) -> dict:
        # Probe the resolutions table directly so a missing-schema
        # state surfaces with an explicit marker — callers can tell
        # 'no rows yet' from 'table doesn't exist'. The list_resolutions
        # path below would also catch this, but we need the marker on
        # the empty struct, not just an empty list.
        try:
            (self._client.table("resolutions")
             .select("id").limit(1).execute())
        except Exception as exc:  # noqa: BLE001
            if _is_schema_missing_error(exc):
                _log_schema_missing("resolutions", exc)
                return {
                    "count": 0, "brier_mean": None, "crps_mean": None,
                    "outcome_histogram": {}, "reliability_bins": [],
                    "baseline_persistence_brier_mean": None,
                    "baseline_climatology_brier_mean": None,
                    "schema_missing": True,
                }
            raise

        # Single-pass aggregation in Python over the joined resolutions
        # because PostgREST aggregation is limited and the calibration
        # math (reliability bins) needs the per-row data anyway.
        rows = self.list_resolutions(symbol=symbol, kind=kind, limit=2000)
        if horizon_minutes is not None:
            rows = [r for r in rows
                    if r.get("horizon_minutes") == horizon_minutes]
        if not rows:
            return {
                "count": 0, "brier_mean": None, "crps_mean": None,
                "outcome_histogram": {}, "reliability_bins": [],
                "baseline_persistence_brier_mean": None,
                "baseline_climatology_brier_mean": None,
            }
        briers = [r["brier_score"] for r in rows if r.get("brier_score") is not None]
        crps = [r["crps_score"] for r in rows if r.get("crps_score") is not None]
        bp = [r["baseline_persistence_brier"] for r in rows
              if r.get("baseline_persistence_brier") is not None]
        bc = [r["baseline_climatology_brier"] for r in rows
              if r.get("baseline_climatology_brier") is not None]
        hist: dict[str, int] = {}
        for r in rows:
            k = r.get("outcome_kind", "unknown")
            hist[k] = hist.get(k, 0) + 1

        bins: list[dict] = []
        for low in range(0, 100, 10):
            high = low + 10
            in_bin = [
                r for r in rows
                if r.get("prob_positive") is not None
                and low / 100.0 <= float(r["prob_positive"]) < high / 100.0
            ]
            if not in_bin:
                continue
            # Reliability bin empirical rate for a DIRECTION prediction
            # = fraction of times the outcome went positive. The
            # earlier 'outcome_kind in {hit, inside_p50}' check was
            # wrong: that conflates band-containment with direction
            # for continuous predictions and is meaningless for the
            # binary case.
            positives = sum(
                1 for r in in_bin
                if r.get("actual_value") is not None
                and float(r["actual_value"]) > 0
            )
            bins.append({
                "lower_pct": low,
                "upper_pct": high,
                "midpoint": (low + high) / 200.0,
                "count": len(in_bin),
                "empirical_rate": positives / len(in_bin),
                "predicted_mean": sum(
                    float(r.get("prob_positive", 0)) for r in in_bin
                ) / len(in_bin),
            })

        def _mean(xs: list) -> float | None:
            return sum(float(x) for x in xs) / len(xs) if xs else None

        return {
            "count": len(rows),
            "brier_mean": _mean(briers),
            "crps_mean": _mean(crps),
            "outcome_histogram": hist,
            "reliability_bins": bins,
            "baseline_persistence_brier_mean": _mean(bp),
            "baseline_climatology_brier_mean": _mean(bc),
        }
