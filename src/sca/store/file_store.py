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
    ) -> None:
        # Paths default to the repo layout; overridable for hermetic tests.
        self._votes_path = votes_path or (config.ROOT / "votes.yaml")
        self._sources_path = sources_path or (config.CORPUS_DIR / "sources.yaml")
        self._corpus_dir = corpus_dir or (config.CORPUS_DIR / "data")
        # analyses + monitor have no on-disk equivalent: keep them in memory.
        self._analyses: dict[str, dict] = {}
        self._snapshots: list[dict] = []

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
