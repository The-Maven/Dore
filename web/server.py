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

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from sca import config
from sca.agent import analyze, assess_redemption, screen_token
from sca.corpus.sources import all_sources
from sca.store import get_store
from sca.tools import get_onchain_supply
from web.auth import current_user, require_user

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
_CACHE_FRESH_S = 10 * 60


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
    """Most recent completed analysis for a token, if computed < 10min ago.

    Returns the instant-serve payload (status done, result, cached flag,
    computed_at) or None when there is nothing fresh to serve. Shared across
    all callers — the lookup is intentionally not scoped by user. Never
    raises: a store hiccup just falls through to running a real job.
    """
    try:
        rows = get_store().list_analyses(surface=surface, symbol=symbol, limit=1)
    except Exception:  # noqa: BLE001 - never break on a store hiccup
        return None
    if not rows:
        return None
    row = rows[0]
    if row.get("status") != "done" or not row.get("result"):
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


# ── async analysis jobs ───────────────────────────────────────────────
# An analysis hits live RPCs + a PDF download + an LLM (~10-40s). We run
# it in a background thread and let the client poll, so the UI can show a
# real, staged progress experience instead of a frozen request.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

_STAGES = [
    "Resolving on-chain supply across deployments",
    "Locating + downloading the latest attestation",
    "Extracting reserves from the attestation PDF",
    "Running deterministic guardrail checks",
    "Retrieving the approved corpus reasoning frame",
    "Synthesising the cited compliance analysis",
]


def _run_job(
    job_id: str, symbol: str, refresh: bool = False, user_id: str | None = None
) -> None:
    started = time.time()
    try:
        # user_id attributes the persisted analysis row to the signed-in
        # user; None (anonymous) means the run is transient.
        result = analyze(symbol, refresh=refresh, user_id=user_id)
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
) -> dict[str, Any]:
    """Kick off an analysis job. Returns a job id to poll.

    Pass ``refresh: true`` to bypass the analysis cache and force a full
    recompute (live RPCs + LLM). Omitted/false reuses the cached result,
    which is instant and free.

    Anonymous callers run the analysis fully — only persistence to history
    requires an account, so the run is attributed only when ``user`` is set.
    """
    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol is required")
    if symbol not in config.stablecoins():
        raise HTTPException(404, f"unknown stablecoin: {symbol}")
    refresh = bool(body.get("refresh", False))
    # A fresh, already-computed result is served instantly — no job, no
    # staged motions. An explicit refresh always bypasses this.
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
    threading.Thread(
        target=_run_job,
        args=(job_id, symbol, refresh, _uid(user)),
        daemon=True,
    ).start()
    return {"job_id": job_id, "symbol": symbol, "stages": _STAGES}


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
    "Retrieving the approved corpus reasoning frame",
    "Synthesising the cited sanctions screen",
]
_REDEMPTION_STAGES = [
    "Resolving on-chain supply across deployments",
    "Locating + downloading the latest attestation",
    "Classifying reserves into liquidity tiers",
    "Running deterministic guardrail checks",
    "Retrieving the approved corpus reasoning frame",
    "Synthesising the cited redemption assessment",
]


def _run_surface_job(
    job_id: str, symbol: str, fn: Any, user_id: str | None = None
) -> None:
    """Run a slow compliance-surface job (sanctions or redemption)."""
    started = time.time()
    try:
        result = fn(symbol, user_id=user_id)
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
    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol is required")
    if symbol not in config.stablecoins():
        raise HTTPException(404, f"unknown stablecoin: {symbol}")
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
    threading.Thread(
        target=_run_surface_job,
        args=(job_id, symbol, fn, user_id),
        daemon=True,
    ).start()
    return {"job_id": job_id, "symbol": symbol, "stages": stages}


@app.post("/api/sanctions")
def start_sanctions(
    body: dict[str, Any],
    user: dict[str, Any] | None = Depends(current_user),
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
    """The corpus source registry + the human-curation gate state."""
    items = [_asdict(s) for s in all_sources()]
    for s, raw in zip(items, all_sources()):
        s["approved"] = raw.approved
    approved = sum(1 for s in items if s["approved"])
    return {"sources": items, "count": len(items), "approved": approved}


@app.post("/api/sources/{source_id}/vote")
def vote_source(
    source_id: str,
    body: dict[str, Any],
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Record a human curation decision on a corpus source.

    Mirrors ``sca curate``: records the decision, clears the lru_cached
    source loader, and — on an ``approved`` decision — ingests any staged
    file so the source becomes citeable.

    Curation is a save-type action: it requires an account. An anonymous
    caller gets a 401 from ``require_user`` (the SPA turns this into a
    friendly "sign in to save" prompt). The vote is attributed to the user.
    """
    decision = str(body.get("decision", "")).strip().lower()
    if decision not in {"approved", "rejected"}:
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")

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
    if decision == "approved":
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
            updated["approved"] = s.approved
            break

    return {
        "ok": True,
        "source": updated,
        "ingested": ingested,
        "ingest_error": ingest_error,
    }


@app.get("/api/supply/{symbol}")
def supply(symbol: str) -> dict[str, Any]:
    """Live on-chain supply only — fast, no LLM, no attestation."""
    symbol = symbol.strip().upper()
    if symbol not in config.stablecoins():
        raise HTTPException(404, f"unknown stablecoin: {symbol}")
    try:
        result = get_onchain_supply(symbol, allow_unverified=True)
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
    symbol = str(body.get("symbol", "")).strip().upper()
    chain = str(body.get("chain", "")).strip()
    decision = str(body.get("decision", "")).strip().lower()
    if not symbol or symbol not in config.stablecoins():
        raise HTTPException(404, f"unknown stablecoin: {symbol}")
    if decision not in {"verified", "rejected"}:
        raise HTTPException(400, "decision must be 'verified' or 'rejected'")
    coin = config.stablecoins()[symbol]
    match = next((d for d in coin.deployments if d.chain == chain), None)
    if match is None:
        raise HTTPException(404, f"unknown deployment: {symbol} on {chain}")
    get_store().record_address_decision(
        symbol, chain, match.contract, decision, user_id=user["user"]["id"]
    )
    return {"ok": True, "symbol": symbol, "chain": chain, "decision": decision}


@app.get("/api/evals")
def evals() -> dict[str, Any]:
    """Run the eval harness. This is slow (one analyze() per case)."""
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


def _hermes_binary() -> str | None:
    """Path to the `hermes` runtime binary, or None when not installed."""
    return shutil.which("hermes")


@app.post("/api/agent")
def agent(body: dict[str, Any]) -> dict[str, Any]:
    """Relay one turn to Doré's conversational analyst (the Hermes agent).

    Takes the user's plain-language message, invokes the Hermes runtime in
    a non-interactive prompt mode, and returns the analyst's reply plus any
    tool-call trace the runtime surfaced.

    The Hermes runtime is a *separate* deploy artefact (see the repo
    Dockerfile). When its binary is absent the bridge does not crash — it
    returns ``{"status": "runtime_offline"}`` so the console can render its
    designed offline state. This is the expected response until deployment.
    """
    message = str(body.get("message", "")).strip()
    if not message:
        raise HTTPException(400, "message is required")

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
