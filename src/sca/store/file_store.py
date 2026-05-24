"""File / in-memory backend — keeps the app working offline and tests hermetic.

Domain-by-domain mapping to existing on-disk state:
  - sources           → corpus/sources.yaml          (read-only)
  - curation          → votes.yaml                   (read / append)
  - corpus_passages   → corpus/data/<source_id>.json  (read / write)
  - analyses          → process memory  (no current file equivalent)
  - monitor_snapshots → process memory  (no current file equivalent)

The votes.yaml format and its "latest decision wins" semantics match
sca.votes exactly, so this backend is a drop-in for the current behaviour.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from sca import config
from sca.store.base import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileStore(Store):
    """File-backed + in-memory store. See module docstring."""

    def __init__(
        self,
        *,
        votes_path: Path | None = None,
        sources_path: Path | None = None,
        corpus_dir: Path | None = None,
        attestation_overrides_path: Path | None = None,
        verified_facts_path: Path | None = None,
    ) -> None:
        # Paths default to the repo layout; overridable for hermetic tests.
        self._votes_path = votes_path or (config.ROOT / "votes.yaml")
        self._sources_path = sources_path or (config.CORPUS_DIR / "sources.yaml")
        self._corpus_dir = corpus_dir or (config.CORPUS_DIR / "data")
        self._att_overrides_path = (
            attestation_overrides_path
            or (config.DATA_DIR / "attestation_overrides.json")
        )
        self._verified_facts_path = (
            verified_facts_path
            or (config.DATA_DIR / "verified_facts.jsonl")
        )
        # analyses + monitor have no on-disk equivalent: keep them in memory.
        self._analyses: dict[str, dict] = {}
        self._snapshots: list[dict] = []
        # Movement simulator: immutable archives held in memory for the
        # FileStore. Production runs on SupabaseStore where they hit a
        # real table; this lets tests + offline dev exercise the same
        # call surface end-to-end without a network.
        self._peg_ticks: list[dict] = []
        self._predictions: list[dict] = []
        self._resolutions: list[dict] = []
        # Audit #8: protect concurrent append + iterate on the
        # in-memory archives. RLock so unresolved_predictions can
        # read _resolutions inside a held lock without deadlock.
        # SupabaseStore enforces consistency at the DB; this only
        # matters for the FileStore (tests + offline dev + the
        # background ticker + resolver running in one process).
        import threading
        self._sim_lock = threading.RLock()

    # ── votes.yaml helpers (mirror sca.votes) ─────────────────────────
    _SECTIONS = ("source_decisions", "address_decisions")

    def _load_votes(self) -> dict:
        data: dict = {}
        if self._votes_path.exists():
            data = yaml.safe_load(self._votes_path.read_text()) or {}
        for section in self._SECTIONS:
            data.setdefault(section, [])
        return data

    def _save_votes(self, data: dict) -> None:
        from sca.persist import atomic_write_text
        atomic_write_text(self._votes_path, yaml.safe_dump(data, sort_keys=False))

    # ── analyses ──────────────────────────────────────────────────────
    def create_analysis(
        self, surface: str, symbol: str, user_id: str | None = None
    ) -> str:
        analysis_id = str(uuid.uuid4())
        self._analyses[analysis_id] = {
            "id": analysis_id,
            "user_id": user_id,
            "surface": surface,
            "symbol": symbol,
            "status": "pending",
            "input_fingerprint": None,
            "result": None,
            "error": None,
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
        }
        return analysis_id

    def update_analysis(
        self,
        id: str,
        *,
        status: str | None = None,
        result: dict | None = None,
        error: str | None = None,
        input_fingerprint: str | None = None,
    ) -> None:
        row = self._analyses.get(id)
        if row is None:
            raise KeyError(f"unknown analysis: {id!r}")
        if status is not None:
            row["status"] = status
            if status == "running" and row["started_at"] is None:
                row["started_at"] = _now()
            if status in ("done", "error"):
                row["completed_at"] = _now()
        if result is not None:
            row["result"] = result
        if error is not None:
            row["error"] = error
        if input_fingerprint is not None:
            row["input_fingerprint"] = input_fingerprint

    def get_analysis(self, id: str) -> dict | None:
        row = self._analyses.get(id)
        return dict(row) if row is not None else None

    def find_cached_analysis(
        self, surface: str, symbol: str, input_fingerprint: str
    ) -> dict | None:
        matches = [
            r
            for r in self._analyses.values()
            if r["surface"] == surface
            and r["symbol"] == symbol
            and r["status"] == "done"
            and r["input_fingerprint"] == input_fingerprint
        ]
        if not matches:
            return None
        matches.sort(key=lambda r: r["created_at"], reverse=True)
        return dict(matches[0])

    def list_analyses(
        self,
        surface: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        rows = list(self._analyses.values())
        if surface is not None:
            rows = [r for r in rows if r["surface"] == surface]
        if symbol is not None:
            rows = [r for r in rows if r["symbol"] == symbol]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    # ── sources ───────────────────────────────────────────────────────
    def list_sources(self) -> list[dict]:
        if not self._sources_path.exists():
            return []
        raw = yaml.safe_load(self._sources_path.read_text()) or {}
        out: list[dict] = []
        for s in raw.get("sources", []):
            out.append(
                {
                    "id": s["id"],
                    "title": s["title"],
                    "tier": s.get("tier", ""),
                    "status": s.get("status", "proposed"),
                    "url": s.get("url", ""),
                    "summary": s.get("summary", ""),
                    "notes": s.get("notes", ""),
                }
            )
        return out

    # ── corpus ────────────────────────────────────────────────────────
    def save_passages(self, source_id: str, passages: list[dict]) -> None:
        from sca.persist import atomic_write_json
        rows = [self._normalise_passage(source_id, i, p)
                for i, p in enumerate(passages)]
        atomic_write_json(
            self._corpus_dir / f"{source_id}.json", rows, sort_keys=False,
        )

    def get_passages(self, source_id: str | None = None) -> list[dict]:
        out: list[dict] = []
        if not self._corpus_dir.exists():
            return out
        if source_id is not None:
            files = [self._corpus_dir / f"{source_id}.json"]
        else:
            files = sorted(self._corpus_dir.glob("*.json"))
        for path in files:
            if not path.exists():
                continue
            sid = path.stem
            raw = json.loads(path.read_text())
            for i, p in enumerate(raw):
                out.append(self._normalise_passage(sid, i, p))
        out.sort(key=lambda r: (r["source_id"], r["ordinal"]))
        return out

    @staticmethod
    def _normalise_passage(source_id: str, ordinal: int, p: dict) -> dict:
        """Coerce a chunk dict to the corpus_passages shape.

        The on-disk JSON (sca.corpus.ingest.Chunk) has no `ordinal`/`citation`
        columns; synthesise them so callers see the same shape both backends
        return. `section` defaults from `heading` when absent.
        """
        section = p.get("section") or p.get("heading", "")
        heading = p.get("heading", "")
        return {
            "source_id": p.get("source_id", source_id),
            "ordinal": p.get("ordinal", ordinal),
            "section": section,
            "heading": heading,
            "text": p.get("text", ""),
            "citation": p.get("citation") or f"{source_id} — {section}",
            "page": p.get("page"),
        }

    # ── curation ──────────────────────────────────────────────────────
    _SOURCE_DECISIONS = ("excluded", "included", "verified")

    def record_source_decision(
        self, source_id: str, decision: str, user_id: str | None = None
    ) -> None:
        if decision not in self._SOURCE_DECISIONS:
            raise ValueError(
                "decision must be 'excluded', 'included' or 'verified'"
            )
        data = self._load_votes()
        # Same record shape as sca.votes; user_id is an additive field.
        entry = {"id": source_id, "decision": decision, "at": str(date.today())}
        if user_id is not None:
            entry["decided_by"] = user_id
        data["source_decisions"].append(entry)
        self._save_votes(data)

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
        data = self._load_votes()
        entry = {
            "symbol": symbol,
            "chain": chain,
            "contract": contract,
            "decision": decision,
            "at": str(date.today()),
        }
        if user_id is not None:
            entry["decided_by"] = user_id
        data["address_decisions"].append(entry)
        self._save_votes(data)

    def source_status_overrides(self) -> dict[str, str]:
        # A 'verified' vote keeps the source included; it never excludes.
        out: dict[str, str] = {}
        for d in self._load_votes()["source_decisions"]:
            out[d["id"]] = "excluded" if d["decision"] == "excluded" \
                else "included"
        return out

    def source_verified_overrides(self) -> dict[str, bool]:
        # A 'verified' vote marks a source human-reviewed; any later
        # include/exclude vote clears that explicit signal.
        out: dict[str, bool] = {}
        for d in self._load_votes()["source_decisions"]:
            out[d["id"]] = d["decision"] == "verified"
        return out

    def address_verified_overrides(self) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for d in self._load_votes()["address_decisions"]:
            out[f"{d['symbol']}:{d['chain']}"] = d["decision"] == "verified"
        return out

    def curation_history(self) -> dict:
        data = self._load_votes()
        return {section: list(data[section]) for section in self._SECTIONS}

    # ── attestation URL overrides ─────────────────────────────────────
    def _load_attestation_overrides(self) -> dict[str, dict]:
        if not self._att_overrides_path.exists():
            return {}
        try:
            return json.loads(self._att_overrides_path.read_text())
        except json.JSONDecodeError:
            return {}

    def _save_attestation_overrides(self, data: dict[str, dict]) -> None:
        from sca.persist import atomic_write_json
        atomic_write_json(self._att_overrides_path, data, sort_keys=False)

    def get_attestation_url_override(self, symbol: str) -> dict | None:
        row = self._load_attestation_overrides().get(symbol)
        if not row or not row.get("url"):
            return None
        return dict(row)

    def set_attestation_url_override(
        self,
        symbol: str,
        url: str,
        *,
        via: str = "manual",
        set_by: str | None = None,
        notes: str = "",
    ) -> None:
        data = self._load_attestation_overrides()
        data[symbol] = {
            "symbol": symbol,
            "url": url,
            "via": via,
            "set_by": set_by or "auto",
            "set_at": _now(),
            "notes": notes,
        }
        self._save_attestation_overrides(data)

    def list_attestation_url_overrides(self) -> list[dict]:
        data = self._load_attestation_overrides()
        rows = list(data.values())
        rows.sort(key=lambda r: r.get("set_at", ""), reverse=True)
        return [dict(r) for r in rows]

    # ── verified facts (append-only JSONL) ────────────────────────────
    # JSONL chosen over JSON-array so each append is one line write — no
    # full-file rewrite per fact, naturally append-only. The file IS the
    # audit trail; readers parse it line by line.
    def _verified_facts_iter(self):
        import json as _json
        if not self._verified_facts_path.exists():
            return
        with self._verified_facts_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield _json.loads(line)
                except _json.JSONDecodeError:
                    continue

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
            "id": str(uuid.uuid4()),
            "claim_type": claim_type,
            "subject": subject,
            "value": value,
            "sources": sources,
            "block_number": block_number,
            "chain": chain,
            "observed_at": _now(),
            "as_of": as_of,
            "status": status,
            "content_hash": content_hash,
            "superseded_by": None,
            "notes": notes,
        }
        # Append-only write — JSONL line. atomic_write isn't appropriate
        # here (it rewrites the whole file); instead open in append mode
        # with fsync so a crash mid-write leaves only a partial line that
        # the JSONDecodeError handler skips.
        self._verified_facts_path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps(row, sort_keys=True, default=str) + "\n"
        import os as _os
        with self._verified_facts_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            _os.fsync(f.fileno())
        return row["id"]

    def latest_verified_fact(
        self,
        claim_type: str,
        subject: str,
        *,
        as_of_lte: str | None = None,
    ) -> dict | None:
        best = None
        for row in self._verified_facts_iter():
            if row.get("claim_type") != claim_type:
                continue
            if row.get("subject") != subject:
                continue
            if as_of_lte and row.get("observed_at", "") > as_of_lte:
                continue
            if best is None or row.get("observed_at", "") > best.get(
                "observed_at", ""
            ):
                best = row
        return dict(best) if best else None

    def list_verified_facts(
        self,
        *,
        claim_type: str | None = None,
        subject: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        rows = []
        for row in self._verified_facts_iter():
            if claim_type and row.get("claim_type") != claim_type:
                continue
            if subject and row.get("subject") != subject:
                continue
            rows.append(row)
        rows.sort(key=lambda r: r.get("observed_at", ""), reverse=True)
        return [dict(r) for r in rows[:limit]]

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
        self._snapshots.append(
            {
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "total_supply": total_supply,
                "native_supply": native_supply,
                "bridged_supply": bridged_supply,
                "per_chain": per_chain,
                "warnings": warnings,
                "read_at": _now(),
            }
        )

    def list_snapshots(self, symbol: str, limit: int = 50) -> list[dict]:
        rows = [s for s in self._snapshots if s["symbol"] == symbol]
        rows.sort(key=lambda r: r["read_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    # ── movement simulator: peg ticks ─────────────────────────────────
    def insert_peg_tick(
        self, *, symbol: str, source: str,
        price: float, deviation_bps: float,
    ) -> None:
        row = {
            "id": str(uuid.uuid4()),
            "symbol": symbol,
            "source": source,
            "price": price,
            "deviation_bps": deviation_bps,
            "read_at": _now(),
        }
        with self._sim_lock:
            self._peg_ticks.append(row)

    def list_peg_ticks(
        self, symbol: str, *, limit: int = 200,
    ) -> list[dict]:
        with self._sim_lock:
            snapshot = list(self._peg_ticks)
        rows = [t for t in snapshot if t["symbol"] == symbol]
        rows.sort(key=lambda r: r["read_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    # ── movement simulator: predictions ───────────────────────────────
    def insert_prediction(self, prediction: dict) -> str:
        # Generate id + made_at server-side if not supplied so the
        # archive's invariants (every row has both) hold across
        # backends.
        row = dict(prediction)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("made_at", _now())
        row.setdefault("drivers", [])
        row.setdefault("notes", "")
        with self._sim_lock:
            self._predictions.append(row)
        return row["id"]

    def list_predictions(
        self, *, symbol: str | None = None, kind: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        with self._sim_lock:
            rows = list(self._predictions)
        if symbol is not None:
            rows = [r for r in rows if r.get("symbol") == symbol]
        if kind is not None:
            rows = [r for r in rows if r.get("kind") == kind]
        rows.sort(key=lambda r: r.get("made_at", ""), reverse=True)
        return [dict(r) for r in rows[:limit]]

    def unresolved_predictions(
        self, *, before_ts: str | None = None, limit: int = 500,
    ) -> list[dict]:
        # Snapshot both lists under one lock so a resolutions write
        # between the two reads can't make us double-count a
        # prediction as unresolved AND already-resolved.
        with self._sim_lock:
            resolved = {r.get("prediction_id") for r in self._resolutions}
            predictions_snapshot = list(self._predictions)
        cutoff = before_ts or _now()
        rows = [
            r for r in predictions_snapshot
            if r.get("id") not in resolved
            and r.get("resolves_at", "") <= cutoff
        ]
        rows.sort(key=lambda r: r.get("resolves_at", ""))
        return [dict(r) for r in rows[:limit]]

    # ── movement simulator: resolutions ───────────────────────────────
    def insert_resolution(self, resolution: dict) -> str:
        row = dict(resolution)
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("resolved_at", _now())
        row.setdefault("narrative", "")
        # Enforce 1:1 with predictions like the SQL unique constraint
        # — a second resolution for the same prediction_id is dropped
        # silently so the calibration archive stays clean. The check
        # + append happen under one lock so two threads racing to
        # grade the same prediction can't both succeed.
        prediction_id = row.get("prediction_id")
        with self._sim_lock:
            if prediction_id is not None and any(
                r.get("prediction_id") == prediction_id
                for r in self._resolutions
            ):
                return ""
            self._resolutions.append(row)
        return row["id"]

    def list_resolutions(
        self, *, symbol: str | None = None, kind: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        with self._sim_lock:
            preds_snapshot = list(self._predictions)
            res_snapshot = list(self._resolutions)
        by_pred = {r["id"]: r for r in preds_snapshot if "id" in r}
        out = []
        for res in res_snapshot:
            pred = by_pred.get(res.get("prediction_id"))
            if pred is None:
                continue
            if symbol is not None and pred.get("symbol") != symbol:
                continue
            if kind is not None and pred.get("kind") != kind:
                continue
            joined = dict(pred)
            joined.update({
                "resolution_id": res.get("id"),
                "resolved_at": res.get("resolved_at"),
                "actual_value": res.get("actual_value"),
                "brier_score": res.get("brier_score"),
                "crps_score": res.get("crps_score"),
                "outcome_kind": res.get("outcome_kind"),
                "narrative": res.get("narrative", ""),
                "baseline_persistence_brier":
                    res.get("baseline_persistence_brier"),
                "baseline_climatology_brier":
                    res.get("baseline_climatology_brier"),
            })
            out.append(joined)
        out.sort(key=lambda r: r.get("resolved_at", ""), reverse=True)
        return out[:limit]

    def calibration_summary(
        self, *, symbol: str | None = None, kind: str | None = None,
        horizon_minutes: int | None = None,
    ) -> dict:
        # Read predictions + their resolutions in one pass; build the
        # aggregate the calibration UI needs.
        by_pred = {r["id"]: r for r in self._predictions if "id" in r}
        rows = []
        for res in self._resolutions:
            pred = by_pred.get(res.get("prediction_id"))
            if pred is None:
                continue
            if symbol is not None and pred.get("symbol") != symbol:
                continue
            if kind is not None and pred.get("kind") != kind:
                continue
            if (horizon_minutes is not None
                    and pred.get("horizon_minutes") != horizon_minutes):
                continue
            rows.append((pred, res))

        if not rows:
            return {
                "count": 0, "brier_mean": None, "crps_mean": None,
                "outcome_histogram": {}, "reliability_bins": [],
                "baseline_persistence_brier_mean": None,
                "baseline_climatology_brier_mean": None,
            }

        briers = [r[1].get("brier_score") for r in rows
                  if r[1].get("brier_score") is not None]
        crps = [r[1].get("crps_score") for r in rows
                if r[1].get("crps_score") is not None]
        baselines_p = [r[1].get("baseline_persistence_brier") for r in rows
                       if r[1].get("baseline_persistence_brier") is not None]
        baselines_c = [r[1].get("baseline_climatology_brier") for r in rows
                       if r[1].get("baseline_climatology_brier") is not None]
        hist: dict[str, int] = {}
        for _, res in rows:
            k = res.get("outcome_kind", "unknown")
            hist[k] = hist.get(k, 0) + 1

        # Reliability bins for binary direction predictions: bucket
        # prob_positive into 10% buckets and compute empirical hit
        # rate. Only rows with a numeric prob_positive count.
        bins: list[dict] = []
        for low in range(0, 100, 10):
            high = low + 10
            in_bin = [
                (p, r) for p, r in rows
                if p.get("prob_positive") is not None
                and low / 100.0 <= p["prob_positive"] < high / 100.0
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
                1 for _, r in in_bin
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
                    p.get("prob_positive", 0.0) for p, _ in in_bin
                ) / len(in_bin),
            })

        def _mean(xs: list) -> float | None:
            return sum(xs) / len(xs) if xs else None

        return {
            "count": len(rows),
            "brier_mean": _mean(briers),
            "crps_mean": _mean(crps),
            "outcome_histogram": hist,
            "reliability_bins": bins,
            "baseline_persistence_brier_mean": _mean(baselines_p),
            "baseline_climatology_brier_mean": _mean(baselines_c),
        }
