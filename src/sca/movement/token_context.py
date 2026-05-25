"""Per-token structural context — the cheat sheet behind AI Commentary.

For each stablecoin, name the structural facts that a sophisticated
investor needs to interpret a peg deviation reading:
  - Backing model (fiat reserves / crypto collateral / synthetic
    delta-neutral / tokenised T-bill / algorithmic)
  - Attestation cadence + auditor + transparency URL
  - Regulatory regime + issuer jurisdiction
  - Peg target (most are 1.00 but USDY / sUSDe / syrupUSDC / OUSG
    drift above by design — yield-bearing notes)
  - What signal is worth watching (cone width thresholds, paired
    metrics like collateralization ratio or PSM utilisation)

This is the cheat sheet the LLM Commentary card grounds itself in.
Plus the current forecast + delta. Plus citations.

Source-of-truth precedence: this dict is the BOOTSTRAP. The
attestation URL stored here is the editorial fallback; the live
attestation_url_overrides table wins when it's populated.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenContext:
    symbol: str
    issuer: str
    backing_model: str       # human label
    backing_short: str       # 4-8 word elevator
    expected_peg: float      # 1.0 for fiat-pegged; >1 for yield-bearing
    cadence: str             # 'monthly' / 'quarterly' / 'on-chain real-time' / 'NAV-rebased'
    auditor: str             # firm name when applicable
    transparency_url: str
    watchlist_signal: str    # what to watch beyond peg
    structural_one_liner: str  # 1 sentence the LLM card opens with
    cone_thresholds_bps: tuple[float, float]  # (normal max, alert threshold)


_REGISTRY: dict[str, TokenContext] = {
    "USDC": TokenContext(
        symbol="USDC", issuer="Circle",
        backing_model="fiat_reserves",
        backing_short="Cash + short-duration U.S. Treasuries via BlackRock-managed reserve fund",
        expected_peg=1.0, cadence="monthly", auditor="Deloitte",
        transparency_url="https://www.circle.com/transparency",
        watchlist_signal="Cone widening past ±5bp alongside negative basis on Curve 3pool",
        structural_one_liner=(
            "USDC behaves like a money-market wrapper — sub-5bp "
            "deviations are normalised by authorised participants "
            "within minutes."
        ),
        cone_thresholds_bps=(5.0, 15.0),
    ),
    "USDT": TokenContext(
        symbol="USDT", issuer="Tether",
        backing_model="fiat_reserves",
        backing_short="Treasuries + secured loans + gold + bitcoin (mixed reserves)",
        expected_peg=1.0, cadence="quarterly", auditor="BDO",
        transparency_url="https://tether.to/en/transparency",
        watchlist_signal="Cone widening past ±10bp + persistent negative basis on Curve",
        structural_one_liner=(
            "USDT runs a broader, slower-attested reserve mix than "
            "USDC; a 4–8bp cone is normal microstructure noise."
        ),
        cone_thresholds_bps=(8.0, 20.0),
    ),
    "DAI": TokenContext(
        symbol="DAI", issuer="Sky (MakerDAO)",
        backing_model="crypto_collateral",
        backing_short="Crypto + RWA collateral, peg held by 1:1 PSM swap against USDC",
        expected_peg=1.0, cadence="on-chain real-time", auditor="—",
        transparency_url="https://daistats.com",
        watchlist_signal="Collateralization ratio falling below 150% system-wide",
        structural_one_liner=(
            "DAI's peg is maintained by the MakerDAO PSM — sub-2bp "
            "moves are PSM mechanics, not collateral stress."
        ),
        cone_thresholds_bps=(3.0, 12.0),
    ),
    "PYUSD": TokenContext(
        symbol="PYUSD", issuer="PayPal (via Paxos Trust)",
        backing_model="fiat_reserves",
        backing_short="USD deposits + short-term U.S. Treasuries, NYDFS-supervised",
        expected_peg=1.0, cadence="monthly", auditor="Withum",
        transparency_url="https://www.paypal.com/us/cshelp/article/what-is-paypal-usd-pyusd-help1006",
        watchlist_signal="Cone widening alongside payment-rail outage chatter",
        structural_one_liner=(
            "PYUSD is bank-issued via Paxos under NYDFS supervision — "
            "tight regulatory perimeter; deviations are unusual."
        ),
        cone_thresholds_bps=(4.0, 12.0),
    ),
    "USDP": TokenContext(
        symbol="USDP", issuer="Paxos",
        backing_model="fiat_reserves",
        backing_short="USD deposits + short-term U.S. Treasuries, NYDFS-supervised",
        expected_peg=1.0, cadence="monthly", auditor="Withum",
        transparency_url="https://paxos.com/usdp-transparency/",
        watchlist_signal="Cone widening past 5bp",
        structural_one_liner=(
            "USDP is Paxos' flagship NYDFS-supervised dollar — "
            "monthly attestations; supply has been shrinking in 2026."
        ),
        cone_thresholds_bps=(4.0, 12.0),
    ),
    "TUSD": TokenContext(
        symbol="TUSD", issuer="TrueUSD / Techteryx",
        backing_model="fiat_reserves",
        backing_short="USD reserves with real-time MoonPay-Chainlink proof-of-reserves",
        expected_peg=1.0, cadence="real-time on-chain", auditor="The Network Firm",
        transparency_url="https://real-time-attest.trustexplorer.io/trueusd",
        watchlist_signal="Cone widening past 10bp + PoR feed staleness",
        structural_one_liner=(
            "TUSD uses on-chain proof-of-reserves but transparency "
            "events in 2023–24 leave a wider cone profile than peers."
        ),
        cone_thresholds_bps=(10.0, 30.0),
    ),
    "FDUSD": TokenContext(
        symbol="FDUSD", issuer="First Digital Trust (Hong Kong)",
        backing_model="fiat_reserves",
        backing_short="USD + Treasuries, Hong Kong-domiciled trust",
        expected_peg=1.0, cadence="monthly", auditor="Prescient Assurance",
        transparency_url="https://firstdigitallabs.com/transparency",
        watchlist_signal="Cone widening past 8bp + Hong Kong regulatory news",
        structural_one_liner=(
            "FDUSD is Hong Kong-trust-issued and Binance-dominant in "
            "volume — its cone reflects single-venue concentration."
        ),
        cone_thresholds_bps=(8.0, 25.0),
    ),
    "GUSD": TokenContext(
        symbol="GUSD", issuer="Gemini",
        backing_model="fiat_reserves",
        backing_short="USD deposits at State Street, NYDFS-supervised",
        expected_peg=1.0, cadence="monthly", auditor="BPM",
        transparency_url="https://www.gemini.com/dollar",
        watchlist_signal="Cone widening past 6bp",
        structural_one_liner=(
            "GUSD is Gemini's NYDFS-supervised dollar — small float "
            "with a tight regulatory perimeter."
        ),
        cone_thresholds_bps=(6.0, 18.0),
    ),
    "USDe": TokenContext(
        symbol="USDe", issuer="Ethena Labs",
        backing_model="synthetic_delta_neutral",
        backing_short="ETH + BTC + LSTs hedged with perp shorts; yield from funding",
        expected_peg=1.0, cadence="on-chain attested",
        auditor="Chaos Labs (risk) + Harris & Trotter (reserves)",
        transparency_url="https://app.ethena.fi/dashboards/transparency",
        watchlist_signal="Negative perp funding regime + cone widening",
        structural_one_liner=(
            "USDe is delta-neutral synthetic — its peg holds while "
            "perp funding is positive, wobbles when funding inverts."
        ),
        cone_thresholds_bps=(8.0, 25.0),
    ),
    "sUSDe": TokenContext(
        symbol="sUSDe", issuer="Ethena Labs",
        backing_model="synthetic_delta_neutral",
        backing_short="Staked USDe — drifts above $1 by design as yield accrues",
        expected_peg=1.0,  # actually drifts up; commentary names this
        cadence="on-chain real-time", auditor="Chaos Labs",
        transparency_url="https://app.ethena.fi/dashboards/transparency",
        watchlist_signal="sUSDe/USDe ratio (the yield curve)",
        structural_one_liner=(
            "sUSDe is the staked wrapper over USDe — drifts above "
            "$1 by design as yield accrues. NOT a 1:1 peg target."
        ),
        cone_thresholds_bps=(15.0, 50.0),
    ),
    "FRAX": TokenContext(
        symbol="FRAX", issuer="Frax Finance",
        backing_model="hybrid",
        backing_short="Hybrid collateral + algorithmic stabilization (v3)",
        expected_peg=1.0, cadence="on-chain", auditor="—",
        transparency_url="https://app.frax.finance/",
        watchlist_signal="Collateral ratio drops below 100%",
        structural_one_liner=(
            "FRAX v3 is sUSD-backed and operates more like a "
            "narrow-bank reserve than its earlier algorithmic form."
        ),
        cone_thresholds_bps=(6.0, 18.0),
    ),
    "GHO": TokenContext(
        symbol="GHO", issuer="Aave",
        backing_model="crypto_collateral",
        backing_short="Over-collateralised crypto via Aave v3 facilitator model",
        expected_peg=1.0, cadence="on-chain real-time", auditor="—",
        transparency_url="https://aave.com/docs/concepts/gho",
        watchlist_signal="GHO mint utilisation > 90% of facilitator cap",
        structural_one_liner=(
            "GHO is Aave's over-collateralised dollar — peg held by "
            "facilitator-controlled mint/burn against borrower demand."
        ),
        cone_thresholds_bps=(8.0, 25.0),
    ),
    "crvUSD": TokenContext(
        symbol="crvUSD", issuer="Curve Finance",
        backing_model="crypto_collateral",
        backing_short="LLAMMA (lending-liquidating AMM) backed by major crypto",
        expected_peg=1.0, cadence="on-chain real-time", auditor="—",
        transparency_url="https://crvusd.curve.finance/",
        watchlist_signal="LLAMMA bands inverting on largest market",
        structural_one_liner=(
            "crvUSD uses LLAMMA — bands soft-liquidate borrowers "
            "before bad debt; the peg is a downstream of band integrity."
        ),
        cone_thresholds_bps=(8.0, 30.0),
    ),
    "LUSD": TokenContext(
        symbol="LUSD", issuer="Liquity",
        backing_model="crypto_collateral",
        backing_short="ETH-only over-collateralised, redeemable 1:1 against $1.00",
        expected_peg=1.0, cadence="on-chain real-time", auditor="—",
        transparency_url="https://www.liquity.org/",
        watchlist_signal="Recovery Mode trigger (TCR < 150%)",
        structural_one_liner=(
            "LUSD is ETH-backed and directly redeemable for $1.00 — "
            "the hardest crypto-collateral peg on the rail."
        ),
        cone_thresholds_bps=(5.0, 18.0),
    ),
    "USDD": TokenContext(
        symbol="USDD", issuer="TRON DAO Reserve",
        backing_model="crypto_collateral",
        backing_short="TRX + BTC + USDT reserves; non-USA",
        expected_peg=1.0, cadence="reserves dashboard", auditor="—",
        transparency_url="https://usdd.io/",
        watchlist_signal="Cone widening past 15bp",
        structural_one_liner=(
            "USDD is over-collateralised by TRX/BTC reserves — "
            "TRON-network-centric; thinner peg-defence than DAI."
        ),
        cone_thresholds_bps=(15.0, 60.0),
    ),
    "USDS": TokenContext(
        symbol="USDS", issuer="Sky (MakerDAO successor)",
        backing_model="crypto_collateral",
        backing_short="Sky's USDS — DAI upgrade path with same crypto + RWA backing",
        expected_peg=1.0, cadence="on-chain real-time", auditor="—",
        transparency_url="https://sky.money/",
        watchlist_signal="USDS/DAI conversion flow",
        structural_one_liner=(
            "USDS is Sky's flagship dollar — DAI's successor with "
            "the same PSM mechanics and a yield-bearing sUSDS sibling."
        ),
        cone_thresholds_bps=(3.0, 12.0),
    ),
    "RLUSD": TokenContext(
        symbol="RLUSD", issuer="Ripple (Standard Custody, NYDFS trust)",
        backing_model="fiat_reserves",
        backing_short="Cash + short-dated U.S. Treasuries, BNY Mellon custody",
        expected_peg=1.0, cadence="monthly", auditor="Deloitte",
        transparency_url="https://ripple.com/rlusd",
        watchlist_signal="Cone widening past 5bp",
        structural_one_liner=(
            "RLUSD is Ripple's NYDFS-trust-issued dollar — banking-"
            "grade reserves with Deloitte attestation."
        ),
        cone_thresholds_bps=(4.0, 12.0),
    ),
    "USDY": TokenContext(
        symbol="USDY", issuer="Ondo Finance",
        backing_model="tokenised_treasury",
        backing_short="Tokenised note over short-dated Treasuries + bank deposits",
        expected_peg=1.0,  # actually drifts above $1 as yield accrues
        cadence="NAV-rebased daily", auditor="Ankura",
        transparency_url="https://ondo.finance/usdy",
        watchlist_signal="NAV stale-feed (>24h)",
        structural_one_liner=(
            "USDY is a yield-bearing tokenised note — drifts above "
            "$1.00 by design as yield accrues. NOT a 1:1 peg target."
        ),
        cone_thresholds_bps=(50.0, 150.0),  # wide because it drifts up
    ),
    "USDM": TokenContext(
        symbol="USDM", issuer="Mountain Protocol (Bermuda BMA)",
        backing_model="tokenised_treasury",
        backing_short="UST-backed, yield-bearing, Bermuda-regulated",
        expected_peg=1.0,  # yield-bearing; drifts
        cadence="daily NAV", auditor="Nephila Capital",
        transparency_url="https://mountainprotocol.com/",
        watchlist_signal="NAV-feed staleness",
        structural_one_liner=(
            "USDM is Bermuda-regulated and yield-bearing — drifts "
            "above $1 by design. Treat as a tokenised MMF, not a peg."
        ),
        cone_thresholds_bps=(40.0, 120.0),
    ),
}


def get_context(symbol: str) -> TokenContext | None:
    """Look up the cheat sheet for a symbol. Case-sensitive — most
    stablecoin tickers ARE case-sensitive (crvUSD, sUSDe). Tries
    upper-case as a fallback."""
    if symbol in _REGISTRY:
        return _REGISTRY[symbol]
    if symbol.upper() in _REGISTRY:
        return _REGISTRY[symbol.upper()]
    return None


def all_known_symbols() -> list[str]:
    """All tokens with a structural context entry."""
    return list(_REGISTRY.keys())
