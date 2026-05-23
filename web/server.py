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
    return _asdict(result)


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


@app.get("/api/evals")
def evals(
    _rate: None = Depends(rate_limit_enforce),
) -> dict[str, Any]:
    """Run the eval harness. This is slow (one analyze() per case).

    Rate-limited: each case invokes the LLM and live RPCs; an uncapped
    caller could burn the LLM quota in minutes.
    """
    from sca.evals import run_evals

    try:
        results = run_evals()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"eval run failed: {exc}") from exc
    cases = []
    for r in results:
        cases.append(
            {
                "case_id": r.case_id,
                "symbol": r.symbol,
                "passed": r.passed,
                "points": [_asdict(p) for p in r.points],
            }
        )
    passed = sum(1 for c in cases if c["passed"])
    return {"cases": cases, "count": len(cases), "passed": passed}


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

    return {
        "attestations": attestations,
        "sources_health": sources_health,
        "supply_history": history_snapshot,
        "discoveries": discoveries,
        "events": events,
        "verified_facts": facts,
        "discovery_provider": os.environ.get(
            "SCA_WEB_SEARCH_PROVIDER", "",
        ).strip().lower() or None,
    }


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
