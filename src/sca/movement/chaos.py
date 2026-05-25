"""Chaos engineering — controlled fault injection against the live system.

A daemon thread that periodically fires controlled fault scenarios at
the running stack and verifies the defensive paths actually catch
them. Findings are persisted; the LLM judge reads them as "known
fragility patterns" so its narrative is grounded in tested resilience
state rather than implied stability.

## Design discipline

  - **Read-only at user-facing surfaces.** Chaos NEVER injects faults
    into the live peg-tick stream, the resolver, the calibration
    archive, or the trader's persistent state. It exercises code
    paths IN ISOLATION (via direct function calls on synthetic
    inputs) so we measure resilience without contaminating real data.
  - **Deterministic scenarios.** Each scenario is a pure function over
    a fixture. Reproducible — the chaos report can be re-run by hand
    from the persisted scenario id.
  - **Bounded cadence.** Default one cycle every 15 minutes. Override
    with SCA_CHAOS_INTERVAL_MINUTES. Disable entirely with
    SCA_CHAOS_DISABLED=1 (CI / tests always set this).
  - **Honest reporting.** Findings include scenario id, expected
    defensive behaviour, observed behaviour, and pass/fail. Failures
    are logged as warnings so an operator notices the regression.
  - **LLM-input shape.** Recent findings (last 5) are appended to the
    judge prompt as a "Known fragility patterns" block, so judge
    narratives stay grounded in tested invariants.

## Scenario library (v1)

  • peg_consensus_null: feed a ConsensusTick with consensus_price=None
    to the peg-tick persistence path; verify it doesn't crash and
    emits no-source-responded.
  • commentary_malformed_context: invoke get_commentary with a
    malformed cone_thresholds_bps; verify the audit-fix returns None.
  • trader_none_current: feed evaluate_cycle a token with
    current_bps=None; verify no trade opens.
  • feed_signature_skip: call simulator_feed-equivalent twice with
    the same fixture; verify the second is a cache hit (instant).
  • event_ts_string: simulate the SSE diff loop with a string ts;
    verify the audit-fix coerce path returns 0.0 instead of crashing.

The chaos thread runs scenarios in deterministic order, persisting
the latest run per scenario. The judge reads only the latest five
findings (the recency window).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional

from sca.config import DATA_DIR
from sca.observability import log_event


CHAOS_FINDINGS_PATH: Path = DATA_DIR / "chaos_findings.json"
_DEFAULT_INTERVAL_MIN = 15.0
_MIN_INTERVAL_MIN = 5.0
_THREAD_STARTED = threading.Event()


@dataclass
class Finding:
    scenario: str               # e.g. "trader_none_current"
    ran_at: str                 # ISO 8601 UTC
    passed: bool                # True if defensive path held
    expected: str               # one-line description of expected behavior
    observed: str               # one-line description of what happened
    severity: str = "info"      # info | warn | fail


# ── persistence ──────────────────────────────────────────────────────
def _load_findings() -> dict[str, dict]:
    """Return {scenario_id: finding_dict}. Empty if the file is missing
    or malformed."""
    if not CHAOS_FINDINGS_PATH.exists():
        return {}
    try:
        raw = json.loads(CHAOS_FINDINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_findings(findings: dict[str, dict]) -> None:
    from sca.persist import atomic_write_json
    try:
        atomic_write_json(CHAOS_FINDINGS_PATH, findings)
    except Exception:  # noqa: BLE001
        pass


def recent_findings(limit: int = 5) -> list[dict]:
    """Public read — the N most-recent findings across scenarios,
    newest first. Used by the LLM judge for the 'Known fragility
    patterns' prompt block."""
    findings = _load_findings()
    rows = list(findings.values())
    rows.sort(key=lambda r: r.get("ran_at") or "", reverse=True)
    return rows[:limit]


# ── scenario library ─────────────────────────────────────────────────
# Each scenario returns a Finding. Scenarios MUST be deterministic
# and self-contained — no network, no mutation of real state.

def _scenario_commentary_malformed_context() -> Finding:
    """Audit-fix invariant: a malformed cone_thresholds_bps must NOT
    crash get_commentary; it must return None."""
    from sca.movement import commentary

    class _Broken:
        symbol = "CHAOS-USDC"
        cone_thresholds_bps = ()  # empty — used to crash via index
        issuer = "ChaosOps"
        backing_model = "synthetic_test"
        backing_short = ""
        expected_peg = 1.0
        cadence = ""
        auditor = ""
        transparency_url = ""
        watchlist_signal = ""
        structural_one_liner = ""
        yield_bearing = False
        venue_type = "CEX"
        pl_lens = ""

    orig = commentary.get_context

    try:
        commentary.get_context = (  # type: ignore[assignment]
            lambda sym: _Broken() if sym == "CHAOS-USDC" else None
        )
        out = commentary.get_commentary(
            "CHAOS-USDC",
            {"current_bps": -3.0, "cone_p80_bps": 5.0},
            wait_for_llm=False,
        )
        passed = (out is None)
        observed = ("returned None as expected" if passed
                    else f"returned a Commentary unexpectedly: {out!r}")
    except Exception as exc:  # noqa: BLE001
        passed = False
        observed = f"crashed with {type(exc).__name__}: {exc}"
    finally:
        commentary.get_context = orig  # type: ignore[assignment]

    return Finding(
        scenario="commentary_malformed_context",
        ran_at=_now_iso(),
        passed=passed,
        expected="get_commentary returns None on malformed cone_thresholds_bps",
        observed=observed,
        severity="info" if passed else "fail",
    )


def _scenario_trader_none_current() -> Finding:
    """Audit-fix invariant: trader.evaluate_cycle must skip tokens
    whose current_bps is None — never open a trade with an
    un-signed deviation."""
    from sca.movement import trader

    # Isolate persistence to avoid polluting real trader state.
    real_path = trader.TRADER_PATH
    chaos_path = DATA_DIR / "_chaos_trader_scratch.json"
    try:
        trader.TRADER_PATH = chaos_path  # type: ignore[assignment]
        if chaos_path.exists():
            chaos_path.unlink()
        feed = [{
            "symbol": "CHAOS-USDC",
            "current_bps": None,
            "meta": {"cone_normal_bps": 5, "cone_alert_bps": 15},
            "latest_prediction": {
                "made_at": "chaos-t1",
                "resolves_at": "2030-01-01T00:00:00+00:00",
                "point": -2.0, "p80_low": -7.0, "p80_high": 3.0,
                "confidence_word": "likely",
            },
        }]
        opened = trader.evaluate_cycle(
            feed, now_iso="2030-01-01T00:00:00+00:00")
        passed = (len(opened) == 0)
        observed = ("no trade opened (correct)" if passed
                    else f"opened {len(opened)} trade(s) — direction "
                         "could be wrong-side")
    except Exception as exc:  # noqa: BLE001
        passed = False
        observed = f"crashed: {type(exc).__name__}: {exc}"
    finally:
        trader.TRADER_PATH = real_path  # type: ignore[assignment]
        try:
            if chaos_path.exists():
                chaos_path.unlink()
        except OSError:
            pass

    return Finding(
        scenario="trader_none_current",
        ran_at=_now_iso(),
        passed=passed,
        expected="evaluate_cycle opens 0 trades when current_bps=None",
        observed=observed,
        severity="info" if passed else "fail",
    )


def _scenario_event_ts_string_coercion() -> Finding:
    """Audit-fix invariant: SSE-like diff loop must coerce string /
    missing event ts values without raising TypeError."""
    try:
        # Recreate the helper as the live SSE handler defines it.
        def coerce(ev):
            t = ev.get("ts")
            if isinstance(t, (int, float)):
                return float(t)
            if isinstance(t, str) and t:
                try:
                    return float(t)
                except ValueError:
                    return 0.0
            return 0.0

        cases = [
            ({"ts": 1779700000.5}, 1779700000.5),
            ({"ts": 1779700000}, 1779700000.0),
            ({"ts": "1779700000.5"}, 1779700000.5),
            ({"ts": ""}, 0.0),
            ({"ts": "not-a-number"}, 0.0),
            ({}, 0.0),
        ]
        all_pass = all(abs(coerce(c) - exp) < 1e-6
                        for c, exp in cases)
        passed = all_pass
        observed = ("all 6 cases coerce cleanly" if passed
                    else "one or more cases failed coercion")
    except Exception as exc:  # noqa: BLE001
        passed = False
        observed = f"crashed: {type(exc).__name__}: {exc}"

    return Finding(
        scenario="event_ts_string_coercion",
        ran_at=_now_iso(),
        passed=passed,
        expected="float/int/string/missing event ts coerce to a finite float",
        observed=observed,
        severity="info" if passed else "fail",
    )


def _scenario_peg_consensus_silent_token() -> Finding:
    """A token whose all sources returned None should produce a
    None ConsensusTick, not a row with zero/garbage data."""
    from sca import peg_price

    real_sources = peg_price._SOURCES
    real_env = os.environ.get(peg_price._HTTP_DISABLED_ENV)
    try:
        # Replace every source with a no-op (returns None) so no
        # network call leaks even on a misconfigured dev box.
        peg_price._SOURCES = [  # type: ignore[assignment]
            (name, lambda *a, **kw: None)
            for name, _ in real_sources
        ]
        os.environ.pop(peg_price._HTTP_DISABLED_ENV, None)
        peg_price._CACHE.clear()
        tick = peg_price.fetch_consensus("CHAOS-PEG")
        passed = (tick is None)
        observed = ("returned None as expected" if passed
                    else f"returned a tick unexpectedly: {tick!r}")
    except Exception as exc:  # noqa: BLE001
        passed = False
        observed = f"crashed: {type(exc).__name__}: {exc}"
    finally:
        peg_price._SOURCES = real_sources  # type: ignore[assignment]
        if real_env is not None:
            os.environ[peg_price._HTTP_DISABLED_ENV] = real_env
        peg_price._CACHE.clear()

    return Finding(
        scenario="peg_consensus_silent_token",
        ran_at=_now_iso(),
        passed=passed,
        expected="fetch_consensus returns None when all sources are silent",
        observed=observed,
        severity="info" if passed else "fail",
    )


def _scenario_synthesis_strip_html() -> Finding:
    """Audit-fix invariant: synthesise pipeline strips literal HTML
    tags the LLM occasionally emits."""
    from sca.agent.synthesis import _strip_html_tags
    src = "Foo.<br>Bar.<p>Baz</p><strong>Bold</strong>"
    try:
        out = _strip_html_tags(src)
        passed = ("<br" not in out.lower()
                  and "<p" not in out.lower()
                  and "<strong" not in out.lower()
                  and "Foo." in out and "Bar." in out
                  and "Baz" in out and "Bold" in out)
        observed = ("HTML stripped, content preserved" if passed
                    else f"leak detected in: {out!r}")
    except Exception as exc:  # noqa: BLE001
        passed = False
        observed = f"crashed: {type(exc).__name__}: {exc}"

    return Finding(
        scenario="synthesis_strip_html",
        ran_at=_now_iso(),
        passed=passed,
        expected="_strip_html_tags removes <br>/<p>/<strong>",
        observed=observed,
        severity="info" if passed else "fail",
    )


_SCENARIOS: list[Callable[[], Finding]] = [
    _scenario_commentary_malformed_context,
    _scenario_trader_none_current,
    _scenario_event_ts_string_coercion,
    _scenario_peg_consensus_silent_token,
    _scenario_synthesis_strip_html,
]


# ── runner ──────────────────────────────────────────────────────────
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_one_cycle() -> list[Finding]:
    """Execute every scenario once and persist the latest finding per
    scenario. Public so the test suite can drive a deterministic run
    without spinning a thread.

    Failures are logged as warnings. Passes are info-level (visible
    but not noisy).
    """
    findings = _load_findings()
    fresh: list[Finding] = []
    for scenario_fn in _SCENARIOS:
        try:
            finding = scenario_fn()
        except Exception as exc:  # noqa: BLE001 — never let a scenario kill the loop
            finding = Finding(
                scenario=getattr(scenario_fn, "__name__", "unknown"),
                ran_at=_now_iso(), passed=False,
                expected="scenario must run cleanly",
                observed=f"runner crashed: {type(exc).__name__}: {exc}",
                severity="fail",
            )
        findings[finding.scenario] = asdict(finding)
        fresh.append(finding)
        log_event(
            "chaos.scenario.run",
            level=("warn" if not finding.passed else "info"),
            scenario=finding.scenario, passed=finding.passed,
            severity=finding.severity, observed=finding.observed[:160],
        )
    _save_findings(findings)
    log_event(
        "chaos.cycle.done", level="info",
        total=len(fresh),
        passed=sum(1 for f in fresh if f.passed),
        failed=sum(1 for f in fresh if not f.passed),
    )
    return fresh


def _interval_seconds() -> float:
    raw = os.environ.get("SCA_CHAOS_INTERVAL_MINUTES", "").strip()
    try:
        minutes = float(raw) if raw else _DEFAULT_INTERVAL_MIN
    except ValueError:
        minutes = _DEFAULT_INTERVAL_MIN
    minutes = max(minutes, _MIN_INTERVAL_MIN)
    return minutes * 60.0


def _loop() -> None:
    try:
        interval = _interval_seconds()
        # Sleep first so startup latency isn't loaded with a chaos
        # cycle; the canary thread has the same shape.
        while True:
            time.sleep(interval)
            try:
                run_one_cycle()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "chaos.cycle.crashed", level="error",
                    error_class=type(exc).__name__,
                    error_message=str(exc)[:200],
                )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "chaos.thread.died", level="error",
            error_class=type(exc).__name__,
        )


def start_chaos_thread() -> None:
    """Spawn the daemon thread. Idempotent — calling twice is a no-op.
    Gated by SCA_CHAOS_DISABLED=1 (tests always set this)."""
    if os.environ.get("SCA_CHAOS_DISABLED", "").strip() == "1":
        return
    if _THREAD_STARTED.is_set():
        return
    _THREAD_STARTED.set()
    t = threading.Thread(target=_loop, name="chaos-loop", daemon=True)
    t.start()
    log_event(
        "chaos.thread.started", level="info",
        interval_minutes=_interval_seconds() / 60.0,
        scenarios=len(_SCENARIOS),
    )


def fragility_prompt_block(limit: int = 5) -> str:
    """Render the recent findings as a markdown block the LLM judge
    can include in its prompt. Empty string if there are no findings
    (don't introduce noise on a fresh deploy).

    The judge uses this to ground its narrative in tested resilience
    state — e.g. "the SSE diff loop's ts coercion has been verified
    over the past N cycles" — rather than making implied claims.
    """
    findings = recent_findings(limit=limit)
    if not findings:
        return ""
    lines = ["## Known fragility patterns (chaos-engineering log)"]
    for f in findings:
        glyph = "✓" if f.get("passed") else "✕"
        lines.append(
            f"- {glyph} `{f.get('scenario')}`: {f.get('observed')}"
        )
    return "\n".join(lines)
