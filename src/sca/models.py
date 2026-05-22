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
    verified: bool = True  # was this contract address human-verified


@dataclass
class SupplyResult:
    symbol: str
    total_supply: float  # native issuance only — bridged excluded (no double-count)
    per_chain: list[ChainSupply] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    native_supply: float = 0.0
    bridged_supply: float = 0.0  # wrapped/bridged copies — shown, not summed in
    read_at: str = ""  # ISO timestamp — when these on-chain reads were taken

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
