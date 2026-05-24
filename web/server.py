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
        if per_chain is not None and not isinstance(per_chain, (list, dict)):
            per_chain = _asdict(per_chain)
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
        return get_store().calibration_summary(
            symbol=symbol, kind=kind, horizon_minutes=horizon_minutes,
        )
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
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Curator endpoint: kick a ticker cycle synchronously, bypassing
    the schedule. Useful for ops + smoke tests; the response is the
    cycle summary so callers see exactly what landed."""
    from sca.movement.ticker import _tick_once
    summary = _tick_once()
    log_event(
        "movement.tick.manual", level="info",
        by=user["user"]["id"],
        symbols=len(summary.get("per_symbol", [])),
    )
    return summary


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
