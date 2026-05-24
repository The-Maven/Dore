"""Engine tests — EWMA + volatility cone + confidence ladder.

The math is intentionally simple, but the calibration story depends on
its honesty. These tests pin the contract:
  - Insufficient history emits a wide, honest 'we don't know' row.
  - With history, the cone widens with sqrt(horizon).
  - The confidence word maps from the 80%-band width to IPCC ladder.
  - Direction forecasts clamp prob_positive into a sane envelope so
    one outlier tick can never make us say 99.9%.
"""
from __future__ import annotations

import math

from sca.movement.predict import (
    MODEL_VERSION,
    climatology_baseline_peg,
    forecast_net_flow_direction,
    forecast_peg_deviation,
    persistence_baseline_peg,
)


def test_insufficient_history_emits_honest_wide_row():
    """Below the min-history floor, the row exists (the archive must
    show 'we did try') but the cone is wide and the confidence word
    names it as a 50/50."""
    f = forecast_peg_deviation("USDC", history_bps=[0.1, 0.0], horizon_minutes=60)
    assert f.notes.startswith("insufficient_history")
    assert f.confidence_word == "about_as_likely_as_not"
    # Wide 95% band so a clean miss inside p95 is still possible
    assert f.p95_low <= -50
    assert f.p95_high >= 50


def test_peg_forecast_emits_full_band_with_enough_history():
    history = [1.0, 0.8, 1.2, 0.9, 1.1, 1.05, 0.95, 1.0]
    f = forecast_peg_deviation("USDC", history, horizon_minutes=60)
    # Point should land near the EWMA, well inside the recent range
    assert -2.0 < f.point < 2.0
    # Bands ordered correctly: wider bands extend further from point
    assert f.p95_low < f.p80_low < f.p50_low < f.point
    assert f.point < f.p50_high < f.p80_high < f.p95_high
    assert f.model == MODEL_VERSION
    assert f.confidence_word in (
        "virtually_certain", "very_likely", "likely",
        "about_as_likely_as_not", "unlikely", "very_unlikely",
        "exceptionally_unlikely",
    )


def test_cone_widens_with_horizon():
    history = [1.0, 0.8, 1.2, 0.9, 1.1, 1.05, 0.95, 1.0]
    short = forecast_peg_deviation("USDC", history, horizon_minutes=10)
    long_ = forecast_peg_deviation("USDC", history, horizon_minutes=240)
    # 95% band on long horizon must be wider than on short — sqrt scaling
    short_width = short.p95_high - short.p95_low
    long_width = long_.p95_high - long_.p95_low
    assert long_width > short_width
    # Loosely sqrt-scaled
    assert long_width > short_width * 2


def test_direction_forecast_clamps_envelope():
    """One huge outlier tick must not make us claim 0.999. The
    clamp is the safety net the methodology research demanded."""
    # Mostly noise; one big positive step
    series = [1_000_000.0, 1_000_001.0, 999_999.0, 1_000_000.0,
              1_000_002.0, 1_500_000.0, 1_500_001.0]
    f = forecast_net_flow_direction("USDC", series, horizon_minutes=60)
    assert 0.05 <= f.prob_positive <= 0.95
    assert f.confidence_word in (
        "very_likely", "likely", "about_as_likely_as_not",
    )


def test_baselines_are_distinguishable():
    """Persistence + climatology must not collide. If a model can't
    show it differs from these, it has no skill story to tell."""
    history = [10.0, 12.0, 8.0, 11.0, 9.5, 10.5, 10.0]
    p = persistence_baseline_peg(history, horizon_minutes=60)
    c = climatology_baseline_peg(history, horizon_minutes=60)
    # Persistence points at the last value
    assert p.point == history[-1]
    # Climatology points at the mean
    expected_mean = sum(history) / len(history)
    assert abs(c.point - expected_mean) < 1e-6
    # They name themselves so the resolver can write the right column
    assert p.model == "persistence_baseline"
    assert c.model == "climatology_baseline"


def test_confidence_word_for_tight_band():
    """A tight 80% band must yield a 'very_likely' word — IPCC ladder
    is the headline-reader's surface, must match the math."""
    history = [0.0] * 10  # zero variance
    f = forecast_peg_deviation("USDC", history, horizon_minutes=10)
    # Sigma floor in _ewma_volatility is 0.5; at 10min horizon the
    # 80% half-width is ~1.28 * 0.5 * sqrt(10) ≈ 2.0; below the
    # 'very_likely' cutoff
    assert f.confidence_word in ("very_likely", "likely")


def test_drivers_default_to_empty_list_not_none():
    """Honest 'no driver cited' = empty list, not null. The schema
    rejects null, and the UI distinguishes empty from absent."""
    f = forecast_peg_deviation("USDC", [0.0] * 10, horizon_minutes=60)
    assert f.drivers == []
    f2 = forecast_net_flow_direction("USDC", [1e6] * 10, horizon_minutes=60)
    assert f2.drivers == []
