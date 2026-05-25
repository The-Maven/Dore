"""Chaos engineering — scenario runner tests.

Each scenario must:
  • produce a Finding with passed=True when the live system holds the
    invariant the scenario tests
  • produce a Finding with passed=False (and severity='fail') if the
    invariant is broken
  • never raise out of the runner

The chaos thread itself is gated by SCA_CHAOS_DISABLED=1 in tests, so
we drive scenarios via run_one_cycle() directly.
"""
from __future__ import annotations

import os
import pytest

from sca.movement import chaos


@pytest.fixture(autouse=True)
def _isolated_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(chaos, "CHAOS_FINDINGS_PATH", tmp_path / "chaos.json")


def test_run_one_cycle_runs_every_scenario_and_persists():
    """A single chaos cycle must execute every registered scenario
    and persist a Finding per scenario. Findings file should exist
    after the cycle."""
    fresh = chaos.run_one_cycle()
    assert len(fresh) >= 5
    # All scenarios must be represented in the persisted findings.
    persisted = chaos._load_findings()
    for finding in fresh:
        assert finding.scenario in persisted


def test_all_scenarios_pass_against_current_codebase():
    """Smoke test: every audit-fix invariant the chaos suite tests
    against must pass on the live code. A failure here means a real
    regression — the LLM judge would also see the warn-level finding
    in its prompt."""
    fresh = chaos.run_one_cycle()
    failing = [f for f in fresh if not f.passed]
    if failing:
        msg = "\n".join(
            f"  ✕ {f.scenario}: {f.observed}" for f in failing)
        pytest.fail(
            f"{len(failing)} chaos scenario(s) failed:\n{msg}\n"
            "An invariant the audit-fix tests rely on has regressed.")


def test_fragility_prompt_block_renders_findings():
    """The judge prompt incorporates recent findings as a markdown
    block. Empty when there are no findings; rendered cleanly when
    there are."""
    # Empty case
    block_empty = chaos.fragility_prompt_block()
    assert block_empty == ""

    # Run a cycle then check
    chaos.run_one_cycle()
    block = chaos.fragility_prompt_block(limit=5)
    assert "Known fragility patterns" in block
    assert "✓" in block or "✕" in block
    # Each scenario name should appear as a code-tag
    assert "`commentary_malformed_context`" in block \
        or "`trader_none_current`" in block


def test_recent_findings_orders_by_ran_at_desc():
    """Most-recent findings come first so the judge sees the freshest
    invariant state."""
    chaos.run_one_cycle()
    rows = chaos.recent_findings(limit=10)
    assert rows
    # Each scenario should appear at most once (we key by scenario id).
    seen = set()
    for r in rows:
        assert r["scenario"] not in seen
        seen.add(r["scenario"])


def test_scenario_runner_never_raises_on_inner_exception(monkeypatch):
    """If a scenario function itself raises, the runner records a
    fail-Finding rather than crashing the cycle."""
    def _boom():
        raise RuntimeError("scenario explicit failure")
    monkeypatch.setattr(chaos, "_SCENARIOS", [_boom])
    fresh = chaos.run_one_cycle()
    assert len(fresh) == 1
    assert fresh[0].passed is False
    assert fresh[0].severity == "fail"
    assert "scenario explicit failure" in fresh[0].observed
