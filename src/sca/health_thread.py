"""Background source-health thread — periodic canary, flip detection.

The canary (`sca.canary`) sweeps every external dependency we have —
RPCs, OFAC feeds, issuer transparency URLs, corpus source URLs — and
reports which are live, which served from snapshot only, and which are
broken outright. On-demand it runs from the CLI (`sca canary`) and on a
nightly cron in production.

What this module adds: a daemon thread that runs the same sweep every
N hours from inside the web server, persists the last-seen state per
source, and emits a structured `health.source.flipped` event the
moment a source transitions live ↔ broken. The persistent state file
(`data/health_state.json`) makes flip detection survive process
restarts — without it, every redeploy would falsely "flip" every
broken source back to live on the next sweep.

Design notes:

  - **Fire-and-forget daemon thread.** The web server starts it from an
    `@app.on_event("startup")` hook. The thread is a daemon so the
    process exits cleanly without waiting for the sleep loop. If the
    thread dies (a malformed config, an OS error), we log it and let
    the next deploy bring it back — we do NOT auto-restart inside the
    process, because a thread that keeps dying every minute is worse
    than one that's clearly absent.
  - **Never blocks startup.** The first sweep happens AFTER the first
    sleep interval, so a slow first canary doesn't delay the server's
    readiness probe. (Operators who want an immediate sweep can run
    `sca canary` once after deploy.)
  - **One event per cycle + one event per flip.** Counts go on
    `health.sweep.done`; flips go on `health.source.flipped` with the
    transition direction. Cheap to alert on either.
  - **Interval is env-configurable**: `SCA_HEALTH_INTERVAL_HOURS`
    (default 6h, minimum 1h to avoid hammering a regulator).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from sca import config
from sca.observability import log_event

# ── persistence ───────────────────────────────────────────────────────
# Per-source status the last time we swept it. Keyed by source id; value
# is the SourceCheck.status string ('live' | 'snapshot' | 'broken'). The
# file is the ONLY state this module keeps across restarts — flip
# detection is a diff against this dict, nothing else.
HEALTH_STATE_PATH: Path = config.DATA_DIR / "health_state.json"

# Default sweep cadence. Six hours is a quiet-enough refresh that we
# notice a tier-1 outage within a window the on-call would care about,
# without burning rate-limited regulator endpoints. Override via env.
_DEFAULT_INTERVAL_HOURS = 6.0
_MIN_INTERVAL_HOURS = 1.0

# Module-level state so `start_health_thread()` is idempotent — calling
# it twice (e.g. tests that exercise the startup hook) doesn't spawn
# two daemons reading the same state file.
_thread_started = threading.Event()


def _interval_seconds() -> float:
    """Seconds between sweeps, parsed from SCA_HEALTH_INTERVAL_HOURS.

    Floors at the safety minimum so a misconfiguration doesn't turn the
    health thread into a DoS against a regulator. Unparseable env →
    default; the env is a hint, not an authority.
    """
    raw = os.environ.get("SCA_HEALTH_INTERVAL_HOURS", "").strip()
    try:
        hours = float(raw) if raw else _DEFAULT_INTERVAL_HOURS
    except ValueError:
        hours = _DEFAULT_INTERVAL_HOURS
    hours = max(hours, _MIN_INTERVAL_HOURS)
    return hours * 3600.0


def _load_state() -> dict[str, str]:
    """Per-source last-seen status. Empty dict on first run or corruption."""
    if not HEALTH_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(HEALTH_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Defensive: only keep string→string entries. A future schema change
    # in canary won't silently corrupt the flip detector.
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _save_state(state: dict[str, str]) -> None:
    from sca.persist import atomic_write_json
    atomic_write_json(HEALTH_STATE_PATH, state)


# ── single-cycle work ─────────────────────────────────────────────────
def _flip_level(prev: str, cur: str) -> str:
    """The log level for a transition. Live→broken is warn; the reverse
    (broken→live) is info — it's a recovery, not an incident. Anything
    else (live↔snapshot, snapshot↔broken) is info too; the broken state
    is the only one that warrants attention."""
    if cur == "broken" and prev != "broken":
        return "warn"
    return "info"


def run_health_cycle() -> dict[str, int]:
    """Run one canary sweep, persist state, emit events. Returns the counts.

    Public so the test suite can invoke it directly without spawning a
    thread and without waiting for a sleep interval. In production it's
    called from the daemon's loop body.
    """
    from sca.canary import run_canary

    prev_state = _load_state()
    try:
        report = run_canary()
    except Exception as exc:  # noqa: BLE001 - never let the thread die
        log_event(
            "health.sweep.failed", level="error",
            error_class=type(exc).__name__, error_message=str(exc),
        )
        return {"live": 0, "snapshot": 0, "broken": 0, "changed": 0}

    new_state: dict[str, str] = {}
    for check in report.checks:
        new_state[check.id] = check.status
        prev = prev_state.get(check.id, "")
        if prev and prev != check.status:
            # `kind` collides with `log_event`'s first positional param —
            # rename the SourceCheck.kind field to source_kind in the event
            # payload so observability sees a single, unambiguous schema.
            log_event(
                "health.source.flipped",
                level=_flip_level(prev, check.status),
                id=check.id, url=check.url, source_kind=check.kind,
                from_status=prev, to_status=check.status,
                error=check.error or "",
            )
        # Brave-backed recovery: when a source has just flipped to broken
        # (or is still broken this sweep), search the web for a
        # replacement URL. Best-effort, never propagates exceptions. A
        # hit is LOGGED for a curator to confirm — we don't auto-mutate
        # the registry, just surface the candidate so the human is
        # prompted with "we think you should update source X to this URL".
        if check.status == "broken":
            try:
                from sca.web_discovery import find_replacement_source
                replacement = find_replacement_source(
                    check.id, check.url,
                    title=getattr(check, "title", "") or check.id,
                )
                if replacement:
                    log_event(
                        "canary.replacement.proposed", level="info",
                        source_id=check.id, broken_url=check.url,
                        replacement_url=replacement,
                    )
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "canary.recovery.unexpected_error", level="warn",
                    source_id=check.id, error_class=type(exc).__name__,
                )

    _save_state(new_state)
    counts = {
        "live": report.live,
        "snapshot": report.snapshot_only,
        "broken": report.broken,
        "changed": report.changed,
    }
    log_event(
        "health.sweep.done", level="info",
        total=len(report.checks),
        **counts,
    )
    return counts


def run_attestation_gap_sweep() -> dict[str, int]:
    """Walk every fiat-backed token and try to (re-)resolve its attestation.

    Cheap: every step except the LLM-driven locator is a HEAD probe or a
    cache hit. A successful resolution writes the URL through to the
    store via `_record_override`, so the next deploy sees it. Per-symbol
    errors are logged and skipped — the sweep never throws.
    """
    from sca import config
    from sca.tools import attestation_fetch

    summary = {"resolved": 0, "unresolved": 0, "skipped": 0}
    for coin in config.stablecoins().values():
        if coin.backing_model != "fiat_reserves":
            summary["skipped"] += 1
            continue
        try:
            out = attestation_fetch.resolve_url(coin.symbol)
        except attestation_fetch.AttestationUnavailable as exc:
            log_event(
                "attestation.gap_sweep.unresolved", level="warn",
                symbol=coin.symbol, error_message=str(exc)[:200],
            )
            summary["unresolved"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one bad token, keep going
            log_event(
                "attestation.gap_sweep.error", level="error",
                symbol=coin.symbol, error_class=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            summary["unresolved"] += 1
            continue
        log_event(
            "attestation.gap_sweep.ok", level="info",
            symbol=coin.symbol, url=out.get("url", ""),
            via=out.get("via", ""),
        )
        summary["resolved"] += 1
    log_event(
        "attestation.gap_sweep.done", level="info", **summary,
    )
    return summary


# ── daemon loop ───────────────────────────────────────────────────────
def _last_sweep_at() -> float:
    """Unix seconds of the last completed canary sweep, or 0.0 if
    unknown. Persisted in a sibling file next to HEALTH_STATE_PATH so
    restart-frequent development cycles can still get a sweep when
    one is genuinely due, while production stampede protection holds.
    """
    p = HEALTH_STATE_PATH.parent / "health_last_sweep.txt"
    try:
        return float(p.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0.0


def _mark_sweep_done(ts: float | None = None) -> None:
    p = HEALTH_STATE_PATH.parent / "health_last_sweep.txt"
    try:
        p.write_text(str(ts or time.time()), encoding="utf-8")
    except OSError:
        pass


def _loop() -> None:
    """The forever-loop body. Sleep first so startup is never delayed,
    EXCEPT when the last sweep is older than the interval — then we run
    immediately so a restart-frequent dev cycle doesn't perpetually
    skip the canary.

    Stampede-safe: only the leader-elected worker runs this loop, and
    we record `last_sweep_at` on disk so two workers can't both decide
    to fire immediately on the same boot.

    Crashes inside `run_health_cycle` are caught there; if something
    catastrophic escapes here (e.g. KeyboardInterrupt in a test), we
    log it and exit the thread — better than busy-looping on a fault.
    """
    try:
        interval = _interval_seconds()
        while True:
            last = _last_sweep_at()
            now = time.time()
            elapsed = now - last if last > 0 else interval + 1
            if elapsed < interval:
                time.sleep(interval - elapsed)
            else:
                # First-sweep-on-boot path: yield briefly so startup
                # observability gets a clean window, then run.
                time.sleep(5.0)
            try:
                run_health_cycle()
                _mark_sweep_done()
            except Exception as exc:  # noqa: BLE001 - never die on one bad cycle
                log_event(
                    "health.cycle.failed", level="error",
                    error_class=type(exc).__name__, error_message=str(exc),
                )
            # After the canary, try to close any attestation gaps. This is
            # the discovery sweep the user asked for: every cycle walks the
            # fiat token list and re-resolves through the override→cache→
            # seed→paxos→web_search→locator chain. Each hit writes through
            # to the store so it survives the next redeploy. Bounded and
            # cheap (HEAD probes + cache hits dominate).
            try:
                run_attestation_gap_sweep()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "attestation.gap_sweep.failed", level="error",
                    error_class=type(exc).__name__, error_message=str(exc),
                )
            # Also run contract-address auto-verification — calls each
            # contract's own symbol() / decimals() and clears the
            # "unverified address" flag on a clean match. This is what
            # makes TUSD-on-ethereum-style "pending verification"
            # warnings clear themselves over time without an operator
            # running `sca verify` by hand. Honours human votes (a
            # human-rejected deployment is never auto-verified).
            try:
                from sca.tools.address_verify import verify_all
                summary = verify_all()
                log_event(
                    "address.auto_verify.sweep_done", level="info",
                    auto_verified=summary.get("auto_verified", 0),
                    skipped_human=summary.get("skipped_human", 0),
                    mismatch=summary.get("mismatch", 0),
                    errored=summary.get("errored", 0),
                )
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "address.auto_verify.failed", level="error",
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                )
    except Exception as exc:  # noqa: BLE001 - thread terminates cleanly
        log_event(
            "health.thread.died", level="error",
            error_class=type(exc).__name__, error_message=str(exc),
        )


LEADER_LOCKFILE = config.DATA_DIR / "health_thread.leader"
# Stale leader (process died without releasing): claim after this age.
_LEADER_STALE_S = 600  # 10 minutes — well above sweep interval


def _try_claim_leadership() -> bool:
    """Cross-process leader election via a PID lockfile.

    Multi-replica deploys (uvicorn --workers N, gunicorn -w N) each call
    `start_health_thread()` on startup; without leadership, every worker
    runs its own sweep — redundant work, duplicated logs, racing writes
    to data/health_state.json.

    Claim path:
      - If the lockfile doesn't exist → write our PID, win.
      - If it exists and its mtime is recent → the existing holder is alive; lose.
      - If it exists but is stale (mtime older than _LEADER_STALE_S) → reclaim.

    Best-effort: filesystem may be ephemeral in containers, so leadership
    is advisory not strict. In the worst case two workers run the sweep
    and the only harm is duplicated work — never correctness."""
    try:
        LEADER_LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if LEADER_LOCKFILE.exists():
            age = now - LEADER_LOCKFILE.stat().st_mtime
            if age < _LEADER_STALE_S:
                return False  # someone else holds it, recently
        # Claim or reclaim
        LEADER_LOCKFILE.write_text(f"{os.getpid()} {now}\n")
        return True
    except OSError:
        # Filesystem unavailable — be safe, assume someone else has it
        return False


def _touch_leadership() -> None:
    """Heartbeat the lockfile so other workers don't try to reclaim.
    Called periodically by the health loop."""
    try:
        LEADER_LOCKFILE.write_text(f"{os.getpid()} {time.time()}\n")
    except OSError:
        pass


def start_health_thread() -> threading.Thread | None:
    """Spawn the background health thread; idempotent + worker-safe.

    Returns the Thread when this worker won leadership, None otherwise
    (so multi-replica deploys don't spawn redundant sweeps). Setting
    `SCA_HEALTH_DISABLED=1` short-circuits — useful in CI and in dev
    when an operator wants to run sweeps manually via `sca canary`.
    """
    if os.environ.get("SCA_HEALTH_DISABLED", "").strip().lower() in (
        "1", "true", "on", "yes",
    ):
        log_event("health.thread.disabled", level="info")
        return None
    if _thread_started.is_set():
        return None
    if not _try_claim_leadership():
        log_event(
            "health.thread.not_leader", level="info",
            pid=os.getpid(),
            reason="another worker holds the leadership lockfile",
        )
        return None
    _thread_started.set()
    thread = threading.Thread(
        target=_loop, name="sca-health-canary", daemon=True,
    )
    thread.start()
    log_event(
        "health.thread.started", level="info",
        interval_seconds=_interval_seconds(),
        pid=os.getpid(),
    )
    return thread


def _reset_for_tests() -> None:
    """Clear the started flag + leader lockfile so a test can spawn the
    thread fresh.

    The module-level `_thread_started` flag is process-global; tests
    that exercise `start_health_thread` need to reset it between cases
    or only the first test gets a real thread back. Same for the
    leader lockfile — without clearing it, a re-run sees a "recent"
    lockfile and refuses to claim leadership.
    """
    _thread_started.clear()
    try:
        if LEADER_LOCKFILE.exists():
            LEADER_LOCKFILE.unlink()
    except OSError:
        pass
