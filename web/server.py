"""FastAPI server for the Stablecoin Compliance Agent web UI.

Thin wrapper: imports the `sca` package directly and exposes JSON
endpoints. Dataclasses are serialised with dataclasses.asdict().

Run:  .venv/bin/python -m web.server
  or  .venv/bin/uvicorn web.server:app --port 8000

Nothing here mutates the sca package — read-only consumption.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from sca import config
from sca.agent import analyze, assess_redemption, screen_token
from sca.corpus.sources import all_sources
from sca.observability import log_event
from sca.store import get_store
from sca.tools import get_onchain_supply
from web.auth import current_user, require_user
from web.rate_limit import enforce as rate_limit_enforce

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Stablecoin Compliance Agent", docs_url="/api/docs")


# ── optional authentication ───────────────────────────────────────────
# Auth is optional and gated on *persistence*, never on *use*. Anonymous
# callers reach every functional endpoint; an account only unlocks durable
# state (saved analysis history, curation votes, a profile). See web/auth.py.
def _uid(user: dict[str, Any] | None) -> str | None:
    """The user id to attribute persistence to — None when anonymous."""
    return user["user"]["id"] if user else None


@app.get("/api/config")
def public_config() -> dict[str, Any]:
    """Public Supabase client config — the SPA inits supabase-js from this.

    The anon key is the public client key; it is safe to expose. When
    Supabase is unconfigured the SPA stays in its fully-usable anonymous
    mode (``supabase`` is null and the sign-in control is hidden).
    """
    if not config.supabase_configured() or not config.SUPABASE_ANON_KEY:
        return {"supabase_url": None, "supabase_anon_key": None}
    return {
        "supabase_url": config.SUPABASE_URL,
        "supabase_anon_key": config.SUPABASE_ANON_KEY,
    }


@app.get("/api/me")
def me(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    """The current user + profile, or an anonymous marker."""
    if user is None:
        return {"authenticated": False, "user": None, "profile": None}
    return {
        "authenticated": True,
        "user": user["user"],
        "profile": user["profile"],
    }


@app.get("/api/history")
def history(
    user: dict[str, Any] | None = Depends(current_user),
) -> dict[str, Any]:
    """The signed-in user's past analyses, newest first.

    Anonymous callers get an empty list — their runs are transient. Filtered
    to the caller's own ``user_id`` (``list_analyses`` has no user filter, so
    we scope client-side over the recent rows).
    """
    uid = _uid(user)
    if uid is None:
        return {"analyses": [], "count": 0, "authenticated": False}
    try:
        rows = get_store().list_analyses(limit=200)
    except Exception as exc:  # noqa: BLE001 - never break on a store hiccup
        raise HTTPException(502, f"history read failed: {exc}") from exc
    mine = [r for r in rows if r.get("user_id") == uid]
    return {"analyses": mine, "count": len(mine), "authenticated": True}


# ── serialisation ─────────────────────────────────────────────────────
def _asdict(obj: Any) -> Any:
    """dataclass -> plain dict; falls back gracefully for other types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


# ── cached-result freshness ───────────────────────────────────────────
# A compute that ran within this window is treated as fresh: serve the
# stored result instantly, with no job and no staged-progress animation.
#
# Sized for "the background thread keeps us current": every 6h the canary
# re-resolves attestation URLs and the discovery thread closes gaps, so a
# 6h analysis-result cache is materially backed by fresh underlying data.
# Force-refresh remains the on-demand escape hatch (the SPA's "refresh"
# action always bypasses this), and the Compendium shows the user when
# each cached artefact was last computed.
_CACHE_FRESH_S = 6 * 60 * 60


def _parse_ts(raw: Any) -> datetime | None:
    """Parse a stored ``created_at`` as a UTC datetime, or None if unusable.

    Postgres ``timestamptz`` arrives as an ISO string; a naive value is
    assumed UTC. Anything missing or unparseable returns None so the caller
    treats the row as not-fresh and runs a real job (the safe default).
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fresh_cached(surface: str, symbol: str) -> dict[str, Any] | None:
    """Most recent completed analysis for a token, if computed < 6h ago.

    Returns the instant-serve payload (status done, result, cached flag,
    computed_at) or None when there is nothing fresh to serve. Shared across
    all callers — the lookup is intentionally not scoped by user. Never
    raises: a store hiccup just falls through to running a real job.
    """
    row = _latest_completed(surface, symbol)
    if row is None:
        return None
    ts = _parse_ts(row.get("created_at"))
    if ts is None:
        return None
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age < 0 or age > _CACHE_FRESH_S:
        return None
    return {
        "status": "done",
        "result": row["result"],
        "cached": True,
        "computed_at": row.get("created_at"),
    }


def _latest_completed(surface: str, symbol: str) -> dict[str, Any] | None:
    """Most recent completed analysis for (surface, symbol) regardless of
    age. Used by the GET /api/cached endpoint so the frontend can render
    yesterday's analysis instantly on page load while the freshness strip
    tells the user the result is stale and offers a one-click refresh."""
    try:
        rows = get_store().list_analyses(
            surface=surface, symbol=symbol, limit=1,
        )
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    row = rows[0]
    if row.get("status") != "done" or not row.get("result"):
        return None
    return row


# ── async analysis jobs ───────────────────────────────────────────────
# An analysis hits live RPCs + a PDF download + an LLM (~10-40s). We run
# it through a BOUNDED thread pool so a burst of users can't spawn 100
# concurrent jobs and exhaust LLM quota / file descriptors / RAM. Jobs
# auto-evict after `_JOB_TTL_S` so memory stays flat under sustained load.
#
# Defaults: 4 concurrent jobs, 1-hour TTL. Override via env
# `SCA_JOB_POOL` and `SCA_JOB_TTL_S`. With 4 workers each running ~30s LLM
# calls, throughput is ~8 jobs/minute — fine for current load and bounded
# enough to be reasoned about. Live cache (`_fresh_cached`) makes the
# common case (warm token) free, so concurrent compute is the rare path.
import os as _os
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

_JOB_POOL_SIZE = int(_os.environ.get("SCA_JOB_POOL", "4"))
_JOB_TTL_S = int(_os.environ.get("SCA_JOB_TTL_S", str(60 * 60)))
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_POOL = _ThreadPoolExecutor(
    max_workers=_JOB_POOL_SIZE, thread_name_prefix="dore-job",
)


def _evict_old_jobs() -> int:
    """Drop completed jobs older than the TTL. Cheap O(n); called whenever
    we enqueue a new job so eviction tracks volume, not wall time."""
    cutoff = time.time() - _JOB_TTL_S
    with _JOBS_LOCK:
        stale = [
            j for j, st in _JOBS.items()
            if st.get("started_at", 0) < cutoff
            and st.get("status") in {"done", "error"}
        ]
        for j in stale:
            del _JOBS[j]
    return len(stale)


def _submit_job(job_id: str, fn, *args, **kwargs) -> None:
    """Enqueue work on the bounded pool. Bounded queueing prevents the
    thundering-herd / quota-exhaustion class of bug."""
    _evict_old_jobs()
    _JOB_POOL.submit(fn, *args, **kwargs)

_STAGES = [
    "Resolving on-chain supply across deployments",
    "Locating + downloading the latest attestation",
    "Extracting reserves from the attestation PDF",
    "Running deterministic guardrail checks",
    "Retrieving the corpus reasoning frame",
    "Synthesising the cited compliance analysis",
]


def _run_job(
    job_id: str, symbol: str, refresh: bool = False,
    user_id: str | None = None, tier: str = "fast",
) -> None:
    started = time.time()
    try:
        # user_id attributes the persisted analysis row to the signed-in
        # user; None (anonymous) means the run is transient.
        # tier selects the LLM (fast / deep) — user opt-in for deeper
        # reasoning; default is fast for sub-2s LLM hops.
        from sca.llm import get_llm
        llm = get_llm(tier)
        result = analyze(symbol, refresh=refresh, user_id=user_id, llm=llm)
        payload = _asdict(result)
        with _JOBS_LOCK:
            _JOBS[job_id].update(
                status="done",
                result=payload,
                elapsed=round(time.time() - started, 1),
            )
    except Exception as exc:  # noqa: BLE001 - surface to client
        with _JOBS_LOCK:
            _JOBS[job_id].update(
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                trace=traceback.format_exc(),
                elapsed=round(time.time() - started, 1),
            )


@app.post("/api/analyze")
def start_analysis(
    body: dict[str, Any],
    user: dict[str, Any] | None = Depends(current_user),
    _rate: None = Depends(rate_limit_enforce),
) -> dict[str, Any]:
    """Kick off an analysis job. Returns a job id to poll.

    Pass ``refresh: true`` to bypass the analysis cache and force a full
    recompute (live RPCs + LLM). Omitted/false reuses the cached result,
    which is instant and free.

    Anonymous callers run the analysis fully — only persistence to history
    requires an account, so the run is attributed only when ``user`` is set.
    """
    raw = str(body.get("symbol", "")).strip()
    if not raw:
        raise HTTPException(400, "symbol is required")
    try:
        symbol = config.get_stablecoin(raw).symbol  # canonical case
    except ValueError:
        raise HTTPException(404, f"unknown stablecoin: {raw}")
    refresh = bool(body.get("refresh", False))
    tier = "deep" if str(body.get("tier", "")).lower() == "deep" else "fast"
    # A fresh, already-computed result is served instantly — no job, no
    # staged motions. An explicit refresh always bypasses this.
    # Cache lookup is shared across tiers (the heavy work is identical;
    # only narrative style differs). `refresh=true` is the escape hatch
    # when a user wants a deep-tier re-run.
    if not refresh:
        cached = _fresh_cached("attestation", symbol)
        if cached is not None:
            return cached
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "symbol": symbol,
            "status": "running",
            "started_at": time.time(),
        }
    _submit_job(job_id, _run_job, job_id, symbol, refresh, _uid(user), tier)
    return {"job_id": job_id, "symbol": symbol, "stages": _STAGES}


_CACHED_SURFACES = {"attestation", "sanctions", "redemption"}


@app.get("/api/cached/{surface}/{symbol}")
def cached_result(surface: str, symbol: str) -> dict[str, Any]:
    """Instant-render endpoint: return the most recent completed analysis
    for (surface, symbol) regardless of age. The freshness strip on the
    client decides whether to flag it as stale and prompt for refresh.

    Lets every view render its last state on initial page load before
    any RUN button is pressed — server restarts don't lose the picture
    because the lookup is Store-backed, not in-memory."""
    if surface not in _CACHED_SURFACES:
        raise HTTPException(404, f"unknown surface: {surface}")
    try:
        canonical = config.get_stablecoin(symbol.upper()).symbol
    except ValueError:
        raise HTTPException(404, f"unknown stablecoin: {symbol}")
    row = _latest_completed(surface, canonical)
    if row is None:
        return {"cached": False, "symbol": canonical, "surface": surface}
    ts = _parse_ts(row.get("created_at"))
    age_s = None
    if ts is not None:
        age_s = (datetime.now(timezone.utc) - ts).total_seconds()
    return {
        "cached": True,
        "status": "done",
        "result": row["result"],
        "computed_at": row.get("created_at"),
        "age_s": age_s,
        "stale": age_s is not None and age_s > _CACHE_FRESH_S,
        "symbol": canonical,
        "surface": surface,
    }


@app.get("/api/analyze/{job_id}")
def poll_analysis(job_id: str) -> dict[str, Any]:
    """Poll an analysis job. status: running | done | error."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        job = dict(job)
    job["age"] = round(time.time() - job.get("started_at", time.time()), 1)
    return job


# ── async sanctions / redemption jobs ─────────────────────────────────
# Both new compliance surfaces are slow for the same reasons analysis is:
# a live on-chain supply read, a curated-corpus retrieval, and an LLM
# synthesis pass — plus an OFAC SDN-list load for sanctions. We mirror
# the analyze() async-job pattern exactly so the UI can stage progress.
_SANCTIONS_STAGES = [
    "Resolving on-chain supply across deployments",
    "Loading the OFAC SDN sanctioned-address list",
    "Screening token deployment addresses against the list",
    "Running deterministic guardrail checks",
    "Retrieving the corpus reasoning frame",
    "Synthesising the cited sanctions screen",
]
_REDEMPTION_STAGES = [
    "Resolving on-chain supply across deployments",
    "Locating + downloading the latest attestation",
    "Classifying reserves into liquidity tiers",
    "Running deterministic guardrail checks",
    "Retrieving the corpus reasoning frame",
    "Synthesising the cited redemption assessment",
]


def _run_surface_job(
    job_id: str, symbol: str, fn: Any, user_id: str | None = None,
    tier: str = "fast",
) -> None:
    """Run a slow compliance-surface job (sanctions or redemption)."""
    from sca.llm import get_llm
    started = time.time()
    try:
        result = fn(symbol, user_id=user_id, llm=get_llm(tier))
        payload = _asdict(result)
        with _JOBS_LOCK:
            _JOBS[job_id].update(
                status="done",
                result=payload,
                elapsed=round(time.time() - started, 1),
            )
    except Exception as exc:  # noqa: BLE001 - surface to client
        with _JOBS_LOCK:
            _JOBS[job_id].update(
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                trace=traceback.format_exc(),
                elapsed=round(time.time() - started, 1),
            )


def _start_surface_job(
    body: dict[str, Any], fn: Any, stages: list[str], surface: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    raw = str(body.get("symbol", "")).strip()
    if not raw:
        raise HTTPException(400, "symbol is required")
    try:
        symbol = config.get_stablecoin(raw).symbol  # canonical case
    except ValueError:
        raise HTTPException(404, f"unknown stablecoin: {raw}")
    # A fresh, already-computed result is served instantly — no job, no
    # staged motions. An explicit refresh always bypasses this.
    if not bool(body.get("refresh", False)):
        cached = _fresh_cached(surface, symbol)
        if cached is not None:
            return cached
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "symbol": symbol,
            "status": "running",
            "started_at": time.time(),
        }
    tier = "deep" if str(body.get("tier", "")).lower() == "deep" else "fast"
    _submit_job(job_id, _run_surface_job, job_id, symbol, fn, user_id, tier)
    return {"job_id": job_id, "symbol": symbol, "stages": stages}


@app.post("/api/sanctions")
def start_sanctions(
    body: dict[str, Any],
    user: dict[str, Any] | None = Depends(current_user),
    _rate: None = Depends(rate_limit_enforce),
) -> dict[str, Any]:
    """Kick off an OFAC sanctions screen. Returns a job id to poll."""
    uid = user["user"]["id"] if user else None
    return _start_surface_job(
        body, screen_token, _SANCTIONS_STAGES, "sanctions", uid
    )


@app.get("/api/sanctions/{job_id}")
def poll_sanctions(job_id: str) -> dict[str, Any]:
    """Poll a sanctions-screen job. status: running | done | error."""
    return poll_analysis(job_id)


@app.post("/api/redemption")
def start_redemption(
    body: dict[str, Any],
    user: dict[str, Any] | None = Depends(current_user),
    _rate: None = Depends(rate_limit_enforce),
) -> dict[str, Any]:
    """Kick off a redemption-capacity assessment. Returns a job id."""
    uid = user["user"]["id"] if user else None
    return _start_surface_job(
        body, assess_redemption, _REDEMPTION_STAGES, "redemption", uid
    )


@app.get("/api/redemption/{job_id}")
def poll_redemption(job_id: str) -> dict[str, Any]:
    """Poll a redemption-assessment job. status: running | done | error."""
    return poll_analysis(job_id)


# ── registry / corpus / supply ────────────────────────────────────────
@app.get("/api/tokens")
def tokens() -> dict[str, Any]:
    """The stablecoin registry — every configured token + deployments."""
    out = []
    for coin in config.stablecoins().values():
        d = _asdict(coin)
        d["chain_count"] = len(coin.deployments)
        d["verified_count"] = sum(1 for x in coin.deployments if x.verified)
        out.append(d)
    return {"tokens": out, "count": len(out)}


@app.get("/api/sources")
def sources() -> dict[str, Any]:
    """The corpus source registry (opt-out model).

    Every source is `included` and citable by default; `included` is False
    only for a source a human has explicitly excluded. `verified` flags a
    source a human has explicitly reviewed — a badge, not a gate.

    Each source also carries `snapshot_status` and `snapshot_url` so the UI
    can render a health badge and swap the live link to an archived copy
    when the source is broken.
    """
    from sca import snapshots as _snap

    raw = all_sources()
    items = [_asdict(s) for s in raw]
    for s, src in zip(items, raw):
        s["included"] = src.included
        s["verified"] = src.verified
        # Source health — derived from the snapshot store.
        meta = _snap.load_meta(f"corpus::{src.id}")
        if meta is None:
            s["snapshot_status"] = "unknown"  # never canaried
            s["snapshot_url"] = ""
            s["snapshot_age_days"] = None
        else:
            s["snapshot_status"] = (
                "broken" if meta.status_code >= 400 else "live"
            )
            s["snapshot_url"] = f"/api/snapshot/corpus::{src.id}"
            s["snapshot_age_days"] = _snap.staleness_days(meta)
    included = sum(1 for s in items if s["included"])
    verified = sum(1 for s in items if s["verified"])
    return {
        "sources": items,
        "count": len(items),
        "included": included,
        "verified": verified,
    }


@app.get("/api/snapshot/{snapshot_id}")
def get_snapshot(snapshot_id: str):
    """Serve the last-known-good body for a snapshot id.

    Used by the UI when the live source URL is broken — every "view
    source ↗" link transparently falls back to "view archived copy". The
    archived bytes come from the canary's most recent successful fetch.
    """
    from fastapi import HTTPException
    from fastapi.responses import Response

    from sca import snapshots as _snap

    loaded = _snap.load_body(snapshot_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="no snapshot for that id")
    body, meta = loaded
    return Response(
        content=body, media_type=meta.content_type,
        headers={
            "X-Snapshot-Fetched-At": meta.fetched_at,
            "X-Snapshot-Sha256": meta.sha256,
            "X-Snapshot-Source-Url": meta.url,
        },
    )


@app.post("/api/sources/{source_id}/vote")
def vote_source(
    source_id: str,
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Record a human curation decision on a corpus source (opt-out model).

    Mirrors ``sca curate``: records the decision, clears the lru_cached
    source loader, and — on an ``included``/``verified`` decision — ingests
    any staged file so the source contributes citable text.

    decision: ``excluded`` (opt the source out), ``included`` (reverse an
    exclusion), or ``verified`` (mark it human-reviewed; stays included).

    Curation is a save-type action: it requires an account. An anonymous
    caller gets a 401 from ``require_user`` (the SPA turns this into a
    friendly "sign in to save" prompt). The vote is attributed to the user.
    """
    decision = str(body.get("decision", "")).strip().lower()
    if decision not in {"excluded", "included", "verified"}:
        raise HTTPException(
            400, "decision must be 'excluded', 'included' or 'verified'"
        )

    known = {s.id for s in all_sources()}
    if source_id not in known:
        raise HTTPException(404, f"unknown source: {source_id}")

    # Record straight through the store so the decision is attributed to the
    # signed-in user (votes.record_source_decision takes no user_id).
    get_store().record_source_decision(
        source_id, decision, user_id=user["user"]["id"]
    )
    all_sources.cache_clear()  # loader is lru_cached — REQUIRED or status won't update

    ingested = False
    ingest_error = None
    if decision in {"included", "verified"}:
        try:
            from sca.corpus.ingest import ingest_source

            staged = config.CORPUS_DIR / "staging" / f"{source_id}.md"
            if staged.exists():
                ingest_source(source_id, staged.read_text())
                ingested = True
        except Exception as exc:  # noqa: BLE001 - never break the vote
            ingest_error = f"{type(exc).__name__}: {exc}"

    updated = None
    for s in all_sources():
        if s.id == source_id:
            updated = _asdict(s)
            updated["included"] = s.included
            updated["verified"] = s.verified
            break

    return {
        "ok": True,
        "source": updated,
        "ingested": ingested,
        "ingest_error": ingest_error,
    }


@app.get("/api/supply/{symbol}")
def supply(symbol: str) -> dict[str, Any]:
    """Live on-chain supply only — fast, no LLM, no attestation.

    Intentionally NOT rate-limited at the HTTP layer: this endpoint is the
    target of the F1 monitor's live poll loop (one read per token per
    cycle) AND every analyze/sanctions/redemption job triggers an internal
    supply read. The upstream RPC reads are already bounded by the
    `_onchain._cache` (60s TTL) — re-reads inside that window are
    in-process memory hits with no network cost. A per-IP HTTP bucket
    here just makes the monitor flap "RPC-FAIL" for the user when their
    own analyses race against the poll loop on the same IP.
    """
    try:
        # Canonicalize via the case-insensitive lookup so USDe / USDf / crvUSD
        # resolve from URL params without forcing the caller to know casing.
        canon = config.get_stablecoin(symbol.strip()).symbol
    except ValueError:
        raise HTTPException(404, f"unknown stablecoin: {symbol}")
    try:
        result = get_onchain_supply(canon, allow_unverified=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"supply read failed: {exc}") from exc
    # Persist a snapshot row at most once per minute per symbol so the
    # movement simulator has a time series to forecast from. The
    # F1 monitor polls every few seconds; without this gate we'd flood
    # the store. Cheap dict check, no lock — duplicate writes within
    # the same second from concurrent requests are tolerable.
    _record_movement_snapshot(canon, result)
    return _asdict(result)


# Per-symbol last-snapshot-write timestamps. In-memory is sufficient:
# at restart we lose the gate but the worst case is one extra snapshot
# row per symbol, which the store cheerfully accepts.
_SNAPSHOT_WRITE_GATE: dict[str, float] = {}
_SNAPSHOT_WRITE_INTERVAL_S = 60.0


def _record_movement_snapshot(symbol: str, result: Any) -> None:
    """Write the supply reading through to the store if it's been at
    least _SNAPSHOT_WRITE_INTERVAL_S since the last write for this
    symbol. Best-effort: a store failure logs but never raises so the
    /api/supply path stays fast and resilient."""
    now = time.time()
    last = _SNAPSHOT_WRITE_GATE.get(symbol, 0.0)
    if now - last < _SNAPSHOT_WRITE_INTERVAL_S:
        return
    _SNAPSHOT_WRITE_GATE[symbol] = now
    try:
        per_chain = getattr(result, "per_chain", None)
        # per_chain arrives as list[ChainSupply] from onchain_supply. Each
        # element is a dataclass that json.dumps cannot serialise — the
        # SupabaseStore jsonb write fails with a TypeError otherwise.
        # Deep-convert: dataclass → dict on the top-level container AND
        # on every element of a list.
        if dataclasses.is_dataclass(per_chain) and not isinstance(per_chain, type):
            per_chain = dataclasses.asdict(per_chain)
        elif isinstance(per_chain, list):
            per_chain = [
                dataclasses.asdict(item)
                if dataclasses.is_dataclass(item) and not isinstance(item, type)
                else item
                for item in per_chain
            ]
        get_store().save_snapshot(
            symbol=symbol,
            total_supply=float(getattr(result, "total_supply", 0.0) or 0.0),
            native_supply=getattr(result, "native_supply", None),
            bridged_supply=getattr(result, "bridged_supply", None),
            per_chain=per_chain,
            warnings=list(getattr(result, "warnings", []) or []),
        )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "movement.snapshot_persist_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
            error_message=str(exc)[:200],
        )


@app.post("/api/addresses/decision")
def address_decision(
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Record a human contract-address verification decision.

    A save-type curation action — like a source vote, it requires an
    account and returns 401 for an anonymous caller. The decision is
    attributed to the signed-in user and persisted through the store.
    """
    raw = str(body.get("symbol", "")).strip()
    chain = str(body.get("chain", "")).strip()
    decision = str(body.get("decision", "")).strip().lower()
    try:
        coin = config.get_stablecoin(raw)  # canonical case
    except ValueError:
        raise HTTPException(404, f"unknown stablecoin: {raw}")
    symbol = coin.symbol
    if decision not in {"verified", "rejected"}:
        raise HTTPException(400, "decision must be 'verified' or 'rejected'")
    match = next((d for d in coin.deployments if d.chain == chain), None)
    if match is None:
        raise HTTPException(404, f"unknown deployment: {symbol} on {chain}")
    get_store().record_address_decision(
        symbol, chain, match.contract, decision, user_id=user["user"]["id"]
    )
    return {"ok": True, "symbol": symbol, "chain": chain, "decision": decision}


# ── eval suite — cached read + on-demand run ─────────────────────────
# GET is a cheap read (returns last run + age) so the view mounts
# instantly; POST is the slow run (one analyze() per case). Mirrors
# the analyze / sanctions / redemption pattern so every surface has
# the same data-freshness + RE-RUN shape.
_EVALS_CACHE: dict[str, Any] = {"payload": None, "computed_at": None,
                                 "elapsed_s": None}
_EVALS_CACHE_LOCK = threading.Lock()


def _evals_payload(results: list[Any]) -> dict[str, Any]:
    cases = []
    for r in results:
        cases.append({
            "case_id": r.case_id,
            "symbol": r.symbol,
            "passed": r.passed,
            "points": [_asdict(p) for p in r.points],
        })
    passed = sum(1 for c in cases if c["passed"])
    return {"cases": cases, "count": len(cases), "passed": passed}


@app.get("/api/evals")
def evals_cached() -> dict[str, Any]:
    """Return the last completed eval suite + when it was computed.
    Cheap: no LLM, no live RPCs. The Evals view mounts on this so the
    page renders the last picture instantly; explicit RUN/RE-RUN goes
    through POST."""
    with _EVALS_CACHE_LOCK:
        if _EVALS_CACHE["payload"] is None:
            return {"cached": False}
        ts = _EVALS_CACHE["computed_at"]
        age_s = None
        if ts:
            parsed = _parse_ts(ts)
            if parsed is not None:
                age_s = (datetime.now(timezone.utc) - parsed).total_seconds()
        return {
            "cached": True,
            "computed_at": ts,
            "elapsed_s": _EVALS_CACHE["elapsed_s"],
            "age_s": age_s,
            "stale": age_s is not None and age_s > _CACHE_FRESH_S,
            **_EVALS_CACHE["payload"],
        }


@app.post("/api/evals")
def evals_run(
    _rate: None = Depends(rate_limit_enforce),
) -> dict[str, Any]:
    """Run the eval harness. Slow (one analyze() per case) and rate-
    limited so an uncapped caller can't burn LLM quota in minutes.
    Caches the result for subsequent GETs."""
    from sca.evals import run_evals

    started = time.time()
    try:
        results = run_evals()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"eval run failed: {exc}") from exc
    payload = _evals_payload(results)
    elapsed = round(time.time() - started, 1)
    now = datetime.now(timezone.utc).isoformat()
    with _EVALS_CACHE_LOCK:
        _EVALS_CACHE["payload"] = payload
        _EVALS_CACHE["computed_at"] = now
        _EVALS_CACHE["elapsed_s"] = elapsed
    return {
        "cached": False,
        "computed_at": now,
        "elapsed_s": elapsed,
        "age_s": 0,
        "stale": False,
        **payload,
    }


# ── Market overview — cross-token editorial aggregation ──────────────
# Composes the live state of every tracked stablecoin into a single
# story: supply concentration, backing-model mix, verification health,
# drift between attestation and on-chain, per-chain map, recent news,
# and an AI Market Brief tying it together. Cached for 30min behind a
# threading lock; refresh=true forces a recompute.
_MARKET_CACHE: dict[str, Any] = {
    "payload": None, "computed_at": None, "elapsed_s": None,
}
_MARKET_CACHE_LOCK = threading.Lock()
_MARKET_FRESH_S = 30 * 60   # 30 minutes


def _backing_model_label(model: str) -> str:
    return {
        "fiat_reserves": "Fiat reserves",
        "crypto_collateral": "Crypto collateral",
        "synthetic_delta_neutral": "Synthetic delta-neutral",
        "algorithmic": "Algorithmic",
        "new_or_unverified": "New / unverified",
    }.get(model, model)


def _compute_market_overview() -> dict[str, Any]:
    """Aggregate every tracked stablecoin into one editorial picture.
    Cheap: reads from supply_history.json (kept warm by the live
    monitor thread) and the attestation cache. No LLM calls except the
    final Market Brief composition.
    """
    from collections import defaultdict
    from sca import config as _cfg
    from sca import supply_history as _hist
    from sca.tools import attestation_fetch as _af

    started = time.time()
    tokens = _cfg.stablecoins()
    history = _hist._load()  # noqa: SLF001 - intentional snapshot read
    cache = _af._load_cache()
    overrides = {
        r["symbol"]: r
        for r in get_store().list_attestation_url_overrides()
    }

    # Aggregate supply per (symbol, chain) from the warmed history.
    sym_supply: dict[str, float] = defaultdict(float)
    sym_chains: dict[str, set[str]] = defaultdict(set)
    chain_supply: dict[str, float] = defaultdict(float)
    chain_tokens: dict[str, set[str]] = defaultdict(set)
    for key, val in history.items():
        if ":" not in key or not isinstance(val, (int, float)):
            continue
        sym, chain = key.split(":", 1)
        if sym not in tokens:
            continue
        sym_supply[sym] += val
        sym_chains[sym].add(chain)
        chain_supply[chain] += val
        chain_tokens[chain].add(sym)

    total_supply = sum(sym_supply.values())

    # Per-issuer roll-up.
    by_issuer: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "total_supply": 0.0, "tokens": []})
    for sym, coin in tokens.items():
        bucket = by_issuer[coin.issuer]
        bucket["count"] += 1
        bucket["total_supply"] += sym_supply.get(sym, 0.0)
        bucket["tokens"].append(sym)
    issuer_rows = []
    for issuer, b in by_issuer.items():
        share = (b["total_supply"] / total_supply * 100) if total_supply else 0
        issuer_rows.append({
            "issuer": issuer, "count": b["count"],
            "total_supply": b["total_supply"],
            "share_pct": round(share, 2),
            "tokens": sorted(b["tokens"]),
        })
    issuer_rows.sort(key=lambda r: -r["total_supply"])

    # Per-backing-model roll-up.
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "total_supply": 0.0, "tokens": []})
    for sym, coin in tokens.items():
        bucket = by_model[coin.backing_model]
        bucket["count"] += 1
        bucket["total_supply"] += sym_supply.get(sym, 0.0)
        bucket["tokens"].append(sym)
    model_rows = []
    for model, b in by_model.items():
        share = (b["total_supply"] / total_supply * 100) if total_supply else 0
        model_rows.append({
            "model": model, "label": _backing_model_label(model),
            "count": b["count"],
            "total_supply": b["total_supply"],
            "share_pct": round(share, 2),
            "tokens": sorted(b["tokens"]),
        })
    model_rows.sort(key=lambda r: -r["total_supply"])

    # Per-chain roll-up.
    chain_rows = []
    for chain, supply in chain_supply.items():
        share = (supply / total_supply * 100) if total_supply else 0
        chain_rows.append({
            "chain": chain,
            "total_supply": supply,
            "share_pct": round(share, 2),
            "token_count": len(chain_tokens[chain]),
            "tokens": sorted(chain_tokens[chain]),
        })
    chain_rows.sort(key=lambda r: -r["total_supply"])

    # Concentration: top-N share + Herfindahl (0-1, higher = more concentrated)
    issuer_shares = [r["share_pct"] / 100 for r in issuer_rows]
    top3 = round(sum(issuer_shares[:3]) * 100, 2)
    top5 = round(sum(issuer_shares[:5]) * 100, 2)
    hhi = round(sum(s * s for s in issuer_shares), 4)

    # Verification health: walk the attestation overrides + cache to
    # classify every fiat token as fresh / stale / unresolved.
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    fresh, stale, by_design, blocked = [], [], [], []
    for sym, coin in tokens.items():
        if coin.backing_model != "fiat_reserves":
            by_design.append({"symbol": sym, "model": coin.backing_model,
                              "label": _backing_model_label(coin.backing_model)})
            continue
        ovr = overrides.get(sym, {}) or {}
        cached = cache.get(sym, {}) or {}
        url = ovr.get("url") or cached.get("url")
        if not url:
            blocked.append({"symbol": sym, "issuer": coin.issuer})
            continue
        # Resolved — call it fresh unless we know the cached snapshot is old.
        # The 6h canary keeps cache_resolved_at <= 6h old, so we use the
        # attestation's own age via the cache resolved date as a proxy.
        resolved_at = cached.get("resolved_at") or ovr.get("set_at") or ""
        try:
            r_dt = _parse_ts(resolved_at)
            age_h = (now - r_dt).total_seconds() / 3600 if r_dt else None
        except Exception:  # noqa: BLE001
            age_h = None
        entry = {
            "symbol": sym, "issuer": coin.issuer,
            "url": url, "resolved_at": resolved_at,
        }
        if age_h is not None and age_h > 24:
            stale.append(entry)
        else:
            fresh.append(entry)

    # Drift: tokens where (current_supply vs the attestation's tokens_outstanding)
    # has moved most. Pull the latest verified_facts reserves row per symbol.
    drift_rows = []
    try:
        from sca.store import get_store as _gs
        store = _gs()
        if hasattr(store, "list_verified_facts"):
            facts = store.list_verified_facts(limit=200) or []
        else:
            facts = []
    except Exception:  # noqa: BLE001
        facts = []
    latest_reserves = {}
    for f in facts:
        if f.get("claim_type") != "reserves":
            continue
        sym = (f.get("subject") or "").split(":")[0]
        if not sym or sym in latest_reserves:
            continue
        latest_reserves[sym] = f
    for sym, f in latest_reserves.items():
        if sym not in tokens:
            continue
        v = f.get("value") or {}
        attested = v.get("tokens_outstanding") or v.get("attested_tokens")
        as_of = v.get("as_of_date") or v.get("as_of") or ""
        current = sym_supply.get(sym, 0.0)
        if not attested or attested <= 0:
            continue
        drift = (current - float(attested)) / float(attested)
        as_of_dt = _parse_ts(as_of) if as_of else None
        stale_days = ((now - as_of_dt).days
                      if as_of_dt else None)
        drift_rows.append({
            "symbol": sym,
            "as_of": as_of,
            "attested_tokens": float(attested),
            "current_supply": current,
            "drift_pct": round(drift * 100, 2),
            "staleness_days": stale_days,
        })
    drift_rows.sort(key=lambda r: -abs(r["drift_pct"]))

    # Recent signals: pull the top corpus events from the discovery feed
    # that have landed in the last week, capped at 6.
    from sca.observability import recent_events
    recent = recent_events(limit=200) or []
    SIGNAL_KINDS = {
        "discovery.published", "discovery.new", "attestation.published",
        "snapshot.flip",
    }
    signals = []
    for e in reversed(recent):
        if e.get("kind") not in SIGNAL_KINDS:
            continue
        signals.append({
            "kind": e.get("kind"),
            "title": (e.get("title") or e.get("name") or
                      e.get("symbol") or ""),
            "ts": e.get("ts"),
            "url": e.get("url", ""),
        })
        if len(signals) >= 6:
            break

    payload = {
        "summary": {
            "total_supply": total_supply,
            "token_count": len(tokens),
            "issuer_count": len(issuer_rows),
            "chain_count": len(chain_rows),
            "verified_count": len(fresh),
            "stale_count": len(stale),
            "by_design_count": len(by_design),
            "blocked_count": len(blocked),
        },
        "by_issuer": issuer_rows,
        "by_backing_model": model_rows,
        "by_chain": chain_rows,
        "concentration": {
            "top3_share_pct": top3,
            "top5_share_pct": top5,
            "hhi": hhi,
        },
        "verification_health": {
            "fresh": fresh, "stale": stale,
            "by_design": by_design, "blocked": blocked,
        },
        "drift_leaderboard": drift_rows[:6],
        "recent_signals": signals,
    }

    # Compose the editorial AI Market Brief on top of the picture.
    # Best-effort: a missing LLM key returns None, the view omits the
    # hero panel.
    facts = _market_facts_block(payload)
    try:
        from sca.brief import generate_market_brief
        from sca.corpus.retrieve import retrieve
        passages = retrieve(
            "stablecoin market regulation attestation supply concentration",
            k=8,
        )
        brief = generate_market_brief(
            market_facts=facts, news_candidates=passages,
        )
    except Exception as exc:  # noqa: BLE001
        from sca.observability import log_event
        log_event("market.brief.failed", level="warn",
                  error_class=type(exc).__name__,
                  error_message=str(exc)[:200])
        brief = None
    payload["brief"] = _asdict(brief) if brief is not None else None

    # Aggregate freshness for the footer: when were the underlying
    # attestations extracted (earliest, latest) and when was the
    # validator last run against any issuer? Read straight from the
    # cache the resolver just touched.
    extracted_isos = [
        (cache.get(sym, {}) or {}).get("resolved_at", "")
        for sym in tokens
    ]
    validated_isos = [
        (cache.get(sym, {}) or {}).get("validated_at", "")
        for sym in tokens
    ]
    extracted_clean = sorted([x for x in extracted_isos if x])
    validated_clean = sorted([x for x in validated_isos if x])
    payload["freshness"] = {
        "earliest_extracted_at": extracted_clean[0] if extracted_clean else "",
        "latest_extracted_at": extracted_clean[-1] if extracted_clean else "",
        "latest_validated_at":
            validated_clean[-1] if validated_clean else "",
    }

    payload["computed_at"] = datetime.now(timezone.utc).isoformat()
    payload["elapsed_s"] = round(time.time() - started, 1)
    return payload


def _market_facts_block(p: dict[str, Any]) -> str:
    """A compact, prose-friendly summary of the market state for the
    LLM. Numbers verbatim from the payload; the model invents nothing."""
    s = p["summary"]
    issuers = p["by_issuer"][:5]
    models = p["by_backing_model"]
    chains = p["by_chain"][:5]
    drifts = p["drift_leaderboard"][:4]
    conc = p["concentration"]

    def usd(n: float) -> str:
        if n >= 1e9:
            return f"${n / 1e9:.2f}B"
        if n >= 1e6:
            return f"${n / 1e6:.2f}M"
        return f"${n:.0f}"

    lines = [
        f"Total native stablecoin supply tracked: {usd(s['total_supply'])} "
        f"across {s['token_count']} tokens, {s['issuer_count']} issuers, "
        f"{s['chain_count']} chains.",
        f"Concentration: top 3 issuers control {conc['top3_share_pct']}%, "
        f"top 5 control {conc['top5_share_pct']}%, HHI = {conc['hhi']}.",
        "",
        "Top issuers by supply:",
    ]
    for r in issuers:
        lines.append(
            f"  - {r['issuer']}: {usd(r['total_supply'])} "
            f"({r['share_pct']}%, {r['count']} token(s): "
            f"{', '.join(r['tokens'])})"
        )
    lines.append("")
    lines.append("Backing-model mix:")
    for r in models:
        lines.append(
            f"  - {r['label']}: {usd(r['total_supply'])} "
            f"({r['share_pct']}%, {r['count']} token(s))"
        )
    lines.append("")
    # Reframed for the LLM: combine fresh + stale into a single
    # "attested" count (both have an extracted, citable attestation;
    # the canary's URL-pointer freshness is irrelevant to product
    # framing). Every token explicitly named in each bucket so the
    # LLM cannot guess membership (a prior version hallucinated USDT
    # into the source-only bucket when only counts were provided).
    vh = p["verification_health"]
    attested_syms = sorted(
        [x["symbol"] for x in (vh.get("fresh") or [])]
        + [x["symbol"] for x in (vh.get("stale") or [])])
    source_only_syms = sorted(
        x["symbol"] for x in (vh.get("blocked") or []))
    onchain_syms = sorted(
        x["symbol"] for x in (vh.get("by_design") or []))
    fiat_total = len(attested_syms) + len(source_only_syms)
    lines.append(
        f"Coverage across {fiat_total} fiat-backed tokens. Attested "
        f"({len(attested_syms)} tokens, issuer report extracted and "
        f"citable): {', '.join(attested_syms) if attested_syms else 'none'}. "
        f"Source-only ({len(source_only_syms)} tokens, issuer publishes "
        f"via a JavaScript-rendered transparency page that we summarise "
        f"in an AI Context card with the live issuer link): "
        f"{', '.join(source_only_syms) if source_only_syms else 'none'}. "
        f"On-chain ({len(onchain_syms)} non-fiat tokens with no CPA "
        f"report by design, backing visible on-chain): "
        f"{', '.join(onchain_syms) if onchain_syms else 'none'}."
    )
    if drifts:
        lines.append("")
        lines.append("Largest drifts between on-chain supply and last "
                     "attestation:")
        for d in drifts:
            sign = "+" if d["drift_pct"] >= 0 else ""
            stale = (f", attestation {d['staleness_days']}d old"
                     if d.get("staleness_days") is not None else "")
            lines.append(
                f"  - {d['symbol']}: supply has moved {sign}{d['drift_pct']}% "
                f"vs {d['as_of']} attestation{stale}"
            )
    lines.append("")
    lines.append("Top chains by stablecoin supply:")
    for r in chains:
        lines.append(
            f"  - {r['chain']}: {usd(r['total_supply'])} "
            f"({r['share_pct']}%, {r['token_count']} tokens)"
        )
    return "\n".join(lines)


@app.get("/api/trust")
def trust_score_all() -> dict[str, Any]:
    """Bulk Trust Scores for every tracked token. Powers the
    MARKET Trust Leaderboard — the cross-token executive summary."""
    from sca.config import stablecoins
    from sca.trust_score import compute_trust_score, tier_color
    rows: list[dict[str, Any]] = []
    for sym, _coin in stablecoins().items():
        try:
            score = compute_trust_score(sym)
            row = score.as_dict()
            row["headline_color"] = tier_color(score.score)
            rows.append(row)
        except Exception as exc:  # noqa: BLE001
            log_event(
                "trust_score.row_failed", level="warn",
                symbol=sym, error_class=type(exc).__name__,
            )
    return {"rows": rows, "count": len(rows)}


@app.get("/api/trust/{symbol}")
def trust_score_endpoint(symbol: str) -> dict[str, Any]:
    """Trust Score — Doré's institutional differentiator.

    Composite 0-100 score across ten dimensions (Reserve Quality, Peg
    Stability, Attestation Freshness, Redemption Capacity, Sanctions
    Exposure, Auditor Credibility, Multi-source Consensus, Regulatory
    Standing, Custodian Concentration, Implementation Risk). Each
    dimension cites its source and trust-tier; the verdict is plain
    English ("Eligible for Tier-1 corporate treasury IPS", etc.).

    Designed for the CFO / risk officer / regulator buyer — not a
    rating-agency letter, a treasury-policy verdict.
    """
    from sca.trust_score import compute_trust_score, tier_color
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, "symbol required")
    score = compute_trust_score(sym)
    out = score.as_dict()
    out["headline_color"] = tier_color(score.score)
    return out


@app.get("/api/market")
def market_overview(refresh: bool = False) -> dict[str, Any]:
    """Cross-token market overview + AI Market Brief. Cached for 30min."""
    now_dt = datetime.now(timezone.utc)
    with _MARKET_CACHE_LOCK:
        cached = _MARKET_CACHE.get("payload")
        ts = _MARKET_CACHE.get("computed_at")
    age_s = None
    if cached is not None and ts:
        parsed = _parse_ts(ts)
        if parsed is not None:
            age_s = (now_dt - parsed).total_seconds()
    if (
        cached is not None
        and not refresh
        and age_s is not None
        and age_s < _MARKET_FRESH_S
    ):
        return {
            "cached": True, "computed_at": ts, "age_s": age_s,
            "stale": False, **cached,
        }
    # Recompute. Hold the lock — single-flight; concurrent callers
    # serialize behind the same in-flight aggregation.
    with _MARKET_CACHE_LOCK:
        # Double-check after acquiring (someone else may have just refilled).
        ts2 = _MARKET_CACHE.get("computed_at")
        if not refresh and ts2 and ts2 != ts:
            cached = _MARKET_CACHE["payload"]
            return {
                "cached": True, "computed_at": ts2,
                "age_s": (now_dt - _parse_ts(ts2)).total_seconds(),
                "stale": False, **cached,
            }
        payload = _compute_market_overview()
        _MARKET_CACHE["payload"] = payload
        _MARKET_CACHE["computed_at"] = payload["computed_at"]
        _MARKET_CACHE["elapsed_s"] = payload["elapsed_s"]
    return {
        "cached": False, "computed_at": payload["computed_at"],
        "age_s": 0, "stale": False, **payload,
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    import os

    return {
        "ok": True,
        "tokens": len(config.stablecoins()),
        "llm_configured": bool(os.environ.get("LLM_API_KEY")),
    }


# ── compendium ────────────────────────────────────────────────────────
# The Data Compendium page reads from this endpoint to render a live
# audit-of-our-own-freshness: attestation URL provenance, per-source
# snapshot health, supply-history recency, web-discovery activity, and a
# rolling log of background-thread events. The contract is: every figure
# here ties back to durable state (the store + the ring-buffer sink) —
# nothing computed, nothing inferred.
@app.get("/api/compendium")
def compendium() -> dict[str, Any]:
    """Aggregate live freshness state for the Compendium page."""
    from sca import config as _cfg
    from sca import snapshots as _snap
    from sca import supply_history as _hist
    from sca import web_discovery as _wd
    from sca.observability import recent_events
    from sca.tools import attestation_fetch as _af

    # Attestation URL provenance, per token. Combines store overrides +
    # local cache so the UI sees whichever path is currently serving.
    store = get_store()
    overrides = {r["symbol"]: r for r in store.list_attestation_url_overrides()}
    cache = _af._load_cache()
    attestations: list[dict[str, Any]] = []
    for coin in _cfg.stablecoins().values():
        sym = coin.symbol
        ovr = overrides.get(sym)
        cached = cache.get(sym, {})
        attestations.append({
            "symbol": sym,
            "issuer": coin.issuer,
            "yaml_seed": coin.latest_attestation_url or "",
            "override_url": (ovr or {}).get("url", ""),
            "override_via": (ovr or {}).get("via", ""),
            "override_set_at": (ovr or {}).get("set_at", ""),
            "cache_url": cached.get("url", ""),
            "cache_via": cached.get("via", ""),
            "cache_resolved_at": cached.get("resolved_at", ""),
            # When the cache was last re-checked against the issuer's
            # published reports (Brave probe + date comparison). Distinct
            # from resolved_at, which only moves on a fresh discovery.
            "cache_validated_at": cached.get("validated_at", ""),
        })

    # Source-snapshot health, newest first.
    sources_health: list[dict[str, Any]] = []
    for src in all_sources():
        meta = _snap.load_meta(f"corpus::{src.id}")
        if meta is None:
            sources_health.append({
                "id": src.id, "title": src.title,
                "status": "unknown", "fetched_at": "",
                "age_days": None, "url": "",
            })
            continue
        sources_health.append({
            "id": src.id,
            "title": src.title,
            "status": "broken" if meta.status_code >= 400 else "live",
            "status_code": meta.status_code,
            "fetched_at": meta.fetched_at,
            # Last time we re-checked the source with conditional HTTP
            # (304 or sha256 match) — distinct from fetched_at, which
            # only bumps on a body change.
            "validated_at": meta.validated_at or meta.fetched_at,
            "age_days": _snap.staleness_days(meta),
            "url": meta.url,
            "sha256": meta.sha256,
        })

    # Supply history — last reading per (symbol, chain). Read straight
    # from the JSON state for the Compendium snapshot; the module's
    # _load() is a deliberate private helper, so we re-read the file we
    # know it writes. Falsy = empty / not-yet-written.
    history_snapshot = _hist._load()  # noqa: SLF001 - intentional read-through

    # Web-discovery activity — most recent attestation discoveries.
    discoveries = _wd.recent_discoveries(limit=30)

    # Rolling log — last N background-thread events. Filter to the kinds
    # the Compendium cares about so the stream is readable.
    INTERESTING = {
        # Web discovery
        "web_discovery.search.start", "web_discovery.search.results",
        "web_discovery.hit", "web_discovery.no_pdf",
        "web_discovery.disabled", "web_discovery.backend_error",
        "web_discovery.cache_hit",
        "web_discovery.combo.brave_hit", "web_discovery.combo.ddg_fallback",
        "web_discovery.second_hop_failed",
        # Issuer status Q&A
        "issuer_status.refreshed", "issuer_status.failed",
        # Canary
        "health.source.flipped", "health.sweep.done", "health.sweep.failed",
        "canary.recovery.start", "canary.recovery.hit",
        "canary.recovery.no_match", "canary.replacement.proposed",
        # Address auto-verify (background)
        "address.auto_verify.sweep_done", "address.auto_verify.failed",
        # Attestation gap-sweep
        "attestation.gap_sweep.ok", "attestation.gap_sweep.unresolved",
        "attestation.gap_sweep.done",
        # Discovery
        "discovery.brave.query", "discovery.brave.sweep_done",
        "discovery.sweep",
        # SDN
        "sdn.fetch.ok", "sdn.fetch.failed",
        # On-chain anomalies
        "supply.jump", "rpc.consensus.degraded",
        # Persistence anomalies
        "attestation.override_persist_failed",
        "verified_facts.write_failed", "verified_facts.table_missing",
    }
    events = [
        e for e in recent_events(limit=500)
        if e.get("kind", "") in INTERESTING
        or e.get("level") in ("warn", "error")
    ][:120]

    # Verified facts (audit trail) — most recent rows so the UI shows
    # the system actually IS recording the immutable history it claims.
    try:
        facts = store.list_verified_facts(limit=40)
    except Exception:  # noqa: BLE001 - never break the compendium
        facts = []

    # System freshness — one row per cache layer, exposing both the
    # last fetch time (when bytes were written) and the last validation
    # time (when a truth source confirmed they're still current). The
    # gap between them is the "have we asked lately?" signal Phase 2
    # built; this panel makes it visible.
    system_freshness = _compute_system_freshness(
        attestations, sources_health,
    )

    return {
        "attestations": attestations,
        "sources_health": sources_health,
        "system_freshness": system_freshness,
        "supply_history": history_snapshot,
        "discoveries": discoveries,
        "events": events,
        "verified_facts": facts,
        "discovery_provider": os.environ.get(
            "SCA_WEB_SEARCH_PROVIDER", "",
        ).strip().lower() or None,
    }


def _compute_system_freshness(
    attestations: list[dict[str, Any]],
    sources_health: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per-cache-layer freshness rollup. Each row carries the most
    recent fetched_at and validated_at across that layer plus the size
    of the layer. Read by the Compendium "System freshness" panel."""
    from sca.tools import paxos_resolver as _paxos
    from sca import config as _cfg

    def _max_iso(values: list[str]) -> str:
        clean = [v for v in values if v]
        return max(clean) if clean else ""

    rows: list[dict[str, Any]] = []

    # 1. Attestation cache (Brave-discovered issuer reports).
    att_fetched = [a.get("cache_resolved_at", "") for a in attestations]
    att_validated = [a.get("cache_validated_at", "") for a in attestations]
    rows.append({
        "layer": "attestation_cache",
        "label": "Attestation cache",
        "description": "Cached issuer attestation URLs (Brave + locator).",
        "count": sum(1 for a in attestations if a.get("cache_url")),
        "latest_fetched_at": _max_iso(att_fetched),
        "latest_validated_at": _max_iso(att_validated),
        "validator": "Brave search + dated-URL comparison",
        "cooldown_hours": 12,
    })

    # 2. Paxos resolver cache (next-publication-month probe).
    try:
        paxos_cache = _paxos._load_cache()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        paxos_cache = {}
    paxos_fetched = [
        (e or {}).get("resolved_at", "") for e in paxos_cache.values()
    ]
    paxos_validated = [
        (e or {}).get("validated_at", "") for e in paxos_cache.values()
    ]
    rows.append({
        "layer": "paxos_resolver",
        "label": "Paxos resolver cache",
        "description": "Next-month URL probe for Paxos-issued tokens.",
        "count": len(paxos_cache),
        "latest_fetched_at": _max_iso(paxos_fetched),
        "latest_validated_at": _max_iso(paxos_validated),
        "validator": "HEAD-probe of next publication month",
        "cooldown_hours": 12,
    })

    # 3. OFAC SDN (Treasury feed).
    sdn_fetched_iso = ""
    sdn_validated_iso = ""
    try:
        from sca.tools import sanctions as _sx
        from datetime import datetime as _dt, timezone as _tz
        if _sx._SDN_FILE.exists():
            sdn_fetched_iso = _dt.fromtimestamp(
                _sx._SDN_FILE.stat().st_mtime, _tz.utc,
            ).isoformat(timespec="seconds")
        if _sx._SDN_VALIDATED_FILE.exists():
            try:
                ts = float(_sx._SDN_VALIDATED_FILE.read_text().strip())
                sdn_validated_iso = _dt.fromtimestamp(
                    ts, _tz.utc,
                ).isoformat(timespec="seconds")
            except (OSError, ValueError):
                sdn_validated_iso = ""
    except Exception:  # noqa: BLE001
        pass
    rows.append({
        "layer": "ofac_sdn",
        "label": "OFAC SDN feed",
        "description": "Treasury sanctions XML, daily refresh.",
        "count": 1 if sdn_fetched_iso else 0,
        "latest_fetched_at": sdn_fetched_iso,
        "latest_validated_at": sdn_validated_iso or sdn_fetched_iso,
        "validator": "HEAD + If-Modified-Since against Treasury",
        "cooldown_hours": 0.5,
    })

    # 4. Source snapshots (corpus pages).
    snap_fetched = [s.get("fetched_at", "") for s in sources_health]
    snap_validated = [s.get("validated_at", "") for s in sources_health]
    rows.append({
        "layer": "source_snapshots",
        "label": "Source snapshots",
        "description": "Corpus pages: ECB, BIS, FSB, OFAC, blogs.",
        "count": sum(1 for s in sources_health if s.get("fetched_at")),
        "latest_fetched_at": _max_iso(snap_fetched),
        "latest_validated_at": _max_iso(snap_validated),
        "validator": "If-Modified-Since + ETag + sha256 compare",
        "cooldown_hours": None,  # gated by canary, not cooldown
    })

    return rows


@app.post("/api/attestations/{symbol}/url")
def set_attestation_url(
    symbol: str,
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Curator endpoint: set the operational attestation URL for a token.

    Writes through the store (durable, survives redeploys). The discovery
    thread uses the same path under the hood (with `set_by=None`); a
    human curator's writes are attributed to their `user_id`.
    """
    try:
        coin = config.get_stablecoin(symbol)
    except ValueError:
        raise HTTPException(404, f"unknown stablecoin: {symbol}")
    url = str(body.get("url", "")).strip()
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be a full http(s) URL")
    notes = str(body.get("notes", "")).strip()
    via = str(body.get("via", "manual")).strip().lower() or "manual"
    if via not in {"manual", "web_search", "locator", "paxos_resolver",
                   "seed", "import"}:
        raise HTTPException(400, f"unknown via: {via}")
    get_store().set_attestation_url_override(
        coin.symbol, url, via=via,
        set_by=user["user"]["id"], notes=notes,
    )
    return {
        "ok": True, "symbol": coin.symbol, "url": url,
        "via": via, "notes": notes,
    }


# ── on-demand Compendium refresh ─────────────────────────────────────
# The Compendium page auto-refreshes its DISPLAY every 30s, but the
# underlying data only changes when the background canary sweep runs
# (every 6h). REFRESH used to just re-pull the same snapshot — clicking
# it felt like nothing happened. This endpoint actually kicks the
# attestation-gap sweep in a background thread so new URLs / re-checked
# health can appear. GET returns whether a sweep is currently running.
_COMPENDIUM_REFRESH: dict[str, Any] = {
    "running": False, "started_at": None, "completed_at": None,
    "summary": None,
}
_COMPENDIUM_REFRESH_LOCK = threading.Lock()


@app.post("/api/compendium/refresh")
def trigger_compendium_refresh() -> dict[str, Any]:
    """Kick the attestation-gap sweep + source-health canary in the
    background. Idempotent while a sweep is already running."""
    with _COMPENDIUM_REFRESH_LOCK:
        if _COMPENDIUM_REFRESH["running"]:
            return dict(_COMPENDIUM_REFRESH)
        _COMPENDIUM_REFRESH.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "summary": None,
        })

    def _runner() -> None:
        summary = None
        try:
            from sca.health_thread import run_attestation_gap_sweep
            summary = run_attestation_gap_sweep()
        except Exception as exc:  # noqa: BLE001
            from sca.observability import log_event
            log_event(
                "compendium.refresh.failed", level="error",
                error_class=type(exc).__name__,
                error_message=str(exc)[:200],
            )
        finally:
            with _COMPENDIUM_REFRESH_LOCK:
                _COMPENDIUM_REFRESH.update({
                    "running": False,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "summary": summary,
                })

    threading.Thread(target=_runner, daemon=True).start()
    with _COMPENDIUM_REFRESH_LOCK:
        return dict(_COMPENDIUM_REFRESH)


@app.get("/api/compendium/refresh")
def compendium_refresh_status() -> dict[str, Any]:
    with _COMPENDIUM_REFRESH_LOCK:
        return dict(_COMPENDIUM_REFRESH)


# ── background source-health canary ───────────────────────────────────
# Periodically re-runs `sca canary` so the snapshot store stays fresh
# and the UI's "broken" badges reflect reality without an operator ever
# touching the CLI. Default cadence is 6h; override with
# SCA_HEALTH_INTERVAL_HOURS or disable with SCA_HEALTH_DISABLED=1
# (useful in CI). See sca.health_thread for the loop body + flip
# detection.
@app.on_event("startup")
def _start_health_thread() -> None:
    try:
        from sca.health_thread import start_health_thread
        start_health_thread()
    except Exception:  # noqa: BLE001 - startup must never fail on a bg thread
        # The thread is best-effort observability; if it fails to spawn,
        # the web app still serves analyses normally. log_event already
        # surfaced the failure inside start_health_thread.
        pass


@app.on_event("startup")
def _start_movement_ticker() -> None:
    """Boot the coin movement simulator's background ticker.

    Gated by SCA_MOVEMENT_TICKER_DISABLED (set in test env) so pytest
    never accidentally fires forecast rows. start() is idempotent and
    returns silently if already running."""
    try:
        from sca.movement.ticker import start as start_movement_ticker
        start_movement_ticker()
    except Exception:  # noqa: BLE001 - startup must never fail on a bg thread
        pass


@app.on_event("startup")
def _start_chaos_thread() -> None:
    """Boot the chaos-engineering daemon. Periodic fault-injection
    against isolated code paths verifies the defensive invariants
    keep holding over time. Gated by SCA_CHAOS_DISABLED=1 (tests
    always set this; production opts in)."""
    try:
        from sca.movement.chaos import start_chaos_thread
        start_chaos_thread()
    except Exception:  # noqa: BLE001 - never block startup on observability
        pass


# ── F7 · ANALYST — the Doré agent bridge ──────────────────────────────
# F7 is the console for Doré's conversational analyst, an agent that runs
# on the Hermes runtime. The runtime deploys separately (see the repo
# Dockerfile + agent/); this bridge invokes it headless as a subprocess
# and relays the reply. When `hermes` is not on PATH — as in any
# environment where the runtime has not been installed — the bridge
# returns {"status": "runtime_offline"} and the SPA shows a calm,
# designed "activates on deployment" panel rather than an error.
_AGENT_HOME = Path(__file__).resolve().parent.parent / "agent"
# A turn touches live RPCs, an attestation fetch and an LLM — generous.
_AGENT_TIMEOUT_S = 180

# strip ANSI escape sequences a TTY-oriented runtime may emit on stdout
_ANSI = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
# strip C0 control characters from incoming user input (defense-in-depth for
# prompt-injection: a smuggled \x00 / \x1b / \x07 sequence cannot reach the
# Hermes runtime). Tab, newline, carriage return are preserved.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# A reasonable upper bound on a user turn — long enough for a paragraph,
# short enough that an attacker cannot pump tokens through the agent.
_AGENT_MAX_MESSAGE_CHARS = 4_000


def _hermes_binary() -> str | None:
    """Path to the `hermes` runtime binary, or None when not installed."""
    return shutil.which("hermes")


@app.post("/api/agent")
def agent(
    body: dict[str, Any],
    _rate: None = Depends(rate_limit_enforce),
) -> dict[str, Any]:
    """Relay one turn to Doré's conversational analyst (the Hermes agent).

    Takes the user's plain-language message, invokes the Hermes runtime in
    a non-interactive prompt mode, and returns the analyst's reply plus any
    tool-call trace the runtime surfaced.

    The Hermes runtime is a *separate* deploy artefact (see the repo
    Dockerfile). When its binary is absent the bridge does not crash — it
    returns ``{"status": "runtime_offline"}`` so the console can render its
    designed offline state. This is the expected response until deployment.

    Defense-in-depth against prompt injection: the agent itself has
    SOUL.md rules treating returned content as data not instructions, but
    we also (1) strip C0 control characters, (2) cap message length, and
    (3) rate-limit per IP so no single client can flood the agent.
    """
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(400, "message is required")
    if len(message) > _AGENT_MAX_MESSAGE_CHARS:
        raise HTTPException(
            400,
            f"message exceeds {_AGENT_MAX_MESSAGE_CHARS} characters",
        )
    message = _CONTROL_CHARS.sub("", message)

    binary = _hermes_binary()
    if binary is None:
        # The runtime is not installed here — this is the normal pre-deploy
        # state, not an error. The console renders a tasteful offline panel.
        return {"status": "runtime_offline", "message": message}

    env = dict(os.environ)
    env.setdefault("HERMES_HOME", str(_AGENT_HOME))
    try:
        # Non-interactive prompt mode: one message in, one reply out. The
        # runtime spawns its own read-only `dore` MCP server (config.yaml).
        proc = subprocess.run(
            [binary, "--prompt", message],
            capture_output=True,
            text=True,
            timeout=_AGENT_TIMEOUT_S,
            cwd=str(_AGENT_HOME),
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            504, "the analyst did not finish in time — please retry"
        ) from None
    except OSError as exc:  # noqa: BLE001 — binary vanished / not executable
        return {"status": "runtime_offline", "message": message,
                "detail": f"{type(exc).__name__}: {exc}"}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise HTTPException(502, f"analyst runtime error: {err[:400]}")

    reply = _ANSI.sub("", proc.stdout or "").strip()
    return {
        "status": "ok",
        "message": message,
        "reply": reply,
        # the trace is parsed client-side from the reply when present;
        # surfaced here too so a richer runtime build can populate it.
        "trace": body.get("_trace") or [],
    }


# ── static frontend ───────────────────────────────────────────────────
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


# ── F9 · MOVEMENT SIMULATOR — predict / attribute / score / narrate ──
# The simulator's read surface for the SPA. Four endpoints, one
# responsibility each:
#   /api/simulator/state       — ticker liveness + config + latest summary
#   /api/simulator/predictions — recent calls per symbol (the live frame)
#   /api/simulator/calibration — the track record (reliability bins, Brier)
#   /api/simulator/config      — GET current, POST validated update
#
# All four are cheap reads off the store; no LLM calls happen in the
# request path. The ticker thread is the only thing that emits new
# predictions, which keeps the request path deterministic + fast.

@app.get("/api/simulator/state")
def simulator_state() -> dict[str, Any]:
    """Ticker liveness + cached last-cycle summary + current config.

    Read by the SPA on view mount to render the configuration panel
    and the 'last tick N min ago' status line. Never blocks on the
    ticker — falls back to a minimal 'not yet started' payload if the
    background thread hasn't run yet."""
    from sca.movement import config as sim_cfg
    from sca.movement.brave_context import quota_state as brave_quota_state
    from sca.movement.ticker import state as ticker_state
    return {
        "ticker": ticker_state(),
        "config": sim_cfg.load(),
        # Brave web-search daily quota — surfaced so operators can
        # see exactly how much of their free-tier budget the
        # simulator is burning. Cap is configurable via env.
        "brave_quota": brave_quota_state(),
        # Peg-source consensus summary (audit #7) — counts of agreed
        # / single / disputed ticks from the last ticker cycle so
        # operators can see source-health at a glance.
        "peg_consensus": _peg_consensus_summary(),
    }


def _peg_consensus_summary() -> dict[str, Any]:
    """Pull consensus counts from the most recent ticker cycle's
    summary. Falls back to a zero-state when no cycle has run."""
    from sca.movement.ticker import state as ticker_state
    s = ticker_state() or {}
    last = (s.get("last_summary") or {})
    resolver = last.get("resolver") or {}
    # The peg_tick refresh summary is keyed under 'per_symbol' /
    # alongside resolver; in current ticker impl we stash the
    # consensus counts in last_summary.peg_tick_refresh if present.
    # Default zero state.
    refresh = last.get("peg_tick_refresh") or {}
    kinds = refresh.get("consensus_kinds") or {
        "agreed": 0, "single": 0, "disputed": 0,
    }
    return {
        "consensus_kinds": kinds,
        "sources_known": ["coinbase", "kraken"],
        "agreement_tolerance_bps": 5.0,
    }


@app.get("/api/simulator/predictions")
def simulator_predictions(
    symbol: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Recent predictions (newest first) with their resolution rows
    joined when present. The SPA renders these into the fan chart +
    attribution feed.

    Optional filters narrow the slice. Limit is capped at 500 so a
    rogue client can't pull the entire archive in one call.

    Degrades gracefully when the predictions table is missing
    (migration 0007 not yet applied) — returns an empty archive so
    the UI renders the 'archive is empty' state instead of a 500."""
    store = get_store()
    cap = max(1, min(limit, 500))
    try:
        preds = store.list_predictions(
            symbol=symbol, kind=kind, limit=cap) or []
        res = store.list_resolutions(
            symbol=symbol, kind=kind, limit=cap) or []
    except Exception as exc:  # noqa: BLE001
        log_event(
            "simulator.api.predictions.degraded", level="warn",
            error_class=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        return {"predictions": [], "count": 0,
                "schema_missing": "predictions table not yet provisioned"}
    # Join by prediction_id so each prediction carries its resolution
    # in one row — the UI doesn't have to do a join itself.
    res_by_pred = {r.get("id"): r for r in res if r.get("id")}
    out = []
    for p in preds:
        joined = dict(p)
        r = res_by_pred.get(p.get("id"))
        if r is not None:
            joined["resolution"] = {
                "resolved_at": r.get("resolved_at"),
                "actual_value": r.get("actual_value"),
                "brier_score": r.get("brier_score"),
                "crps_score": r.get("crps_score"),
                "outcome_kind": r.get("outcome_kind"),
                "narrative": r.get("narrative"),
                "baseline_persistence_brier":
                    r.get("baseline_persistence_brier"),
                "baseline_climatology_brier":
                    r.get("baseline_climatology_brier"),
            }
        out.append(joined)
    return {"predictions": out, "count": len(out)}


@app.get("/api/simulator/calibration")
def simulator_calibration(
    symbol: str | None = None,
    kind: str | None = None,
    horizon_minutes: int | None = None,
) -> dict[str, Any]:
    """The track record. Reliability bins, mean Brier, mean CRPS,
    outcome histogram, plus the two baseline Briers (persistence +
    climatology) for skill comparison.

    Empty struct + count=0 when no resolutions exist — the UI
    renders the 'waiting for resolutions to accumulate' state.
    """
    try:
        result = get_store().calibration_summary(
            symbol=symbol, kind=kind, horizon_minutes=horizon_minutes,
        )
        # The store now sets schema_missing=True directly; translate
        # the boolean into the descriptive string the UI expects.
        if result.get("schema_missing") is True:
            result["schema_missing"] = (
                "resolutions table not yet provisioned"
            )
        return result
    except Exception as exc:  # noqa: BLE001
        log_event(
            "simulator.api.calibration.degraded", level="warn",
            error_class=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        return {
            "count": 0, "brier_mean": None, "crps_mean": None,
            "outcome_histogram": {}, "reliability_bins": [],
            "baseline_persistence_brier_mean": None,
            "baseline_climatology_brier_mean": None,
            "schema_missing": "resolutions table not yet provisioned",
        }


@app.get("/api/simulator/timeline")
def simulator_timeline(
    symbols: str | None = None,
    limit_per_symbol: int = 120,
    bin_minutes: int = 5,
) -> dict[str, Any]:
    """Chart data for the main canvas.

    Returns per-symbol time-series for the candle overlay:
      - raw peg ticks (read_at, deviation_bps, consensus_kind)
      - OHLC-binned candles (open, high, low, close, t_start, t_end)
      - latest prediction with its bands so the cone can be drawn
        forward from 'now'

    `symbols`: comma-separated list, defaults to whatever the
    simulator config has enabled.
    `limit_per_symbol`: cap raw ticks pulled per symbol (default 120
    = 2h at 1min cadence). The bin step is `bin_minutes` (default 5).
    """
    from sca.movement import config as sim_cfg
    cfg = sim_cfg.load()
    requested = (symbols or "").split(",") if symbols else cfg["symbols"]
    requested = [s.strip().upper() for s in requested if s.strip()]
    cap = max(10, min(limit_per_symbol, 1000))
    bin_min = max(1, min(bin_minutes, 60))

    store = get_store()
    out: dict[str, Any] = {
        "symbols": requested,
        "bin_minutes": bin_min,
        "tokens": [],
    }
    for sym in requested:
        try:
            ticks = store.list_peg_ticks(sym, limit=cap) or []
        except Exception:  # noqa: BLE001
            ticks = []
        # Newest-first → oldest-first for plotting.
        ticks = list(reversed(ticks))
        candles = _bin_to_candles(ticks, bin_min)
        # Latest prediction for the forecast cone.
        try:
            preds = store.list_predictions(
                symbol=sym, kind="peg_deviation", limit=1) or []
        except Exception:  # noqa: BLE001
            preds = []
        latest = preds[0] if preds else None
        out["tokens"].append({
            "symbol": sym,
            "ticks": [{
                "t": t.get("read_at"),
                "v": float(t.get("deviation_bps") or 0.0),
                "consensus_kind": t.get("consensus_kind") or "single",
                "max_disagreement_bps": float(
                    t.get("max_disagreement_bps") or 0.0),
            } for t in ticks],
            "candles": candles,
            "latest_prediction": (latest and {
                "made_at": latest.get("made_at"),
                "resolves_at": latest.get("resolves_at"),
                "point": _safe_float(latest.get("point")),
                "p50_low": _safe_float(latest.get("p50_low")),
                "p50_high": _safe_float(latest.get("p50_high")),
                "p80_low": _safe_float(latest.get("p80_low")),
                "p80_high": _safe_float(latest.get("p80_high")),
                "p95_low": _safe_float(latest.get("p95_low")),
                "p95_high": _safe_float(latest.get("p95_high")),
                "confidence_word": latest.get("confidence_word"),
                "horizon_minutes": latest.get("horizon_minutes"),
                "judge_synthesis": latest.get("judge_synthesis"),
                "judge_insight": latest.get("judge_insight"),
                "judge_pitch": latest.get("judge_pitch"),
            }) or None,
        })
    return out


def _safe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _bin_to_candles(ticks: list[dict], bin_minutes: int) -> list[dict]:
    """Aggregate raw peg-tick readings into OHLC candles per
    `bin_minutes` window. Each candle: t_start, t_end, open, high,
    low, close, count. Returns empty when no ticks land in any bin
    (typically when the simulator hasn't accumulated history yet)."""
    from collections import defaultdict
    if not ticks:
        return []
    bin_s = bin_minutes * 60
    buckets: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for t in ticks:
        ts = t.get("read_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        epoch = int(dt.timestamp())
        bucket = (epoch // bin_s) * bin_s
        buckets[bucket].append((ts, float(t.get("deviation_bps") or 0.0)))
    candles: list[dict] = []
    for bucket_start in sorted(buckets.keys()):
        rows = buckets[bucket_start]
        rows.sort(key=lambda r: r[0])
        values = [v for _, v in rows]
        candles.append({
            "t_start": datetime.fromtimestamp(
                bucket_start, timezone.utc).isoformat(timespec="seconds"),
            "t_end": datetime.fromtimestamp(
                bucket_start + bin_s, timezone.utc).isoformat(
                    timespec="seconds"),
            "open": values[0],
            "high": max(values),
            "low": min(values),
            "close": values[-1],
            "count": len(values),
        })
    return candles


@app.get("/api/simulator/stream")
def simulator_stream(request: Request):
    """Server-Sent Events stream — the heartbeat that makes the
    YC-grade UI feel genuinely live.

    Pushes a JSON event every 2s with a compact diff of the feed,
    plus an immediate snapshot on connect. The client uses this as
    the primary live channel and polls /api/simulator/feed every
    20s as a reconciliation heartbeat.

    Event types:
      - 'snapshot' (on connect): full feed payload
      - 'tick' (every 2s): {tokens: [{symbol, current_bps, delta}],
                            events: [new event rows since last]}
      - 'heartbeat' (every 10s if nothing changed): keep-alive

    SSE is the right shape: one direction (server → client),
    EventSource handles reconnection, no WebSocket complexity, and
    devtools show a clean named event stream that demos well.
    """
    from fastapi.responses import StreamingResponse
    import asyncio
    import json as _json

    async def event_generator():
        # Snapshot on connect — the first paint should have full data.
        try:
            payload = simulator_feed()
            yield "event: snapshot\n"
            yield f"data: {_json.dumps(payload, default=str)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {_json.dumps({'error': str(exc)[:200]})}\n\n"
            return

        # Track last seen state so each tick is a DIFF, not a snapshot.
        # `last_event_ts` is a float (unix seconds) because observability
        # events ship `ts` as a float. Initialising it as "" (the old
        # bug) caused `float > ""` TypeErrors every SSE cycle — every
        # client saw the stream silently fail on the first diff cycle.
        last_event_ts: float = 0.0
        last_current_bps: dict[str, float | None] = {
            t["symbol"]: t.get("current_bps") for t in payload.get("tokens", [])
        }
        idle_ticks = 0

        def _ev_ts(ev) -> float:
            """Coerce an event ts to a comparable float. Events ship
            ts as a float, but a future schema change or a manually-
            injected event could send a string — be tolerant."""
            t = ev.get("ts")
            if isinstance(t, (int, float)):
                return float(t)
            if isinstance(t, str) and t:
                try:
                    return float(t)
                except ValueError:
                    return 0.0
            return 0.0

        while True:
            # Client closed connection?
            if await request.is_disconnected():
                break
            try:
                await asyncio.sleep(2.0)
                fresh = simulator_feed()
                # Compute the diff: tokens whose current_bps changed,
                # plus new events since the last event ts.
                changed_tokens = []
                new_last_current: dict[str, float | None] = {}
                for t in fresh.get("tokens", []):
                    sym = t["symbol"]
                    new_v = t.get("current_bps")
                    old_v = last_current_bps.get(sym)
                    new_last_current[sym] = new_v
                    if new_v != old_v:
                        # Include the sparkline + current_price so the
                        # client can refresh the line + the headline
                        # USD price in place — without these the
                        # ticker numbers flash but the chart stays
                        # stale until the next 20s reconciliation.
                        changed_tokens.append({
                            "symbol": sym,
                            "current_bps": new_v,
                            "current_price": t.get("current_price"),
                            "deltas": t.get("deltas"),
                            "consensus": t.get("consensus"),
                            "brand": t.get("brand"),
                            "sparkline": t.get("sparkline"),
                        })
                last_current_bps = new_last_current

                new_events = []
                for ev in fresh.get("events", []):
                    if _ev_ts(ev) > last_event_ts:
                        new_events.append(ev)
                if fresh.get("events"):
                    last_event_ts = max(
                        (_ev_ts(e) for e in fresh["events"]),
                        default=last_event_ts,
                    )

                if changed_tokens or new_events:
                    idle_ticks = 0
                    yield "event: tick\n"
                    yield f"data: {_json.dumps({'tokens': changed_tokens, 'events': new_events, 'brave_quota': fresh.get('brave_quota'), 'ticker_last_tick_at': (fresh.get('ticker') or {}).get('last_tick_at')}, default=str)}\n\n"
                else:
                    idle_ticks += 1
                    # Send a keep-alive every ~10s of idle so the
                    # connection doesn't get reaped by intermediaries
                    # and the client knows the stream is alive.
                    if idle_ticks >= 5:
                        idle_ticks = 0
                        yield "event: heartbeat\n"
                        yield f"data: {_json.dumps({'ts': fresh.get('computed_at')})}\n\n"
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                # Best-effort: log + continue. Don't break the stream
                # over one bad cycle.
                log_event(
                    "simulator.stream.cycle_failed", level="warn",
                    error_class=type(exc).__name__,
                    error_message=str(exc)[:200],
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


# Process-level cache for /feed. The UI polls every 20s and SSE pushes
# diffs in between, so most /feed calls come in clusters (multiple
# tabs, reload bursts, SSE reconciliation). A 3s cache cuts the hot
# path to ~1 store hit per 3s regardless of client count, without
# making the data feel stale (SSE diffs cover the in-between window).
_FEED_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
_FEED_CACHE_TTL_S = 3.0
_FEED_CACHE_LOCK = None  # lazy-initialised threading.Lock


def _fetch_token_block(store, sym: str, brand_for, now):
    """One token's store reads + per-token assembly. Pure function over
    `store` so we can run N of these concurrently via ThreadPoolExecutor.

    Performance audit (May 2026): the original feed loop did 4 sequential
    Supabase reads per token × 18 tokens = 72 HTTP/2 round trips serialised
    on the same event loop, totaling ~12s warm. Parallelising the per-token
    work and dropping the dead per-token `calibration` block (the UI only
    reads the top-level archive) brings this down by ~10x.
    """
    from datetime import datetime, timedelta, timezone
    sym_u = sym.upper()
    brand = brand_for(sym_u)
    try:
        ticks = store.list_peg_ticks(sym_u, limit=400) or []
    except Exception:  # noqa: BLE001
        ticks = []
    ticks_sorted = sorted(
        [t for t in ticks if t.get("read_at")],
        key=lambda t: t["read_at"],
    )

    current = None
    current_price = None  # USD price (e.g. 0.999946) — surfaced alongside bp
    spark: list[dict] = []
    deltas = {"d1m": None, "d5m": None, "d1h": None, "d24h": None}
    consensus_now = "single"
    max_disagreement_now = 0.0
    sources_now: list[dict[str, Any]] = []
    if ticks_sorted:
        latest = ticks_sorted[-1]
        current = float(latest.get("deviation_bps") or 0.0)
        consensus_now = latest.get("consensus_kind") or "single"
        max_disagreement_now = float(
            latest.get("max_disagreement_bps") or 0.0)
        sources_now = latest.get("sources") or []
        # USD price — prefer the persisted `price` column on the tick
        # row (the canonical consensus_price), else compute from bp.
        # Stablecoin price = 1.0 + (bp / 10000); a 1bp move = $0.0001.
        try:
            raw_price = latest.get("price")
            if isinstance(raw_price, (int, float)):
                current_price = float(raw_price)
            else:
                current_price = 1.0 + (current / 10_000.0)
        except (TypeError, ValueError):
            current_price = 1.0 + (current / 10_000.0)
        spark = [
            {
                "t": t.get("read_at"),
                "v": float(t.get("deviation_bps") or 0.0),
                "ck": t.get("consensus_kind") or "single",
            }
            for t in ticks_sorted[-60:]
        ]
        for label, window in (
            ("d1m", timedelta(minutes=1)),
            ("d5m", timedelta(minutes=5)),
            ("d1h", timedelta(hours=1)),
            ("d24h", timedelta(hours=24)),
        ):
            target = now - window
            best = None
            best_gap = float("inf")
            for t in ticks_sorted:
                try:
                    rt = datetime.fromisoformat(
                        str(t["read_at"]).replace("Z", "+00:00"))
                    if rt.tzinfo is None:
                        rt = rt.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                gap = abs((target - rt).total_seconds())
                if gap < best_gap and gap < window.total_seconds() * 0.6:
                    best_gap = gap
                    best = t
            if best is not None:
                base = float(best.get("deviation_bps") or 0.0)
                deltas[label] = round(current - base, 4)

    try:
        preds = store.list_predictions(
            symbol=sym_u, kind="peg_deviation", limit=1) or []
    except Exception:  # noqa: BLE001
        preds = []
    latest_pred = preds[0] if preds else None

    # Per-token recent track record — the last 10 resolved predictions
    # for this symbol. The UI uses this for the "RECENT TRACK RECORD"
    # strip under the cone so an investor sees how well the model has
    # been forecasting THIS token, not just the aggregate archive.
    try:
        recent_res = store.list_resolutions(
            symbol=sym_u, kind="peg_deviation", limit=10) or []
    except Exception:  # noqa: BLE001
        recent_res = []
    # Trim to the fields the UI needs — keeps the feed payload lean.
    recent_resolutions = [
        {
            "resolved_at": r.get("resolved_at"),
            "outcome_kind": r.get("outcome_kind"),
            "actual_value": _safe_float(r.get("actual_value")),
            "point": _safe_float(r.get("point")),
            "p50_low": _safe_float(r.get("p50_low")),
            "p50_high": _safe_float(r.get("p50_high")),
            "p80_low": _safe_float(r.get("p80_low")),
            "p80_high": _safe_float(r.get("p80_high")),
            "p95_low": _safe_float(r.get("p95_low")),
            "p95_high": _safe_float(r.get("p95_high")),
            "brier_score": _safe_float(r.get("brier_score")),
        }
        for r in recent_res
    ]

    from sca.movement.token_context import get_context as _get_ctx
    _ctx_for_payload = _get_ctx(sym_u) or _get_ctx(sym)
    meta = {
        "yield_bearing": bool(
            getattr(_ctx_for_payload, "yield_bearing", False)),
        "venue_type": getattr(_ctx_for_payload, "venue_type", "CEX"),
        "issuer": getattr(_ctx_for_payload, "issuer", ""),
        "backing_model": getattr(
            _ctx_for_payload, "backing_model", ""),
        "structural_one_liner": getattr(
            _ctx_for_payload, "structural_one_liner", ""),
        "cone_normal_bps": (
            _ctx_for_payload.cone_thresholds_bps[0]
            if _ctx_for_payload else None),
        "cone_alert_bps": (
            _ctx_for_payload.cone_thresholds_bps[1]
            if _ctx_for_payload else None),
        # Payout / redemption timeline — used by the hero pane to
        # show time-to-cash. Different tokens have very different
        # liquidity profiles (instant PSM swap vs 40-day lockup)
        # and that drives sizing + carry decisions.
        "payout_timeline_label": getattr(
            _ctx_for_payload, "payout_timeline_label", ""),
        "payout_details": getattr(
            _ctx_for_payload, "payout_details", ""),
    }

    return {
        "symbol": sym_u,
        "brand": brand,
        "current_bps": current,
        "current_price": current_price,  # USD price (e.g. 0.999946)
        "deltas": deltas,
        "sparkline": spark,
        "meta": meta,
        "consensus": {
            "kind": consensus_now,
            "max_disagreement_bps": max_disagreement_now,
            "sources": sources_now,
        },
        "tick_count": len(ticks_sorted),
        "latest_prediction": (latest_pred and {
            "made_at": latest_pred.get("made_at"),
            "resolves_at": latest_pred.get("resolves_at"),
            "point": _safe_float(latest_pred.get("point")),
            "p50_low": _safe_float(latest_pred.get("p50_low")),
            "p50_high": _safe_float(latest_pred.get("p50_high")),
            "p80_low": _safe_float(latest_pred.get("p80_low")),
            "p80_high": _safe_float(latest_pred.get("p80_high")),
            "p95_low": _safe_float(latest_pred.get("p95_low")),
            "p95_high": _safe_float(latest_pred.get("p95_high")),
            "prob_positive": _safe_float(
                latest_pred.get("prob_positive")),
            "confidence_word": latest_pred.get("confidence_word"),
            "horizon_minutes": latest_pred.get("horizon_minutes"),
            "drivers": latest_pred.get("drivers") or [],
            "judge_synthesis": latest_pred.get("judge_synthesis"),
            "judge_insight": latest_pred.get("judge_insight"),
            "judge_pitch": latest_pred.get("judge_pitch"),
        }) or None,
        # Per-token track record — last 10 resolved predictions for the
        # focused symbol. UI renders a marker strip + hit-rate trend.
        "recent_resolutions": recent_resolutions,
        # Per-token calibration was a dead field — the UI only reads
        # the top-level feed.calibration. Removed in the May 2026
        # perf pass; saved 6.5s of HTTP/2 latency per /feed.
    }


@app.get("/api/simulator/feed")
def simulator_feed() -> dict[str, Any]:
    """Single dense payload powering the YC-class live workspace.

    One round-trip returns everything the v3 UI needs to render a
    Bloomberg-class dashboard:
      - tokens[]: every tracked stablecoin with brand colour, current
        peg, 1m/5m/1h/24h deltas, 60-point sparkline, latest forecast
        + judge synthesis, and source-consensus state.
      - events[]: a rolling tail of observability events filtered to
        the simulator-relevant kinds (ticks emitted, resolutions
        graded, brave hits, source disputes).
      - sources[]: per-peg-source liveness — last successful read
        time, count of consensus_kind in the last hour.
      - quota: brave + ticker + config rollup.

    Performance: process-level cache (TTL ~3s) + parallel per-token
    store fetch. Hot warm path is one cache hit; cold path fans out
    18 token fetches concurrently. Was 12s sequential, now ~1s warm.
    """
    import time as _time
    import threading as _threading
    from concurrent.futures import ThreadPoolExecutor
    from collections import Counter, defaultdict
    from datetime import datetime, timezone
    from sca import config as _cfg
    from sca.movement import config as sim_cfg
    from sca.movement.brave_context import quota_state as brave_quota_state
    from sca.movement.ticker import state as ticker_state
    from sca.observability import recent_events
    from sca.token_palette import brand_for

    global _FEED_CACHE_LOCK
    if _FEED_CACHE_LOCK is None:
        _FEED_CACHE_LOCK = _threading.Lock()

    # Process-level cache — first-line latency floor. Skip the cache
    # path in test/CI runs (SCA_SIMULATOR_FEED_CACHE_DISABLED=1) so
    # successive calls in the same suite get fresh fixture data.
    #
    # Lock the read so a concurrent writer can't update payload + ts
    # between our two dict accesses and return a mismatched pair.
    # Cheap: ~microsecond critical section.
    import os as _os
    cache_disabled = _os.environ.get(
        "SCA_SIMULATOR_FEED_CACHE_DISABLED", "").strip() == "1"
    now_ts = _time.time()
    if not cache_disabled:
        with _FEED_CACHE_LOCK:
            cached = _FEED_CACHE.get("payload")
            cached_ts = _FEED_CACHE.get("ts", 0)
        if cached and (now_ts - cached_ts) < _FEED_CACHE_TTL_S:
            return cached

    cfg = sim_cfg.load()
    sym_list = cfg.get("symbols") or []
    store = get_store()
    now = datetime.now(timezone.utc)

    # Parallel per-token fetch. 18 tokens × ~150ms-per-call drops
    # from 5.4s sequential to ~300ms wall-clock with 8 workers.
    tokens_out: list[dict[str, Any]] = []
    if sym_list:
        with ThreadPoolExecutor(
                max_workers=min(8, len(sym_list))) as pool:
            futures = [
                pool.submit(_fetch_token_block, store, sym, brand_for, now)
                for sym in sym_list
            ]
            for fut in futures:
                try:
                    tokens_out.append(fut.result(timeout=10))
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        "simulator.feed.token_block_failed", level="warn",
                        error_class=type(exc).__name__,
                        error_message=str(exc)[:160],
                    )
    # Preserve original sym_list order for stable UI rendering.
    order = {s.upper(): i for i, s in enumerate(sym_list)}
    tokens_out.sort(key=lambda t: order.get(t["symbol"], 999))

    # Recent event stream — narrow filter to simulator-relevant
    # kinds. The earlier (kind OR level=warn) filter caught too
    # many unrelated rpc/snapshot warnings; explicit allow-list is
    # cleaner for THE WIRE.
    SIM_EVENT_KINDS = {
        "movement.ticker.cycle",
        "movement.tick.manual",
        "movement.peg_tick.dispute_persisted",
        "movement.peg_tick.persist_failed",
        "movement.brave.cache_hit", "movement.brave.fetched",
        "movement.brave.skip_calm", "movement.brave.quota_hit",
        "movement.brave.fetch_failed_served_stale",
        "movement.resolver.graded",
        "movement.resolver.list_failed",
        "movement.judge.voice_rule_triggered",
        "movement.judge.citation_forged",
        "movement.judge.length_cap_triggered",
        "movement.judge.llm_unavailable",
        "movement.judge.llm_failed",
        "movement.judge.parse_failed",
        "movement.config.updated",
        "peg_price.dispute",
        "peg_price.no_source_responded",
        "store.schema_missing",
        "snapshot.validated",
        "snapshot.saved",
    }
    raw = recent_events(limit=300) or []
    events_out = []
    for ev in raw:
        if ev.get("kind") not in SIM_EVENT_KINDS:
            continue
        events_out.append({
            "kind": ev.get("kind"),
            "level": ev.get("level", "info"),
            "ts": ev.get("ts"),
            "symbol": ev.get("symbol", ""),
            "summary": _event_summary(ev),
        })
        if len(events_out) >= 80:
            break

    # Per-source liveness — last successful peg read per source name.
    source_seen: dict[str, dict[str, Any]] = {}
    source_hist: dict[str, Counter] = defaultdict(Counter)
    for tk in tokens_out:
        ck = tk["consensus"]["kind"]
        for s in tk["consensus"]["sources"]:
            name = s.get("name", "unknown")
            source_hist[name][ck] += 1
            cur = source_seen.get(name)
            cur_age = (cur and cur.get("fetched_at")) or 0
            new_age = float(s.get("fetched_at") or 0)
            if not cur or new_age > cur_age:
                source_seen[name] = s
    sources_out = []
    for name, last in source_seen.items():
        sources_out.append({
            "name": name,
            "fetched_at": last.get("fetched_at"),
            "consensus_hist": dict(source_hist.get(name, {})),
        })

    payload = {
        "tokens": tokens_out,
        "events": events_out,
        "sources": sources_out,
        "ticker": ticker_state(),
        "config": cfg,
        "brave_quota": brave_quota_state(),
        "computed_at": now.isoformat(timespec="seconds"),
    }
    # Populate cache under the lock so two concurrent /feed callers
    # don't both pay the store-fetch cost. The first wins; the second
    # sees the cache on its next check.
    with _FEED_CACHE_LOCK:
        _FEED_CACHE["payload"] = payload
        _FEED_CACHE["ts"] = _time.time()
    return payload


def _event_summary(ev: dict[str, Any]) -> str:
    """One-line human label for an event in the live feed.
    Editorial, lower-case, ≤ 64 chars so the wire reads as a
    cohesive stream rather than raw event names."""
    kind = ev.get("kind", "")
    sym = ev.get("symbol", "") or ""
    if kind == "movement.ticker.cycle":
        n = ev.get("symbols", "?")
        return f"cycle complete · {n} symbol{'s' if n != 1 else ''}"
    if kind == "movement.tick.manual":
        return f"manual tick · burst {ev.get('burst', 1)}"
    if kind == "movement.resolver.graded":
        out = ev.get("outcome", "?")
        brier = ev.get("brier")
        crps = ev.get("crps")
        score = (f"brier {brier:.3f}" if isinstance(brier, (int, float))
                 else (f"crps {crps:.2f}" if isinstance(crps, (int, float))
                       else ""))
        return f"resolved {out}" + (f" · {score}" if score else "")
    if kind == "movement.brave.fetched":
        return (f"brave fetched · {ev.get('count', 0)} hits · "
                f"{ev.get('calls_today', '?')}/{ev.get('cap', '?')} today")
    if kind == "movement.brave.cache_hit":
        age = ev.get("age_s")
        return f"brave cache hit · age {age}s" if age is not None else "brave cache hit"
    if kind == "movement.brave.skip_calm":
        return "brave skipped — forecast calm"
    if kind == "movement.brave.quota_hit":
        return f"brave quota hit · {ev.get('calls_today', '?')} / {ev.get('cap', '?')}"
    if kind == "movement.brave.fetch_failed_served_stale":
        return "brave fetch failed · served stale cache"
    if kind == "movement.peg_tick.dispute_persisted":
        sp = ev.get("max_disagreement_bps")
        return f"peg sources disputed · spread {sp:.2f}bp" \
            if isinstance(sp, (int, float)) else "peg sources disputed"
    if kind == "movement.peg_tick.persist_failed":
        return "peg tick persist failed"
    if kind == "movement.judge.citation_forged":
        return f"judge forged citation stripped · {ev.get('forged_count', '?')} dropped"
    if kind == "movement.judge.voice_rule_triggered":
        return f"judge voice rule fired · {ev.get('stripped_count', '?')} stripped"
    if kind == "movement.judge.length_cap_triggered":
        f = ev.get("field", "field")
        return f"judge {f} length cap · trimmed to {ev.get('kept_words', '?')}w"
    if kind == "movement.judge.llm_unavailable":
        return "judge unavailable · falling back to engine prose"
    if kind == "movement.config.updated":
        return (f"config updated · tick "
                f"{ev.get('tick_interval_minutes', '?')}m, horizon "
                f"{ev.get('horizon_minutes', '?')}m")
    if kind == "peg_price.dispute":
        return "peg-price dispute logged"
    if kind == "peg_price.no_source_responded":
        return "peg source silence · all upstreams failed"
    if kind == "store.schema_missing":
        return f"schema missing · {ev.get('table', '?')} table"
    if kind == "snapshot.validated":
        return f"snapshot validated · {ev.get('id', '?')[:32]}"
    if kind == "snapshot.saved":
        return f"snapshot saved · {ev.get('bytes', '?')}b"
    return kind


@app.get("/api/simulator/trader")
def simulator_trader() -> dict[str, Any]:
    """The Discipline Trader's recent activity + track record.

    Returns:
      - persona / tagline / version (editorial framing)
      - open[]: currently-open simulated trades (oldest first → newest)
      - resolved[]: last N closed trades with P&L
      - track_record: aggregate wins/losses/net PnL/win-rate +
        daily budget tracking, equity curve, streak, daily ledger,
        best/worst day

    Deterministic — the trader runs inside the ticker cycle. This
    endpoint just reads the persisted store, so it's cache-friendly
    and never blocks on the LLM.
    """
    from sca.movement.trader import (
        all_trades, track_record, PERSONA_NAME, PERSONA_TAGLINE,
    )
    trades = all_trades()  # already sorted newest-first
    opens = [t for t in trades if t.get("status") == "open"]
    resolves = [t for t in trades if t.get("status") == "resolved"]
    return {
        "persona": PERSONA_NAME,
        "tagline": PERSONA_TAGLINE,
        "open": list(reversed(opens)),  # oldest open at top
        "resolved": resolves[:20],      # last 20 resolved
        "track_record": track_record(),
    }


@app.get("/api/simulator/trader/trade/{trade_id}")
def simulator_trader_trade(trade_id: str) -> dict[str, Any]:
    """Full trade receipt — used by the UI for the "receipt detail"
    modal. Returns every field on the trade so a professional reader
    can audit entry sources, consensus state, forecast bands at
    entry, exit-side readings, and the calibration outcome."""
    from sca.movement.trader import all_trades
    for t in all_trades():
        if t.get("id") == trade_id:
            return t
    raise HTTPException(404, f"no trade with id: {trade_id}")


@app.get("/api/simulator/commentary/{symbol}")
def simulator_commentary(symbol: str, refresh: bool = False) -> dict[str, Any]:
    """Per-token AI Commentary card — structural cheat sheet meets
    live read. Cached 1h per regime bucket so chip clicks across
    tokens don't burn LLM budget. Honest fallback when the LLM is
    unavailable: the deterministic card uses the cheat sheet alone
    and names the gap.

    Read by the simulator UI when the operator focuses a new token.
    """
    from sca.movement.commentary import get_commentary
    sym = symbol.strip()
    # Light-weight live state: pull the latest tick + prediction for
    # JUST this symbol, not the full feed. Keeps commentary response
    # times below ~1s so chip clicks feel instant.
    store = get_store()
    live: dict[str, Any] = {}
    try:
        ticks = store.list_peg_ticks(sym, limit=2) or []
        if ticks:
            live["current_bps"] = float(
                ticks[0].get("deviation_bps") or 0.0)
        preds = store.list_predictions(
            symbol=sym, kind="peg_deviation", limit=1) or []
        if preds:
            p = preds[0]
            ph = _safe_float(p.get("p80_high"))
            pl = _safe_float(p.get("p80_low"))
            if ph is not None and pl is not None:
                live["cone_p80_bps"] = (ph - pl) / 2
    except Exception as exc:  # noqa: BLE001
        log_event(
            "simulator.commentary.live_state_failed", level="info",
            symbol=sym, error_class=type(exc).__name__,
        )
    # UX-fast default: serve cached-or-deterministic instantly and
    # schedule a background LLM refresh. `?refresh=true` waits for a
    # fresh LLM call (operator-initiated).
    commentary = get_commentary(
        sym, live,
        force=refresh,
        wait_for_llm=refresh,
    )
    if commentary is None:
        raise HTTPException(
            404, f"no structural context registered for symbol: {sym}")
    # Carry the structural overlay fields (pl_lens, yield_bearing,
    # venue_type, issuer) directly so the card can render P&L framing
    # without a second round trip to /api/simulator/feed.
    from sca.movement.token_context import get_context as _get_ctx
    ctx = _get_ctx(sym)
    return {
        "symbol": commentary.symbol,
        "headline": commentary.headline,
        "body": commentary.body,
        "citations": commentary.citations,
        "structural_one_liner": commentary.structural_one_liner,
        "cone_normal_bps": commentary.cone_normal_bps,
        "cone_alert_bps": commentary.cone_alert_bps,
        "model": commentary.model,
        "pl_lens": getattr(ctx, "pl_lens", "") or "",
        "yield_bearing": bool(getattr(ctx, "yield_bearing", False)),
        "venue_type": getattr(ctx, "venue_type", "CEX"),
        "issuer": getattr(ctx, "issuer", "") if ctx else "",
        "backing_model": getattr(ctx, "backing_model", "") if ctx else "",
    }


@app.get("/api/simulator/commentary/{symbol}/dive")
def simulator_commentary_dive(
    symbol: str, refresh: bool = False,
) -> dict[str, Any]:
    """Commentary DIVE — structured deeper-read on the same token.

    Generated by the LLM from the shallow commentary's headline +
    body + pl_lens + citations + live state, then cached by a hash
    over those inputs. The dive unpacks jargon-dense phrases (PSM,
    NAV, cone half-width, watchlist signal) into plain-English
    explanations, adds a richer P&L lens, and names the loss tail.

    Falls back to a deterministic skeleton when the LLM is offline.
    """
    from sca.movement.commentary import get_commentary
    from sca.movement.commentary_dive import generate_dive
    from sca.movement.token_context import get_context as _get_ctx

    sym = symbol.strip()
    store = get_store()
    live: dict[str, Any] = {}
    try:
        ticks = store.list_peg_ticks(sym, limit=2) or []
        if ticks:
            live["current_bps"] = float(
                ticks[0].get("deviation_bps") or 0.0)
        preds = store.list_predictions(
            symbol=sym, kind="peg_deviation", limit=1) or []
        if preds:
            p = preds[0]
            ph = _safe_float(p.get("p80_high"))
            pl = _safe_float(p.get("p80_low"))
            if ph is not None and pl is not None:
                live["cone_p80_bps"] = (ph - pl) / 2
    except Exception:  # noqa: BLE001
        pass

    commentary = get_commentary(sym, live, force=False, wait_for_llm=False)
    if commentary is None:
        raise HTTPException(
            404, f"no structural context registered for symbol: {sym}")
    ctx = _get_ctx(sym)
    ctx_extras = {
        "issuer": getattr(ctx, "issuer", "") if ctx else "",
        "backing_model": getattr(ctx, "backing_model", "") if ctx else "",
        "backing_short": getattr(ctx, "backing_short", "") if ctx else "",
        "yield_bearing": bool(getattr(ctx, "yield_bearing", False))
            if ctx else False,
        "venue_type": getattr(ctx, "venue_type", "") if ctx else "",
        "payout_timeline_label":
            getattr(ctx, "payout_timeline_label", "") if ctx else "",
        "payout_details":
            getattr(ctx, "payout_details", "") if ctx else "",
        "watchlist_signal":
            getattr(ctx, "watchlist_signal", "") if ctx else "",
    }
    dive = generate_dive(
        symbol=sym,
        headline=commentary.headline or "",
        body=commentary.body or "",
        pl_lens=getattr(ctx, "pl_lens", "") or "",
        citations=commentary.citations or [],
        live=live, ctx_extras=ctx_extras,
        force_refresh=refresh,
    )
    return {
        "symbol": dive.symbol,
        "intro": dive.intro,
        "sections": dive.sections,
        "citations": dive.citations,
        "fallback": dive.fallback,
        "generated_at": dive.generated_at,
    }


@app.get("/api/simulator/config")
def simulator_config_get() -> dict[str, Any]:
    """Current simulator config — read by the configuration panel."""
    from sca.movement import config as sim_cfg
    return sim_cfg.load()


@app.post("/api/simulator/config")
def simulator_config_set(
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Update the simulator config. Auth-gated because cadence /
    symbol changes ripple through the prediction archive — only
    signed-in curators can flip them.

    Returns the merged config so the panel can refresh its bound
    state. ValueError from validation -> 400."""
    from sca.movement import config as sim_cfg
    try:
        merged = sim_cfg.save(body or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log_event(
        "movement.config.updated", level="info",
        by=user["user"]["id"],
        tick_interval_minutes=merged.get("tick_interval_minutes"),
        horizon_minutes=merged.get("horizon_minutes"),
        symbols=merged.get("symbols"),
        kinds=merged.get("kinds"),
    )
    return merged


@app.post("/api/simulator/tick")
def simulator_tick_now(
    count: int = 1,
    user: dict[str, Any] | None = Depends(current_user),
) -> dict[str, Any]:
    """Kick ticker cycle(s) synchronously, bypassing the schedule.

    `count` (default 1) fires N back-to-back cycles. Useful for
    bootstrapping past the 6-reading insufficient-history floor in
    one click. Capped at 20 so a copy-paste typo can't drain the
    Brave / LLM budget.

    Auth-optional by operator preference (heavy testing iterates on
    this endpoint). The cycle costs real LLM + Brave budget though,
    so the audit log captures who triggered it + the burst size.
    """
    from sca.movement.ticker import _tick_once
    n = max(1, min(count, 20))
    summaries = []
    for _ in range(n):
        summaries.append(_tick_once())
    by = (user or {}).get("user", {}).get("id") if user else "anonymous"
    log_event(
        "movement.tick.manual", level="info",
        by=by, burst=n,
        symbols=len(summaries[-1].get("per_symbol", [])),
    )
    # Return the last cycle's full summary plus a roll-up so a
    # caller iterating on the UI can see what landed across the
    # burst without having to parse N nested objects.
    last = summaries[-1]
    emitted_total = sum(
        sum(1 for v in (s.get("emitted") or {}).values() if v)
        for cycle in summaries
        for s in (cycle.get("per_symbol") or [])
    )
    graded_total = sum(
        (cycle.get("resolver") or {}).get("graded", 0)
        for cycle in summaries
    )
    last["burst"] = {
        "cycles": n,
        "emitted_total": emitted_total,
        "graded_total": graded_total,
    }
    return last


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


@app.get("/compendium")
def compendium_page() -> FileResponse:
    """Standalone Compendium document — lives outside the SPA so it can
    be opened in its own tab as a sharable, printable docs page."""
    return FileResponse(STATIC_DIR / "compendium.html", headers=_NO_CACHE)


@app.get("/static/{name}")
def static_asset(name: str) -> FileResponse:
    # Served by hand (not StaticFiles) so dev edits are never cached stale.
    path = (STATIC_DIR / name).resolve()
    if not path.is_file() or STATIC_DIR not in path.parents:
        raise HTTPException(404, "not found")
    return FileResponse(path, headers=_NO_CACHE)


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
