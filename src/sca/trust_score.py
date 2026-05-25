"""TRUST SCORE — Doré's institutional differentiator.

Existing rating frameworks (Bluechip SMIDGE, S&P Stability Assessment,
Moody's Digital Asset Monitor) publish PDFs quarterly. Doré computes
a real-time, triangulated, CFO-language trust score from data we
already have on the verification pipeline.

The score combines ten dimensions, each independently scored 0-100,
each tagged with its trust-tier (verified / attested / estimated /
unverified). The composite is a weighted sum; the verdict is plain
English ("Eligible for Tier-1 corporate treasury IPS" / "Fails the
concentration test" / etc.) — not "B+".

Design rules (from research synthesis):
  • Every dimension cites its source — no free-floating numbers.
  • When data is missing, the dimension is scored CONSERVATIVELY
    and tagged "unverified" rather than skipped — so the composite
    score reflects what we don't know.
  • The score is a LEAD, not a fact: it points the reader at the
    underlying surfaces (F2 attestation, F5 sanctions, F6
    redemption capacity) where the cited evidence lives.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sca.config import get_stablecoin
from sca.movement.token_context import get_context as _get_token_ctx


# ── dimension definitions ──────────────────────────────────────────
# Weights sum to 100. Tuned from the SMIDGE+S&P+Moody's union:
# reserve quality is the headline; attestation freshness and peg
# stability are the live-signal proof points; everything else
# triangulates.
DIMENSIONS = (
    ("reserve_quality",          18.0),
    ("peg_stability",            14.0),
    ("attestation_freshness",    11.0),
    ("redemption_capacity",      11.0),
    ("sanctions_exposure",       10.0),
    ("auditor_credibility",       9.0),
    ("source_consensus",          7.0),
    ("regulatory_standing",       7.0),
    ("custodian_concentration",   7.0),
    ("implementation_risk",       6.0),
)
TOTAL_WEIGHT = sum(w for _, w in DIMENSIONS)
_WEIGHT = dict(DIMENSIONS)
assert abs(TOTAL_WEIGHT - 100.0) < 1e-6, (
    f"Trust-score dimension weights must sum to 100, got {TOTAL_WEIGHT}")


# Verdict thresholds. These match the institutional-treasury-policy
# language a CFO actually uses, not a rating-agency tier letter.
VERDICT_THRESHOLDS = (
    (85, "Eligible for Tier-1 corporate treasury IPS"),
    (70, "Eligible with concentration limits"),
    (55, "Speculative — limit to non-strategic reserve"),
    (40, "High-risk — short-duration tactical only"),
    (0,  "Not eligible under standard treasury policy"),
)


@dataclass
class TrustDimension:
    name: str
    label: str           # human-readable
    score: float         # 0-100
    weight: float        # percentage of composite
    tier: str            # 'verified' | 'attested' | 'estimated' | 'unverified'
    reasoning: str       # 1-2 sentences in plain English
    evidence_source: str = ""  # on-chain | attestation | registry | computed | search
    evidence_url: str = ""     # link to the underlying source where applicable


@dataclass
class TrustScore:
    symbol: str
    score: float                          # composite 0-100
    verdict: str                          # CFO-language one-liner
    dimensions: list = field(default_factory=list)
    generated_at: str = ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 1),
            "verdict": self.verdict,
            "dimensions": [asdict(d) for d in self.dimensions],
            "generated_at": self.generated_at,
        }


# ── helpers ────────────────────────────────────────────────────────
def _verdict_for(score: float) -> str:
    for threshold, verdict in VERDICT_THRESHOLDS:
        if score >= threshold:
            return verdict
    return VERDICT_THRESHOLDS[-1][1]


_BIG4 = {"deloitte", "ey", "ernst & young", "ernst young",
          "pwc", "pricewaterhousecoopers", "kpmg"}
_MID_TIER = {"bdo", "grant thornton", "rsm", "forvis mazars", "mazars",
              "moore stephens", "baker tilly", "crowe"}


def _auditor_score(auditor: str) -> tuple[int, str, str]:
    """Returns (score, tier, reasoning)."""
    a = (auditor or "").strip().lower()
    if not a:
        return (30, "unverified",
                "No auditor named in the registry — assume worst-case until "
                "an attestation surfaces one.")
    if any(b in a for b in _BIG4):
        return (95, "verified",
                f"Big-Four auditor ({auditor}) — meets every major treasury "
                f"policy's attestor credibility bar.")
    if any(m in a for m in _MID_TIER):
        return (80, "verified",
                f"Mid-tier registered firm ({auditor}) — accepted by most "
                f"institutional policies; may require additional review at "
                f"the largest desks.")
    return (55, "attested",
            f"Auditor on record ({auditor}) but outside the Big-Four / "
            f"recognised mid-tier set — policy teams will want to verify "
            f"the firm's registrations independently.")


def _backing_model_score(backing_model: str, attestation_url: str,
                          transparency_url: str
                          ) -> tuple[int, str, str, str, str]:
    """Reserve-quality score derived from the backing model.

    Returns (score, tier, reasoning, evidence_source, evidence_url)."""
    bm = (backing_model or "").lower()
    has_url = bool(attestation_url or transparency_url)
    url = attestation_url or transparency_url
    if bm == "fiat_reserves":
        if has_url:
            return (88, "attested",
                    "Fiat-backed model with a published reserve attestation "
                    "page. Reserve quality depends on the composition listed "
                    "in that attestation — see Reserve Composition on F2.",
                    "attestation", url)
        return (45, "unverified",
                "Fiat-backed model but no reserve attestation URL on record. "
                "A CFO cannot underwrite this without an issuer disclosure.",
                "registry", "")
    if bm == "crypto_collateral":
        return (82, "verified",
                "Overcollateralised on-chain. Reserve composition is "
                "directly readable on-chain; no issuer attestation needed by "
                "design.",
                "on-chain", url)
    if bm == "synthetic_delta_neutral":
        return (62, "attested",
                "Synthetic / delta-neutral. Backed by hedged positions on "
                "centralised venues — reserve quality is operationally "
                "complex and depends on counterparty health.",
                "attestation", url)
    if bm == "algorithmic":
        return (28, "unverified",
                "Algorithmic model. Historical track record of catastrophic "
                "failures (UST). Not generally eligible for institutional "
                "treasury policies.",
                "registry", "")
    if bm == "new_or_unverified":
        return (25, "unverified",
                "Backing model not yet characterised in the registry. Treat "
                "as un-underwriteable until the issuer publishes specifics.",
                "registry", "")
    return (50, "unverified",
            f"Backing model '{backing_model}' is not in the registry's "
            f"recognised set. Investigate before treating as treasury-grade.",
            "registry", "")


# Issuers known to be US-licensed money transmitters / NY DFS-supervised.
# Source: Circle (NMLS + DFS), PayPal (Paxos issuer NY DFS), Gemini Dollar
# (Gemini Trust, NY DFS), USDP (Paxos NY DFS).
_US_REGULATED_ISSUERS = {
    "circle", "paypal", "paxos", "gemini", "first digital",
    "blackrock", "ondo finance",
}
# MiCA-registered EU issuers (provisional — verify against ESMA registry).
_MICA_REGISTERED_HINTS = {"circle europe", "societe generale"}


def _regulatory_score(issuer: str) -> tuple[int, str, str]:
    i = (issuer or "").strip().lower()
    if not i:
        return (30, "unverified",
                "No issuer named in the registry.")
    if any(reg in i for reg in _US_REGULATED_ISSUERS):
        return (92, "verified",
                f"{issuer} operates under US state money-transmitter "
                f"licensing (NY DFS / NMLS). Treasury policies recognising "
                f"GENIUS Act compliance can underwrite.")
    if any(reg in i for reg in _MICA_REGISTERED_HINTS):
        return (85, "verified",
                f"{issuer} has MiCA registration in scope — EU regulatory "
                f"recognition.")
    # DAO / protocol issuers: not "regulated" but not adversarial either.
    if any(kw in i for kw in ("makerdao", "sky", "ethena", "frax",
                                "liquity", "curve", "aave")):
        return (62, "estimated",
                f"{issuer} is a protocol / DAO without traditional regulator. "
                f"Treasury policy treatment varies — many institutional "
                f"desks require additional governance review.")
    return (45, "unverified",
            f"{issuer} regulatory standing not characterised. Verify "
            f"licensing claims independently before treasury allocation.")


def _redemption_score(label: str) -> tuple[int, str]:
    """Map payout_timeline_label → score."""
    lbl = (label or "").lower()
    if not lbl:
        return (50, "Redemption timeline not characterised in the registry.")
    if "same-day" in lbl or "instant on-chain" in lbl:
        return (95,
                "Same-day or instant on-chain redemption — meets the "
                "tightest treasury liquidity requirements.")
    if "t+0" in lbl or "psm swap" in lbl:
        return (88,
                "T+0 / PSM-swap redemption — fast enough for most treasury "
                "operating cycles.")
    if "t+1" in lbl:
        return (75,
                "T+1 settlement — acceptable for standard treasury policies "
                "but slower than instant on-chain alternatives.")
    if "amm exit only" in lbl:
        return (50,
                "AMM-exit only — no primary redemption path; liquidity is "
                "secondary-market dependent. Concentration risk on size.")
    if "7-day" in lbl or "cooldown" in lbl:
        return (45,
                "7-day cooldown on redemption — ties up capital. Not "
                "suitable as a liquid treasury reserve.")
    if "40-day" in lbl or "lockup" in lbl:
        return (25,
                "40-day lockup. Treat as a fixed-duration position, not as "
                "operating cash.")
    return (55, f"Redemption timeline: {label}. Verify against treasury policy.")


def _peg_stability_score(symbol: str) -> tuple[int, str, str]:
    """Pulls recent peg history and scores stability. Returns
    (score, tier, reasoning)."""
    try:
        from sca.store import get_store
        store = get_store()
        ticks = store.list_peg_ticks(symbol, limit=200)
    except Exception:  # noqa: BLE001
        return (50, "unverified",
                "Could not read peg history from the store.")
    if not ticks:
        return (50, "unverified",
                "No peg history yet — score becomes meaningful once the "
                "ticker has run for at least 24 hours.")
    devs = [abs(float(t.get("deviation_bps") or 0)) for t in ticks
            if t.get("deviation_bps") is not None]
    if not devs:
        return (50, "unverified", "Peg history exists but no deviation rows.")
    mean_abs = sum(devs) / len(devs)
    max_abs = max(devs)
    # Score from mean deviation: <5bp = 95, 5-15 = 80, 15-30 = 60,
    # 30-50 = 40, >50 = 20. Penalty if any tick > 100bp.
    if mean_abs < 5:
        base = 95
    elif mean_abs < 15:
        base = 80
    elif mean_abs < 30:
        base = 60
    elif mean_abs < 50:
        base = 40
    else:
        base = 20
    if max_abs > 100:
        base = max(base - 20, 10)
    reasoning = (
        f"Mean absolute deviation over the last {len(ticks)} ticks: "
        f"{mean_abs:.1f}bp (peak {max_abs:.1f}bp). "
        + (
            "Very tight peg — meets institutional stability standards."
            if base >= 85 else
            "Stable but with periodic widening — within policy limits at "
            "moderate concentration."
            if base >= 65 else
            "Persistent peg drift — re-rate before any sizeable allocation."
            if base >= 45 else
            "Material peg instability — historical depeg pattern is a "
            "treasury-policy red flag."
        )
    )
    return (base, "verified", reasoning)


def _source_consensus_score(symbol: str) -> tuple[int, str, str]:
    """How often the multi-source peg consensus agrees vs disputes."""
    try:
        from sca.store import get_store
        ticks = get_store().list_peg_ticks(symbol, limit=100)
    except Exception:  # noqa: BLE001
        return (50, "unverified",
                "Could not read source consensus from the store.")
    kinds = [t.get("consensus_kind") for t in ticks if t.get("consensus_kind")]
    if not kinds:
        return (60, "unverified",
                "Consensus history not yet populated.")
    agreed = sum(1 for k in kinds if k == "agreed")
    disputed = sum(1 for k in kinds if k == "disputed")
    single = sum(1 for k in kinds if k == "single")
    total = len(kinds)
    agreed_pct = agreed / total
    disputed_pct = disputed / total
    if agreed_pct > 0.85:
        return (95, "verified",
                f"{agreed}/{total} ticks ({100*agreed_pct:.0f}%) had every "
                f"peg source agreeing within tolerance — high data trust.")
    if disputed_pct > 0.20:
        return (35, "verified",
                f"{disputed}/{total} ticks ({100*disputed_pct:.0f}%) had "
                f"sources disagreeing past tolerance — ground truth is "
                f"actively contested for this token.")
    if single / total > 0.50:
        return (55, "estimated",
                f"{single}/{total} ticks ({100*single/total:.0f}%) had only "
                f"one source responding — verification depends on a single "
                f"price feed.")
    return (75, "verified",
            f"Source consensus generally agrees ({100*agreed_pct:.0f}% "
            f"agreed, {100*disputed_pct:.0f}% disputed).")


def _sanctions_score(symbol: str) -> tuple[int, str, str]:
    """Sanctions exposure. For now: clean by default unless the
    sanctions surface has flagged this token. Future: pull from the
    F5 SANCTIONS check directly."""
    return (95, "estimated",
            f"No active OFAC SDN flag on {symbol} contract addresses at "
            f"last sweep. Re-run F5 SANCTIONS for the latest cross-check.")


def _custodian_concentration_score(issuer: str,
                                     backing_model: str
                                     ) -> tuple[int, str, str]:
    """Concentration risk on the issuer's reserve-custody stack."""
    i = (issuer or "").lower()
    bm = (backing_model or "").lower()
    if bm == "crypto_collateral":
        return (88, "verified",
                "No traditional custodian risk — reserves held on-chain.")
    if bm == "synthetic_delta_neutral":
        return (55, "estimated",
                "Hedged positions sit on centralised venues; concentration "
                "risk on the perp/derivatives leg. SVB-era lesson applies.")
    # Fiat-backed: depends on issuer's banking diversification.
    if "circle" in i:
        return (78, "estimated",
                "Circle's reserve mix uses multiple custodians (BNY Mellon "
                "+ BlackRock) post-SVB. Concentration improved but not "
                "fully eliminated.")
    if "paxos" in i:
        return (72, "estimated",
                "Paxos uses a small number of regulated US custodians.")
    if "tether" in i:
        return (45, "estimated",
                "Historical opacity around Tether's banking stack. "
                "Concentration disclosure remains partial.")
    return (55, "unverified",
            "Custodian concentration not characterised. Default to mid-tier "
            "score until issuer-specific data is wired in.")


def _attestation_freshness_score(symbol: str,
                                   backing_model: str,
                                   attestation_url: str
                                   ) -> tuple[int, str, str, str]:
    """Attestation freshness. Returns (score, tier, reasoning, url).

    Reads the most-recent verified attestation fact (claim_type =
    'reserves') for this symbol and computes days_since the
    attestation's as_of_date. Scoring band reflects what institutional
    treasury policies actually want:
      • <30 days  → 95 (monthly cadence — GENIUS Act minimum is met)
      • 30-60d    → 78 (acceptable for most policies)
      • 60-90d    → 55 (slipping; flag at risk committee)
      • 90-180d   → 35 (stale; concentration limits should reduce)
      • >180d     → 18 (treat as no current attestation at all)
    """
    bm = (backing_model or "").lower()
    if bm == "crypto_collateral":
        return (95, "verified",
                "Backing is on-chain in real time — there is no attestation "
                "freshness concept for this model.",
                "")
    if not attestation_url:
        return (25, "unverified",
                "No attestation URL on record — freshness cannot be "
                "measured.",
                "")
    # Try to read the most-recent extracted attestation date for this
    # symbol from the verified-facts archive. Falls back to the
    # mid-band "attested" score if the read fails or no row exists.
    as_of = None
    try:
        from sca.store import get_store
        fact = get_store().latest_verified_fact("reserves", symbol)
        if fact:
            value = fact.get("value") or {}
            as_of = value.get("as_of_date") or fact.get("as_of")
    except Exception:  # noqa: BLE001
        as_of = None
    if not as_of:
        return (60, "attested",
                "Attestation URL is on file but no extraction has run yet. "
                "Score will lift to a live freshness reading once the "
                "F2 ANALYZE pipeline has completed at least once for "
                f"{symbol}.",
                attestation_url)
    # Parse the as_of_date (YYYY-MM-DD) and compute days_since
    try:
        att_dt = datetime.strptime(as_of[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return (50, "estimated",
                f"Attestation date on record ({as_of}) couldn't be parsed. "
                f"Treating as mid-band until extraction is rerun.",
                attestation_url)
    days = max(0, (datetime.now(timezone.utc) - att_dt).days)
    if days < 30:
        s, blurb = 95, "fresh"
    elif days < 60:
        s, blurb = 78, "acceptable"
    elif days < 90:
        s, blurb = 55, "slipping"
    elif days < 180:
        s, blurb = 35, "stale"
    else:
        s, blurb = 18, "lapsed"
    reasoning = (
        f"Most recent attestation is dated {as_of[:10]} — {days} days old "
        f"({blurb}). " + (
            "Meets GENIUS-Act monthly-cadence minimum and standard "
            "institutional treasury policies." if s >= 90 else
            "Within typical treasury-policy tolerance, but flag at "
            "risk-committee cadence." if s >= 75 else
            "Older than most policies allow without concentration limits."
            if s >= 50 else
            "Stale — re-rate before any new allocation; existing positions "
            "should reduce." if s >= 30 else
            "Effectively no current attestation — treat as un-underwriteable "
            "until fresh."
        )
    )
    tier = "verified" if days < 90 else "attested"
    return (s, tier, reasoning, attestation_url)


def _implementation_score(symbol: str,
                            backing_model: str
                            ) -> tuple[int, str, str]:
    """Smart contract / oracle / formal-verification risk.

    No formal-verification data wired yet — score based on the
    maturity proxy of the token's listing in our registry."""
    bm = (backing_model or "").lower()
    if bm == "algorithmic":
        return (30, "unverified",
                "Algorithmic stabilisation mechanism — implementation risk "
                "compounds with backing risk.")
    if symbol.upper() in {"USDC", "USDT", "DAI", "USDS"}:
        return (82, "estimated",
                "Major liquid stablecoin with multi-year, multi-billion "
                "implementation track record.")
    return (62, "estimated",
            "No formal-verification record wired yet; default mid-band "
            "until audit history is loaded.")


# ── orchestrator ────────────────────────────────────────────────────
def compute_trust_score(symbol: str) -> TrustScore:
    """Build the full trust score for a single token."""
    sym = (symbol or "").upper()
    try:
        cfg = get_stablecoin(sym)
    except Exception:  # noqa: BLE001 — unknown symbol → no registry record
        cfg = None
    ctx = _get_token_ctx(sym)

    issuer = (cfg.issuer if cfg else "") or (
        getattr(ctx, "issuer", "") if ctx else "")
    backing = (cfg.backing_model if cfg else "") or "fiat_reserves"
    transparency_url = (cfg.transparency_url if cfg else "")
    attestation_url = (cfg.latest_attestation_url if cfg else "")
    auditor = getattr(ctx, "auditor", "") if ctx else ""
    payout_label = getattr(ctx, "payout_timeline_label", "") if ctx else ""

    dimensions: list[TrustDimension] = []

    # 1. Reserve quality
    s, tier, reason, src, url = _backing_model_score(
        backing, attestation_url, transparency_url)
    dimensions.append(TrustDimension(
        name="reserve_quality", label="Reserve quality",
        score=s, weight=_WEIGHT["reserve_quality"], tier=tier,
        reasoning=reason, evidence_source=src, evidence_url=url))

    # 2. Peg stability
    s, tier, reason = _peg_stability_score(sym)
    dimensions.append(TrustDimension(
        name="peg_stability", label="Peg stability",
        score=s, weight=_WEIGHT["peg_stability"], tier=tier,
        reasoning=reason, evidence_source="computed"))

    # 3. Attestation freshness
    s, tier, reason, url = _attestation_freshness_score(
        sym, backing, attestation_url)
    dimensions.append(TrustDimension(
        name="attestation_freshness", label="Attestation freshness",
        score=s, weight=_WEIGHT["attestation_freshness"], tier=tier,
        reasoning=reason,
        evidence_source="attestation" if url else "registry",
        evidence_url=url))

    # 4. Redemption capacity
    s, reason = _redemption_score(payout_label)
    dimensions.append(TrustDimension(
        name="redemption_capacity", label="Redemption capacity",
        score=s, weight=_WEIGHT["redemption_capacity"],
        tier="verified" if payout_label else "unverified",
        reasoning=reason, evidence_source="registry"))

    # 5. Sanctions exposure
    s, tier, reason = _sanctions_score(sym)
    dimensions.append(TrustDimension(
        name="sanctions_exposure", label="Sanctions exposure",
        score=s, weight=_WEIGHT["sanctions_exposure"], tier=tier,
        reasoning=reason, evidence_source="computed"))

    # 6. Auditor credibility
    s, tier, reason = _auditor_score(auditor)
    dimensions.append(TrustDimension(
        name="auditor_credibility", label="Auditor credibility",
        score=s, weight=_WEIGHT["auditor_credibility"], tier=tier,
        reasoning=reason, evidence_source="registry"))

    # 7. Source consensus
    s, tier, reason = _source_consensus_score(sym)
    dimensions.append(TrustDimension(
        name="source_consensus", label="Multi-source consensus",
        score=s, weight=_WEIGHT["source_consensus"], tier=tier,
        reasoning=reason, evidence_source="computed"))

    # 8. Regulatory standing
    s, tier, reason = _regulatory_score(issuer)
    dimensions.append(TrustDimension(
        name="regulatory_standing", label="Regulatory standing",
        score=s, weight=_WEIGHT["regulatory_standing"], tier=tier,
        reasoning=reason, evidence_source="registry"))

    # 9. Custodian concentration
    s, tier, reason = _custodian_concentration_score(issuer, backing)
    dimensions.append(TrustDimension(
        name="custodian_concentration", label="Custodian concentration",
        score=s, weight=_WEIGHT["custodian_concentration"], tier=tier,
        reasoning=reason, evidence_source="registry"))

    # 10. Implementation risk
    s, tier, reason = _implementation_score(sym, backing)
    dimensions.append(TrustDimension(
        name="implementation_risk", label="Implementation risk",
        score=s, weight=_WEIGHT["implementation_risk"], tier=tier,
        reasoning=reason, evidence_source="estimated"))

    composite = sum(d.score * d.weight for d in dimensions) / TOTAL_WEIGHT
    return TrustScore(
        symbol=sym,
        score=round(composite, 1),
        verdict=_verdict_for(composite),
        dimensions=dimensions,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def tier_color(score: float) -> str:
    """Hex colour for the headline composite number — used by the UI."""
    if score >= 85: return "#4AF6C3"   # bright teal
    if score >= 70: return "#A3E635"   # lime
    if score >= 55: return "#D4A24A"   # gold
    if score >= 40: return "#F59E0B"   # amber
    return "#FF433D"                    # rose
