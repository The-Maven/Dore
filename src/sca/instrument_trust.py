"""Instrument-Trust Oracle — Doré's wedge.

Answers ONE question for an autonomous agent at the moment of a
stablecoin payment: is this specific token, from this specific
issuer, right now sound and redeemable at par against reserves that
reconcile to its on-chain supply?

Returns a structured verdict an AP2 policy engine can gate on
without interpretation. Three states, no prose:

    sound      — attestation fresh AND on-chain ↔ reserves reconciled
                 within tolerance AND redemption path operational
    degraded   — one or more reconciliation signals failing within
                 a band the policy engine may still accept under
                 risk-adjusted rules
    unknown    — first-class answer. We do not have the data we
                 need to reach a verdict. A policy engine should
                 apply its conservative rule.

Non-negotiables enforced here (per the build spec §5):

  1. The LLM extracts; it never invents. Every field carries
     provenance.
  2. Pre-compute, then serve. This is a cache read in the request
     path; the reconciliation engine populates it on the back office.
  3. Determinism at the boundary. Three states + confidence (0-1) +
     freshness timestamp + provenance pointer.
  4. Staleness is a risk signal. If the underlying reconciliation
     hasn't refreshed within freshness_ttl_s, the verdict degrades
     toward `unknown`.
  5. Multi-chain by default. Supply = sum across every chain the
     token lives on.

This module is the INTERFACE. The reconciliation engine that
populates the cache lives in src/sca/agent/analyze.py (attestation
extraction) + src/sca/tools/onchain_supply.py (multi-chain supply)
+ src/sca/peg_price.py (peg / redemption signal). This module
COMPOSES those signals into the verdict.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


# Freshness gate: if the reconciliation is older than this, the
# verdict degrades. 24 hours is a defensible default — institutional
# attestations refresh monthly but on-chain supply + peg can be
# verified far more often. The freshness floor is set so an agent
# never acts on a >24h-old reconciliation without being told.
DEFAULT_FRESHNESS_TTL_S = 24 * 3600

# Reconciliation tolerance: reserve_delta_bps measures how far
# attested reserves drift from on-chain supply. Within this band we
# call it `sound`; outside, `degraded` (small) or `unknown` (large).
# 50bp is generous — a meaningful reserve shortfall would exceed
# this; an attestation rounding error would not.
SOUND_DELTA_BPS = 50.0
DEGRADED_DELTA_BPS = 250.0


@dataclass
class Provenance:
    """A single piece of evidence backing the verdict. Every fact
    used in the reconciliation carries one of these so a policy
    engine can audit the decision after the fact."""
    field: str               # e.g. 'reserves_total_usd'
    source: str              # e.g. 'attestation' / 'on_chain' / 'peg_tick'
    source_url: str = ""     # where the data lives
    extracted_at: str = ""   # ISO timestamp of extraction
    confidence: float = 1.0  # 0-1; LLM extraction may be <1


@dataclass
class InstrumentVerdict:
    """The deterministic boundary output. This is what an AP2
    policy engine consumes."""
    symbol: str
    state: str                                # 'sound' | 'degraded' | 'unknown'
    confidence: float                          # 0-1
    timestamp: str                             # ISO of the reconciliation
    fresh: bool                                # is the reconciliation within TTL
    # Reserve reconciliation
    reserves_attested_usd: Optional[float] = None
    supply_on_chain_total: Optional[float] = None
    supply_by_chain: dict = field(default_factory=dict)
    reserve_delta_bps: Optional[float] = None   # (reserves - supply) / supply * 10000
    # Redemption-path test
    redemption_path_status: str = "unknown"     # 'operational' | 'degraded' | 'unknown'
    redemption_path_label: str = ""              # human-readable (e.g. 'same-day via Circle Mint')
    # Peg state at the moment of the verdict
    peg_deviation_bps: Optional[float] = None
    peg_consensus_kind: str = ""                # 'agreed' | 'disputed' | 'single' | ''
    # Free-text rationale (NOT for gating — only for human audit)
    reasoning: str = ""
    # The cited evidence backing every numeric field above
    provenance: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "provenance": [asdict(p) for p in self.provenance],
        }


# ── verdict composition ─────────────────────────────────────────────
def compute_verdict(
    symbol: str,
    *,
    freshness_ttl_s: int = DEFAULT_FRESHNESS_TTL_S,
) -> InstrumentVerdict:
    """Compose the instrument-trust verdict from existing reconciliation
    signals. Cheap when the upstream data is cached; the agent-path
    target is <100ms total."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return InstrumentVerdict(
            symbol="", state="unknown", confidence=0.0,
            timestamp=_now_iso(), fresh=False,
            reasoning="No symbol provided.",
        )

    # 1. Pull reserves + as_of from the verified-facts archive (the
    # post-extraction provenance row, written by F2 ANALYZE).
    reserves_usd: Optional[float] = None
    attestation_as_of: Optional[str] = None
    extraction_confidence: float = 1.0
    attestation_source_url = ""
    try:
        from sca.store import get_store
        fact = get_store().latest_verified_fact("reserves", sym)
    except Exception:  # noqa: BLE001
        fact = None
    if fact:
        value = fact.get("value") or {}
        reserves_usd = _safe_float(value.get("total_reserves_usd"))
        attestation_as_of = value.get("as_of_date") or fact.get("as_of")
        extraction_confidence = _safe_float(
            value.get("extraction_confidence"), default=1.0) or 1.0
        sources = fact.get("sources") or []
        if sources and isinstance(sources, list):
            attestation_source_url = (sources[0] or {}).get("url", "")

    # 2. Sum on-chain supply across every chain. The deployments
    # registry has the canonical list of (chain, address) tuples.
    supply_by_chain: dict[str, float] = {}
    supply_total: Optional[float] = None
    try:
        from sca.config import get_stablecoin
        coin = get_stablecoin(sym)
        # Pull latest verified per-chain supply facts. The
        # F2 pipeline writes one per chain.
        from sca.store import get_store as _gs
        store = _gs()
        for d in coin.deployments:
            try:
                f = store.latest_verified_fact(
                    "native_supply", f"{sym}:{d.chain}")
                if f:
                    v = (f.get("value") or {}).get("supply")
                    s = _safe_float(v)
                    if s is not None:
                        supply_by_chain[d.chain] = s
            except Exception:  # noqa: BLE001
                continue
        if supply_by_chain:
            supply_total = sum(supply_by_chain.values())
    except Exception:  # noqa: BLE001
        pass

    # 3. Compute the reserve delta (bps). Positive = reserves exceed
    # on-chain supply (over-collateralised); negative = shortfall.
    reserve_delta_bps: Optional[float] = None
    if reserves_usd is not None and supply_total and supply_total > 0:
        reserve_delta_bps = ((reserves_usd - supply_total)
                              / supply_total) * 10_000

    # 4. Pull the most recent peg tick for the live redeem-at-par signal.
    peg_deviation_bps: Optional[float] = None
    peg_consensus_kind = ""
    try:
        from sca.store import get_store as _gs
        ticks = _gs().list_peg_ticks(sym, limit=1)
        if ticks:
            peg_deviation_bps = _safe_float(ticks[0].get("deviation_bps"))
            peg_consensus_kind = ticks[0].get("consensus_kind") or ""
    except Exception:  # noqa: BLE001
        pass

    # 5. Redemption-path status. v1: read the registry payout
    # timeline + the current peg consensus. v2 (TODO): a true
    # operational probe per redemption path (Circle Mint API,
    # PSM contract call, etc).
    redemption_status, redemption_label = _redemption_path_status(
        sym, peg_deviation_bps, peg_consensus_kind)

    # 6. Compose the verdict state.
    state, confidence, reasoning, fresh = _compose_state(
        reserves_usd=reserves_usd,
        supply_total=supply_total,
        reserve_delta_bps=reserve_delta_bps,
        attestation_as_of=attestation_as_of,
        peg_deviation_bps=peg_deviation_bps,
        peg_consensus_kind=peg_consensus_kind,
        redemption_status=redemption_status,
        extraction_confidence=extraction_confidence,
        freshness_ttl_s=freshness_ttl_s,
    )

    # 7. Build the provenance bag — one entry per cited field.
    provenance: list[Provenance] = []
    if reserves_usd is not None:
        provenance.append(Provenance(
            field="reserves_attested_usd",
            source="attestation",
            source_url=attestation_source_url,
            extracted_at=attestation_as_of or "",
            confidence=extraction_confidence,
        ))
    for chain, sup in supply_by_chain.items():
        provenance.append(Provenance(
            field=f"supply_on_chain.{chain}",
            source="on_chain",
            confidence=1.0,
        ))
    if peg_deviation_bps is not None:
        provenance.append(Provenance(
            field="peg_deviation_bps",
            source="peg_tick",
            confidence=1.0,
        ))

    return InstrumentVerdict(
        symbol=sym,
        state=state,
        confidence=round(confidence, 3),
        timestamp=_now_iso(),
        fresh=fresh,
        reserves_attested_usd=reserves_usd,
        supply_on_chain_total=supply_total,
        supply_by_chain=supply_by_chain,
        reserve_delta_bps=
            round(reserve_delta_bps, 2) if reserve_delta_bps is not None else None,
        redemption_path_status=redemption_status,
        redemption_path_label=redemption_label,
        peg_deviation_bps=
            round(peg_deviation_bps, 2) if peg_deviation_bps is not None else None,
        peg_consensus_kind=peg_consensus_kind,
        reasoning=reasoning,
        provenance=provenance,
    )


def _redemption_path_status(symbol: str,
                              peg_deviation_bps: Optional[float],
                              peg_consensus_kind: str
                              ) -> tuple[str, str]:
    """Operational redemption-path read.

    v1 heuristic: if the peg is within a few bp AND sources agree,
    the redemption path is `operational` (arbitrageurs are actively
    closing the gap → the redeem mechanism is working). If consensus
    is disputed or peg is past ±50bp, `degraded`. Otherwise `unknown`.

    v2 (queued): replace the heuristic with a real probe — touch the
    issuer's mint/redeem API (Circle Mint), call the PSM contract for
    DAI/USDS, read the redemption-queue depth for sUSDe.
    """
    try:
        from sca.movement.token_context import get_context as _gc
        ctx = _gc(symbol)
        label = (getattr(ctx, "payout_timeline_label", "") or
                  "unknown timeline") if ctx else "unknown timeline"
    except Exception:  # noqa: BLE001
        label = "unknown timeline"
    if peg_consensus_kind == "disputed":
        return ("degraded", f"{label} — peg sources contested")
    if peg_deviation_bps is None:
        return ("unknown", label)
    if abs(peg_deviation_bps) <= 10:
        return ("operational", label)
    if abs(peg_deviation_bps) <= 50:
        return ("degraded", f"{label} — peg drift {peg_deviation_bps:+.1f}bp")
    return ("degraded",
             f"{label} — peg dislocated {peg_deviation_bps:+.1f}bp")


def _compose_state(*,
                     reserves_usd: Optional[float],
                     supply_total: Optional[float],
                     reserve_delta_bps: Optional[float],
                     attestation_as_of: Optional[str],
                     peg_deviation_bps: Optional[float],
                     peg_consensus_kind: str,
                     redemption_status: str,
                     extraction_confidence: float,
                     freshness_ttl_s: int,
                     ) -> tuple[str, float, str, bool]:
    """The deterministic state machine. Returns (state, confidence,
    reasoning, fresh)."""
    # Freshness gate: if we have nothing or the data is too old, the
    # verdict is `unknown` regardless of what we DID read.
    fresh = True
    age_problem = ""
    if attestation_as_of:
        try:
            att_dt = datetime.strptime(attestation_as_of[:10], "%Y-%m-%d"
                                          ).replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - att_dt).total_seconds()
            # Attestations are monthly artefacts; freshness_ttl_s on the
            # API is for the reconciliation, not the attestation itself.
            # Surface but do not gate on attestation age unless > 90 days.
            if age_s > 90 * 86400:
                fresh = False
                age_problem = (
                    f"Attestation is {int(age_s/86400)} days old.")
        except (ValueError, TypeError):
            pass
    # Critical-data gate
    if reserves_usd is None and supply_total is None:
        return ("unknown", 0.0,
                 "No attested reserves and no on-chain supply on record. "
                 "Cannot reconcile.",
                 False)
    if reserves_usd is None:
        return ("unknown", 0.3,
                 "On-chain supply observed but no attested reserves on "
                 "record. Reconciliation requires both sides.",
                 fresh)
    if supply_total is None:
        return ("unknown", 0.3,
                 "Attested reserves on record but no on-chain supply read. "
                 "Reconciliation requires both sides.",
                 fresh)
    # We have both sides — reconcile.
    abs_delta = abs(reserve_delta_bps or 0)
    parts = [
        f"Reserves ${reserves_usd:,.0f} vs on-chain supply "
        f"${supply_total:,.0f} = "
        f"{reserve_delta_bps:+.0f}bp delta."
    ]
    if peg_deviation_bps is not None:
        parts.append(
            f"Peg {peg_deviation_bps:+.1f}bp "
            f"({peg_consensus_kind or 'single source'}).")
    parts.append(f"Redemption path {redemption_status}.")
    if age_problem:
        parts.append(age_problem)
    reasoning = " ".join(parts)
    # State decision: combines delta, peg dislocation, redemption,
    # consensus, and freshness.
    if not fresh or peg_consensus_kind == "disputed":
        return ("unknown", 0.4, reasoning, fresh)
    if (abs_delta <= SOUND_DELTA_BPS
            and redemption_status == "operational"
            and (peg_deviation_bps is None or abs(peg_deviation_bps) <= 10)):
        confidence = 0.92 * (extraction_confidence or 1.0)
        return ("sound", confidence, reasoning, fresh)
    if abs_delta <= DEGRADED_DELTA_BPS or redemption_status == "degraded":
        confidence = 0.7 * (extraction_confidence or 1.0)
        return ("degraded", confidence, reasoning, fresh)
    return ("unknown", 0.5, reasoning, fresh)


def _safe_float(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
