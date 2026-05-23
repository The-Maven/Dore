"""Background source-health thread — flip detection + persistence.

The full daemon thread is daemonised and sleeps for hours; we test the
single-cycle work (`run_health_cycle`) and the persistence layer
directly. The thread spawn itself is exercised once for idempotency
and the disabled-env short-circuit.

Tests are hermetic — `canary.run_canary` is mocked out per test to
produce a deterministic CanaryReport.
"""
from __future__ import annotations

import json
import os

import pytest

from sca import health_thread
from sca.canary import CanaryReport, SourceCheck


def _make_report(checks: list[tuple[str, str]]) -> CanaryReport:
    """Build a CanaryReport from [(id, status), ...] pairs."""
    report = CanaryReport()
    for sid, status in checks:
        report.checks.append(SourceCheck(
            id=sid, url=f"https://example.com/{sid}",
            kind="corpus", status=status,
            status_code=200 if status == "live" else 0,
        ))
    return report


# ── persistence ───────────────────────────────────────────────────────
def test_state_file_round_trips(tmp_path, monkeypatch):
    """Save then load returns the same dict."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "h.json",
    )
    health_thread._save_state({"a": "live", "b": "broken"})
    loaded = health_thread._load_state()
    assert loaded == {"a": "live", "b": "broken"}


def test_load_state_missing_returns_empty(tmp_path, monkeypatch):
    """First run, no state file → empty dict (not a crash)."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "missing.json",
    )
    assert health_thread._load_state() == {}


def test_load_state_corrupt_returns_empty(tmp_path, monkeypatch):
    """A corrupt JSON file is treated as empty — never crashes the thread."""
    path = tmp_path / "h.json"
    path.write_text("not json{{{")
    monkeypatch.setattr(health_thread, "HEALTH_STATE_PATH", path)
    assert health_thread._load_state() == {}


def test_load_state_filters_non_string_values(tmp_path, monkeypatch):
    """A future-schema row with non-string values is silently dropped."""
    path = tmp_path / "h.json"
    path.write_text(json.dumps({"a": "live", "b": {"nested": "junk"}}))
    monkeypatch.setattr(health_thread, "HEALTH_STATE_PATH", path)
    loaded = health_thread._load_state()
    assert loaded == {"a": "live"}


# ── flip detection ────────────────────────────────────────────────────
def _capture_events(monkeypatch) -> list[dict]:
    """Replace the observability sink with a list-collector; returns it."""
    from sca import observability

    captured: list[dict] = []
    monkeypatch.setattr(observability, "_SINK", captured.append)
    # Force events on (sink check short-circuits under pytest by default).
    monkeypatch.setenv("SCA_LOG", "1")
    return captured


def test_no_flip_when_status_stable(monkeypatch, tmp_path):
    """Two cycles with identical statuses → no `health.source.flipped` event."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "h.json",
    )
    events = _capture_events(monkeypatch)

    def fake_canary():
        return _make_report([("src-a", "live"), ("src-b", "broken")])

    monkeypatch.setattr("sca.canary.run_canary", fake_canary)

    health_thread.run_health_cycle()  # establishes baseline
    events.clear()
    health_thread.run_health_cycle()  # second cycle, same statuses

    flipped = [e for e in events if e.get("kind") == "health.source.flipped"]
    assert flipped == []


def test_flip_live_to_broken_emits_warn_event(monkeypatch, tmp_path):
    """A source going live→broken emits one warn-level flipped event."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "h.json",
    )
    events = _capture_events(monkeypatch)

    states = [
        _make_report([("src-a", "live")]),
        _make_report([("src-a", "broken")]),
    ]
    calls = iter(states)
    monkeypatch.setattr("sca.canary.run_canary", lambda: next(calls))

    health_thread.run_health_cycle()
    events.clear()
    health_thread.run_health_cycle()

    flipped = [e for e in events if e.get("kind") == "health.source.flipped"]
    assert len(flipped) == 1
    e = flipped[0]
    assert e["level"] == "warn"
    assert e["from_status"] == "live"
    assert e["to_status"] == "broken"
    assert e["id"] == "src-a"


def test_flip_broken_to_live_emits_info_event(monkeypatch, tmp_path):
    """A source going broken→live emits one info-level flipped event
    (a recovery, not an incident — info, not warn)."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "h.json",
    )
    events = _capture_events(monkeypatch)

    states = [
        _make_report([("src-a", "broken")]),
        _make_report([("src-a", "live")]),
    ]
    calls = iter(states)
    monkeypatch.setattr("sca.canary.run_canary", lambda: next(calls))

    health_thread.run_health_cycle()
    events.clear()
    health_thread.run_health_cycle()

    flipped = [e for e in events if e.get("kind") == "health.source.flipped"]
    assert len(flipped) == 1
    assert flipped[0]["level"] == "info"
    assert flipped[0]["from_status"] == "broken"
    assert flipped[0]["to_status"] == "live"


def test_flip_detection_survives_restart(monkeypatch, tmp_path):
    """The state file persists across simulated process restarts —
    a second cycle, after we drop in-memory state, still detects the
    flip from the on-disk baseline."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "h.json",
    )
    events = _capture_events(monkeypatch)

    monkeypatch.setattr(
        "sca.canary.run_canary",
        lambda: _make_report([("src-a", "live")]),
    )
    health_thread.run_health_cycle()
    # Confirm the state file exists.
    assert (tmp_path / "h.json").exists()

    # "Restart" — no in-memory state to lean on, only the file.
    events.clear()
    monkeypatch.setattr(
        "sca.canary.run_canary",
        lambda: _make_report([("src-a", "broken")]),
    )
    health_thread.run_health_cycle()

    flipped = [e for e in events if e.get("kind") == "health.source.flipped"]
    assert len(flipped) == 1
    assert flipped[0]["from_status"] == "live"
    assert flipped[0]["to_status"] == "broken"


def test_sweep_done_event_carries_counts(monkeypatch, tmp_path):
    """Each cycle emits one health.sweep.done with the counts."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "h.json",
    )
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(
        "sca.canary.run_canary",
        lambda: _make_report([
            ("a", "live"), ("b", "live"), ("c", "broken"),
        ]),
    )

    counts = health_thread.run_health_cycle()
    assert counts == {"live": 2, "snapshot": 0, "broken": 1, "changed": 0}

    done = [e for e in events if e.get("kind") == "health.sweep.done"]
    assert len(done) == 1
    assert done[0]["live"] == 2
    assert done[0]["broken"] == 1


def test_cycle_continues_when_canary_raises(monkeypatch, tmp_path):
    """A canary that explodes is caught — cycle returns zeros, no raise."""
    monkeypatch.setattr(
        health_thread, "HEALTH_STATE_PATH", tmp_path / "h.json",
    )
    events = _capture_events(monkeypatch)

    def boom():
        raise RuntimeError("canary exploded")

    monkeypatch.setattr("sca.canary.run_canary", boom)
    counts = health_thread.run_health_cycle()
    assert counts == {"live": 0, "snapshot": 0, "broken": 0, "changed": 0}

    failed = [e for e in events if e.get("kind") == "health.sweep.failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"


# ── interval parsing ──────────────────────────────────────────────────
def test_default_interval_is_six_hours(monkeypatch):
    monkeypatch.delenv("SCA_HEALTH_INTERVAL_HOURS", raising=False)
    assert health_thread._interval_seconds() == 6 * 3600


def test_env_overrides_interval(monkeypatch):
    monkeypatch.setenv("SCA_HEALTH_INTERVAL_HOURS", "2.5")
    assert health_thread._interval_seconds() == 2.5 * 3600


def test_interval_floors_at_safety_minimum(monkeypatch):
    """A misconfigured 0.1h must not become a DoS — floor at 1h."""
    monkeypatch.setenv("SCA_HEALTH_INTERVAL_HOURS", "0.1")
    assert health_thread._interval_seconds() == 1 * 3600


def test_unparseable_interval_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SCA_HEALTH_INTERVAL_HOURS", "nonsense")
    assert health_thread._interval_seconds() == 6 * 3600


# ── thread spawn ──────────────────────────────────────────────────────
def test_start_health_thread_is_idempotent(monkeypatch):
    """Two start calls in the same process spawn at most one thread."""
    monkeypatch.delenv("SCA_HEALTH_DISABLED", raising=False)
    health_thread._reset_for_tests()
    t1 = health_thread.start_health_thread()
    t2 = health_thread.start_health_thread()
    assert t1 is not None
    assert t2 is None
    # The thread is a daemon; we don't join it. Just confirm it's alive
    # and walk away — the autouse fixture resets state for next test.
    assert t1.daemon
    assert t1.is_alive()


def test_start_health_thread_disabled_env_short_circuits(monkeypatch):
    """SCA_HEALTH_DISABLED=1 → no thread spawned, returns None."""
    monkeypatch.setenv("SCA_HEALTH_DISABLED", "1")
    health_thread._reset_for_tests()
    out = health_thread.start_health_thread()
    assert out is None
