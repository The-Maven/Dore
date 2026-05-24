"""Deterministic forecast engine for the movement simulator.

LAYER: facts. The math here is the audited core. No LLM call. Given a
history of peg ticks and supply snapshots, returns a structured
forecast with a point estimate, asymmetric uncertainty bands, and a
naive-baseline mirror so the resolver can prove the engine adds skill
over persistence + climatology.

Model: EWMA (exponentially weighted moving average) for the point;
rolling EWMA of squared residuals for the conditional volatility. The
cone is parameterized off that vol. This is intentionally the
simplest thing that beats persistence — investor pushback should be on
the *calibration archive*, not on model complexity that doesn't show
skill yet. Bumping to ARIMA/GARCH is a future model version; the
`model` field on each row keeps the audit trail clean across upgrades.

References (see research brief):
  - Gneiting & Raftery 2007 on strictly proper scoring rules
  - BoE fan-chart visual idiom (we emit SYMMETRIC bands at 50/80/95
    in v1; the BoE's two-piece normal asymmetric construction is a
    future model version that would require a separate skew estimate)
  - Murphy decomposition for the calibration diagram
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# Current engine version. Bump on any change to the math so the
# archive carries a clean audit trail of which model wrote what.
MODEL_VERSION = "ewma_cone_v1"

# EWMA smoothing factor — alpha=0.3 gives a half-life of ~2 ticks at
# our default 1-min sampling, which is short enough to react to a
# fresh deviation pulse but long enough that one noisy tick doesn't
# dominate the forecast.
_EWMA_ALPHA = 0.3

# Minimum history we need before emitting a real forecast. Below
# this, we still emit a row (the archive must show 'we did try') but
# notes='insufficient_history' and the bands are wide-open.
_MIN_HISTORY = 6


@dataclass
class Forecast:
    """One forecast row, fully formed for store insertion. The shape
    mirrors the predictions table columns 1:1 — see
    supabase/migrations/0007_movement_simulator.sql."""
    symbol: str
    kind: str  # 'peg_deviation' | 'net_flow_direction' | 'net_flow_magnitude'
    horizon_minutes: int
    resolves_at: str  # ISO 8601 UTC
    point: float
    p50_low: Optional[float]
    p50_high: Optional[float]
    p80_low: Optional[float]
    p80_high: Optional[float]
    p95_low: Optional[float]
    p95_high: Optional[float]
    prob_positive: Optional[float]
    confidence_word: Optional[str]
    drivers: list = field(default_factory=list)
    model: str = MODEL_VERSION
    notes: str = ""


# ── helpers ──────────────────────────────────────────────────────────
def _confidence_word_for_bps_band(p80_half_width: float) -> str:
    """Map an 80%-band half-width (in bps) to the IPCC-style ladder
    word. The cutoffs are deliberately conservative — Doré never
    claims 'virtually certain' from a thin model.

    Reasoning: at 80% interval, half-width <2bps means we're calling
    sub-routine peg drift confidently; 2–8 is normal; 8–20 is wide;
    above 20 is 'we have no idea'."""
    h = abs(p80_half_width)
    if h < 2.0:
        return "very_likely"
    if h < 5.0:
        return "likely"
    if h < 12.0:
        return "about_as_likely_as_not"
    if h < 25.0:
        return "unlikely"
    return "very_unlikely"


def _confidence_word_for_prob(prob: float) -> str:
    """Map a probability (0..1) to the IPCC ladder word that names
    the same claim. Symmetric around 0.5 because the direction matters
    in the band; this just narrates the strength."""
    if prob >= 0.95:
        return "virtually_certain"
    if prob >= 0.85:
        return "very_likely"
    if prob >= 0.66:
        return "likely"
    if prob >= 0.34:
        return "about_as_likely_as_not"
    if prob >= 0.15:
        return "unlikely"
    if prob >= 0.05:
        return "very_unlikely"
    return "exceptionally_unlikely"


def _ewma(values: list[float], alpha: float = _EWMA_ALPHA) -> float:
    """Walk-forward EWMA. Newest value gets the highest weight; very
    old observations decay exponentially. Robust to gaps because each
    new value is a (1-alpha) shrinkage of the prior estimate."""
    if not values:
        return 0.0
    out = values[0]
    for v in values[1:]:
        out = alpha * v + (1 - alpha) * out
    return out


def _ewma_volatility(values: list[float], alpha: float = _EWMA_ALPHA) -> float:
    """Estimate the conditional std-dev of step-to-step changes via
    EWMA of squared first differences. Lower bound at 0.5 to avoid
    over-confident bands when history is unusually quiet — sub-bp
    'certainty' on stablecoin peg is almost never real."""
    if len(values) < 2:
        return 1.0
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
    sq = [d * d for d in diffs]
    var = _ewma(sq, alpha)
    return max(math.sqrt(var), 0.5)


def _horizon_widening(volatility: float, horizon_minutes: int,
                      sample_period_minutes: float = 1.0) -> float:
    """Vol projected forward over the horizon. Independent-step
    diffusion: scale by sqrt(horizon / sample_period). Honest
    over-statement — real stablecoin peg drift is mean-reverting, so
    the cone widens slightly slower than this; we accept the
    conservative cone in v1 because under-confidence is preferable to
    over-confidence in this audience."""
    if sample_period_minutes <= 0:
        return volatility
    return volatility * math.sqrt(horizon_minutes / sample_period_minutes)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── peg deviation forecast ───────────────────────────────────────────
def forecast_peg_deviation(
    symbol: str,
    history_bps: list[float],
    horizon_minutes: int,
    *,
    drivers: Optional[list[dict]] = None,
) -> Forecast:
    """Forecast peg deviation (bps from $1.00) at horizon_minutes ahead.

    `history_bps` is newest-last, evenly sampled (we don't interpolate
    gaps — the ticker is responsible for cadence). The output point
    is the EWMA; bands are symmetric normal at 50/80/95 with sigma
    scaled by sqrt(horizon).

    Returns a Forecast row ready for the store. `drivers` is the
    cited attribution payload; falsy = honest 'no driver cited'.
    """
    drivers = drivers or []
    now = _now_utc()
    resolves_at = (now + timedelta(minutes=horizon_minutes)).isoformat(
        timespec="seconds"
    )

    if len(history_bps) < _MIN_HISTORY:
        # Honest "we don't have enough data yet" row. Wide cone,
        # confidence word names it as a 50/50.
        return Forecast(
            symbol=symbol, kind="peg_deviation",
            horizon_minutes=horizon_minutes, resolves_at=resolves_at,
            point=history_bps[-1] if history_bps else 0.0,
            p50_low=-25.0, p50_high=25.0,
            p80_low=-60.0, p80_high=60.0,
            p95_low=-150.0, p95_high=150.0,
            prob_positive=None,
            confidence_word="about_as_likely_as_not",
            drivers=drivers, model=MODEL_VERSION,
            notes=f"insufficient_history:{len(history_bps)}<{_MIN_HISTORY}",
        )

    point = _ewma(history_bps)
    vol = _ewma_volatility(history_bps)
    sigma = _horizon_widening(vol, horizon_minutes)
    # Normal-quantile multipliers for symmetric bands.
    # 50% -> 0.6745, 80% -> 1.2816, 95% -> 1.96.
    return Forecast(
        symbol=symbol, kind="peg_deviation",
        horizon_minutes=horizon_minutes, resolves_at=resolves_at,
        point=point,
        p50_low=point - 0.6745 * sigma, p50_high=point + 0.6745 * sigma,
        p80_low=point - 1.2816 * sigma, p80_high=point + 1.2816 * sigma,
        p95_low=point - 1.96 * sigma,   p95_high=point + 1.96 * sigma,
        prob_positive=None,
        confidence_word=_confidence_word_for_bps_band(1.2816 * sigma),
        drivers=drivers, model=MODEL_VERSION,
        notes=f"history={len(history_bps)} sigma={sigma:.2f}bp",
    )


# ── net flow direction forecast ──────────────────────────────────────
def forecast_net_flow_direction(
    symbol: str,
    supply_series: list[float],
    horizon_minutes: int,
    *,
    drivers: Optional[list[dict]] = None,
) -> Forecast:
    """Forecast the probability that net mint/burn in the next
    `horizon_minutes` is positive (mint > burn).

    `supply_series` is newest-last total-supply readings. The signal
    is the recent net trend: EWMA of step-to-step supply changes.
    Positive trend nudges prob_positive above 0.5; band widens with
    volatility so a noisy series defaults toward 50/50.

    Output `point` carries the expected net change for the resolver
    to grade against (in token units, same as supply readings).
    """
    drivers = drivers or []
    now = _now_utc()
    resolves_at = (now + timedelta(minutes=horizon_minutes)).isoformat(
        timespec="seconds"
    )

    if len(supply_series) < _MIN_HISTORY:
        return Forecast(
            symbol=symbol, kind="net_flow_direction",
            horizon_minutes=horizon_minutes, resolves_at=resolves_at,
            point=0.0,
            p50_low=None, p50_high=None,
            p80_low=None, p80_high=None,
            p95_low=None, p95_high=None,
            prob_positive=0.5,
            confidence_word="about_as_likely_as_not",
            drivers=drivers, model=MODEL_VERSION,
            notes=f"insufficient_history:{len(supply_series)}<{_MIN_HISTORY}",
        )

    diffs = [supply_series[i] - supply_series[i - 1]
             for i in range(1, len(supply_series))]
    mu = _ewma(diffs)
    # Volatility of the per-step change. With ~unit-step samples the
    # horizon scaling matches the peg model.
    var_diffs = _ewma([d * d for d in diffs])
    # Audit #5: finite-sample EWMA can occasionally produce
    # var_diffs <= mu*mu (the E[X²] >= E[X]² identity only holds in
    # the limit). When that happens we used to snap silently to
    # sigma=1.0, leaving no audit trail. Now we annotate the row's
    # notes so a curator scanning the archive can see exactly why a
    # particular forecast had a degenerate cone.
    variance_snapped = False
    if var_diffs > mu * mu:
        sigma_step = max(math.sqrt(var_diffs - mu * mu), 1.0)
    else:
        sigma_step = 1.0
        variance_snapped = True
    sigma_h = sigma_step * math.sqrt(horizon_minutes)
    expected = mu * horizon_minutes
    # Probability of a positive net change at the horizon under a
    # normal approximation. Clamp to a sane envelope so a wild outlier
    # doesn't return 0.999 from one tick.
    z = expected / sigma_h if sigma_h > 0 else 0.0
    # Standard-normal CDF via erf.
    prob_positive = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    # Clamp inside [0.05, 0.94]. The IPCC 'virtually_certain' cutoff
    # in _confidence_word_for_prob is >= 0.95, so a 0.94 cap means
    # the engine cannot emit 'virtually_certain' from EWMA alone —
    # investors see at most 'very_likely' (>= 0.85). The 0.05 floor
    # is the mirror: the engine cannot emit 'exceptionally_unlikely'
    # either, since that requires < 0.05.
    prob_positive = min(max(prob_positive, 0.05), 0.94)

    base_notes = (f"history={len(supply_series)} sigma={sigma_h:.0f} "
                  f"mu={mu:.2f}")
    if variance_snapped:
        base_notes += " variance_snapped_to_1"
    return Forecast(
        symbol=symbol, kind="net_flow_direction",
        horizon_minutes=horizon_minutes, resolves_at=resolves_at,
        point=expected,
        p50_low=expected - 0.6745 * sigma_h,
        p50_high=expected + 0.6745 * sigma_h,
        p80_low=expected - 1.2816 * sigma_h,
        p80_high=expected + 1.2816 * sigma_h,
        p95_low=expected - 1.96 * sigma_h,
        p95_high=expected + 1.96 * sigma_h,
        prob_positive=prob_positive,
        confidence_word=_confidence_word_for_prob(prob_positive),
        drivers=drivers, model=MODEL_VERSION,
        notes=base_notes,
    )


# ── persistence + climatology baselines ──────────────────────────────
# Two trivial baselines every forecast is scored against. The model
# only "earns" credibility when its Brier beats BOTH of these.
def persistence_baseline_peg(history_bps: list[float],
                              horizon_minutes: int) -> Forecast:
    """Persistence: forecast = last observed value. The band is
    history-derived volatility scaled by sqrt(horizon)."""
    point = history_bps[-1] if history_bps else 0.0
    vol = _ewma_volatility(history_bps)
    sigma = _horizon_widening(vol, horizon_minutes)
    return Forecast(
        symbol="", kind="peg_deviation",
        horizon_minutes=horizon_minutes, resolves_at="",
        point=point,
        p50_low=point - 0.6745 * sigma, p50_high=point + 0.6745 * sigma,
        p80_low=point - 1.2816 * sigma, p80_high=point + 1.2816 * sigma,
        p95_low=point - 1.96 * sigma,   p95_high=point + 1.96 * sigma,
        prob_positive=None, confidence_word=None,
        drivers=[], model="persistence_baseline", notes="",
    )


def climatology_baseline_peg(history_bps: list[float],
                              horizon_minutes: int) -> Forecast:
    """Climatology: forecast = long-run mean (here, mean of available
    history). Useful as a sanity floor — a model that does worse than
    'every prediction is the historical average' is not worth running."""
    point = sum(history_bps) / len(history_bps) if history_bps else 0.0
    if len(history_bps) >= 2:
        var = sum((x - point) ** 2 for x in history_bps) / (len(history_bps) - 1)
        sigma = max(math.sqrt(var), 0.5)
    else:
        sigma = 5.0
    return Forecast(
        symbol="", kind="peg_deviation",
        horizon_minutes=horizon_minutes, resolves_at="",
        point=point,
        p50_low=point - 0.6745 * sigma, p50_high=point + 0.6745 * sigma,
        p80_low=point - 1.2816 * sigma, p80_high=point + 1.2816 * sigma,
        p95_low=point - 1.96 * sigma,   p95_high=point + 1.96 * sigma,
        prob_positive=None, confidence_word=None,
        drivers=[], model="climatology_baseline", notes="",
    )
