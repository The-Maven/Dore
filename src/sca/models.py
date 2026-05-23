"""Typed result models passed between the three layers.

Keeping these as plain dataclasses (not dicts) is what makes each layer
independently testable — every boundary has a known shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── tools layer ───────────────────────────────────────────────────────
@dataclass
class ChainSupply:
    chain: str
    contract: str
    raw: int
    decimals: int
    supply: float
    kind: str = "native"   # native | bridged — provenance
    verified: bool = True  # is this contract address verified (human OR auto)
    verification_method: str = ""  # 'human' | 'auto: on-chain symbol match' | '' — for the badge
    # Multi-RPC reliability metadata — surfaced in the UI to build trust.
    # `consensus` is a short label like "2/2 agree", "1/2 single source"
    # (fallback unreachable), or "DISAGREEMENT" (RPCs returned different
    # values — should never happen on a healthy chain, but if it does we
    # want it loud). `endpoint` records which RPC actually returned the
    # accepted value.
    consensus: str = ""
    endpoint: str = ""


@dataclass
class SupplyResult:
    symbol: str
    total_supply: float  # native issuance only — bridged excluded (no double-count)
    per_chain: list[ChainSupply] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    native_supply: float = 0.0
    bridged_supply: float = 0.0  # wrapped/bridged copies — shown, not summed in
    read_at: str = ""  # ISO timestamp — when these on-chain reads were taken
    # When any chain read fails entirely, `complete` flips to False — the
    # total is partial, not authoritative. UI must render this as PARTIAL.
    # A wrong "fully backed" judgement is the worst class of bug we ship;
    # this is the integrity gate that prevents it.
    complete: bool = True
    chains_expected: int = 0
    chains_read: int = 0
    failed_chains: list[str] = field(default_factory=list)

    @property
    def bridged_share(self) -> float | None:
        gross = self.native_supply + self.bridged_supply
        return self.bridged_supply / gross if gross else None


@dataclass
class ReserveLine:
    asset_class: str
    amount: float


@dataclass
class Attestation:
    symbol: str
    as_of_date: str
    total_reserves: float
    tokens_outstanding: float
    breakdown: list[ReserveLine] = field(default_factory=list)
    source_url: str = ""
    source_pages: list[int] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class Metrics:
    attested_coverage: float | None  # reserves / attested tokens — honest backing ratio
    live_coverage: float | None      # reserves / current supply — drift-affected
    staleness_days: int
    supply_drift: float | None
    # Provenance: how this metric was actually computed. Surfaced in the UI
    # so a user can see "computed across 5 chains, all RPCs corroborated"
    # rather than a bare number with no audit trail. Built from the supply
    # result's per-chain consensus + the attestation citation.
    provenance: str = ""


@dataclass
class AugmentedContext:
    """LLM-generated context that fills a gap an original source couldn't.

    Rendered in the UI clearly tagged as 'AI context' — never confused
    with a deterministic figure. The LLM is bounded: it provides
    qualitative context (backing model, attestation cadence, where to
    find live data) and cites sources where it can. It does NOT invent
    numeric reserves, supply, or coverage figures."""
    surface: str           # 'attestation' | 'sanctions' | 'redemption'
    reason: str            # why we're augmenting (e.g. 'no transparency_url',
                           # 'live fetch failed: 404', 'backing model is crypto')
    text: str              # the LLM-generated context
    citations: list[str] = field(default_factory=list)  # URLs the LLM cited
    confidence: str = ""   # 'training-data-only' | 'web-searched' | 'low'
    backing_model: str = ""  # echoed for the UI badge


@dataclass
class Check:
    """A deterministic guardrail result over a critical value."""

    name: str
    passed: bool
    severity: str  # info | warn | critical
    detail: str = ""


@dataclass
class Gap:
    """A structured shortcoming in an analysis — classified, not a bare string."""

    severity: str  # info | warn | critical
    category: str  # data | guardrail | citation | corpus | coverage
    message: str


# ── corpus layer ──────────────────────────────────────────────────────
@dataclass
class CorpusPassage:
    source_id: str
    section: str
    heading: str
    text: str
    citation: str
    page: int | None = None
    score: float = 0.0
    # True when a human has explicitly marked the source verified — a
    # quality signal surfaced as a badge, not a gate on whether it's used.
    source_verified: bool = False


# ── agent layer ───────────────────────────────────────────────────────
@dataclass
class Analysis:
    symbol: str
    supply: SupplyResult
    attestation: Attestation | None = None
    metrics: Metrics | None = None
    passages: list[CorpusPassage] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    narrative: str = ""
    gaps: list[Gap] = field(default_factory=list)
    # LLM-generated context to fill verified-source gaps. Tagged distinctly
    # in the UI ("AI context") and never confused with deterministic figures.
    augmentations: list[AugmentedContext] = field(default_factory=list)
    # The token's backing model — drives UI framing of the attestation
    # surface. fiat_reserves means an attestation SHOULD exist; crypto_collateral
    # / synthetic / algorithmic mean a different verification path applies.
    backing_model: str = "fiat_reserves"
    protocol_url: str = ""
    # AI Brief — editorial top-of-view synthesis. Optional; UI omits the
    # panel if absent rather than rendering placeholder. Serialised as a
    # dict so the SPA can render without mirroring the dataclass.
    brief: dict | None = None


# ── evals ─────────────────────────────────────────────────────────────
@dataclass
class EvalPointResult:
    point: str
    passed: bool
    detail: str = ""


@dataclass
class EvalCaseResult:
    case_id: str
    symbol: str
    points: list[EvalPointResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.points) and all(p.passed for p in self.points)


# ── sanctions surface ─────────────────────────────────────────────────
@dataclass
class SdnAddress:
    """A digital-currency address on the OFAC SDN list."""

    address: str
    currency: str   # ETH, XBT, TRX, USDT, ...
    sdn_name: str    # the sanctioned entity it belongs to
    sdn_uid: str


@dataclass
class SanctionsScreen:
    symbol: str
    supply: SupplyResult
    screened: list[str] = field(default_factory=list)  # addresses screened
    hits: list[SdnAddress] = field(default_factory=list)  # OFAC matches
    sdn_publish_date: str = ""
    sdn_staleness_days: int | None = None
    sdn_address_count: int = 0  # sanctioned crypto addresses in the list
    checks: list[Check] = field(default_factory=list)
    passages: list[CorpusPassage] = field(default_factory=list)
    narrative: str = ""
    gaps: list[Gap] = field(default_factory=list)
    # LLM-generated context to fill verified-source gaps (SDN unavailable,
    # critically stale, or no addresses to screen). Tagged distinctly in
    # the UI ("AI context") and never replaces a deterministic match.
    augmentations: list[AugmentedContext] = field(default_factory=list)
    # The token's backing model + live-data link — echoed onto every
    # surface so the UI can render the "BACKING MODEL" strip without
    # re-fetching the registry.
    backing_model: str = "fiat_reserves"
    protocol_url: str = ""
    brief: dict | None = None

    @property
    def clean(self) -> bool:
        return not self.hits


# ── redemption surface ────────────────────────────────────────────────
@dataclass
class ReserveTier:
    """A reserve-breakdown line classified by how fast it can fund redemptions."""

    asset_class: str
    amount: float
    tier: str  # liquid | moderate | illiquid


@dataclass
class RedemptionAssessment:
    symbol: str
    supply: SupplyResult
    attestation: Attestation | None = None
    metrics: Metrics | None = None
    tiers: list[ReserveTier] = field(default_factory=list)
    liquid_reserves: float = 0.0          # reserves redeemable fast
    liquid_coverage: float | None = None  # liquid reserves / on-chain supply
    net_redemption_flow: float | None = None  # supply change since attestation
    checks: list[Check] = field(default_factory=list)
    passages: list[CorpusPassage] = field(default_factory=list)
    narrative: str = ""
    gaps: list[Gap] = field(default_factory=list)
    # LLM-generated context — surfaces when there is no attestation to
    # tier OR when the backing model isn't fiat (crypto / synthetic /
    # algorithmic), where redemption is on-chain via the protocol. Tagged
    # distinctly in the UI ("AI context") and never carries numeric
    # reserves or redemption limits.
    augmentations: list[AugmentedContext] = field(default_factory=list)
    # The token's backing model + live-data link — echoed onto every
    # surface so the UI can render the "BACKING MODEL" strip without
    # re-fetching the registry.
    backing_model: str = "fiat_reserves"
    protocol_url: str = ""
    brief: dict | None = None
