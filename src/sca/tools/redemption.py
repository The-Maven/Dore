"""Deterministic tool: classify reserve assets by redemption liquidity.

LAYER: facts. No LLM. Maps each attestation reserve-breakdown line to a
liquidity tier — how fast that asset can be turned into cash to fund holder
redemptions. Pure keyword classification; the agent interprets.

  liquid    cash, deposits, T-bills, repo, money-market funds
  illiquid  commercial paper, corporate debt, loans, bonds, other investments
  moderate  anything not clearly either
"""
from __future__ import annotations

from sca.models import Attestation, ReserveTier

_LIQUID = (
    "cash", "demand deposit", "deposit", "treasury bill", "t-bill",
    "treasuries", "treasury secur", "repurchase", "repo", "reverse repo",
    "money market", "overnight",
)
_ILLIQUID = (
    "commercial paper", "corporate", "loan", "bond", "equity",
    "other invest", "precious metal", "bitcoin", "secured loan",
)


def liquidity_tier(asset_class: str) -> str:
    """Classify a reserve asset class by redemption liquidity."""
    text = asset_class.lower()
    if any(k in text for k in _ILLIQUID):
        return "illiquid"
    if any(k in text for k in _LIQUID):
        return "liquid"
    return "moderate"


def classify_reserves(attestation: Attestation) -> list[ReserveTier]:
    """Tier every line of an attestation's reserve breakdown."""
    return [
        ReserveTier(
            asset_class=line.asset_class,
            amount=line.amount,
            tier=liquidity_tier(line.asset_class),
        )
        for line in attestation.breakdown
    ]


def liquid_reserves(tiers: list[ReserveTier]) -> float:
    """Total reserves redeemable fast — the `liquid` tier only."""
    return sum(t.amount for t in tiers if t.tier == "liquid")
