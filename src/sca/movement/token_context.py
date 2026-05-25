"""Per-token structural context — the cheat sheet behind AI Commentary.

For each stablecoin, name the structural facts that a sophisticated
investor needs to interpret a peg deviation reading:
  - Backing model (fiat reserves / crypto collateral / synthetic
    delta-neutral / tokenised T-bill / algorithmic)
  - Attestation cadence + auditor + transparency URL
  - Regulatory regime + issuer jurisdiction
  - Peg target (most are 1.00 but USDY / sUSDe / syrupUSDC / OUSG
    have NAVs that climb above $1.00 as yield accrues — though the
    SECONDARY-MARKET price can trade above OR below NAV depending on
    liquidity / redemption-fee dynamics)
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
    # — v3.2 additions —
    # When True, the token's NAV climbs above $1.00 by design as yield
    # accrues (tokenised T-bills, staked wrappers, NAV-rebased notes).
    # The SECONDARY-MARKET price (what we poll) can trade above OR
    # below NAV depending on liquidity / redemption-fee dynamics; a
    # negative bp reading is NOT a peg violation, it's the discount
    # investors pay for instant exit (vs the issuer's redemption queue).
    # The UI tags these tokens with a YLD chip and the engine does
    # not flag secondary-market drift as an alert.
    yield_bearing: bool = False
    # Primary venue type for traders moving real volume. CEX = listed
    # on a centralised exchange (Coinbase, Kraken, Binance). DEX =
    # primary liquidity sits on Curve / Uniswap / Balancer pools.
    # MIXED = meaningful volume on both. Used to colour the source
    # tooltip and to set commentary tone.
    venue_type: str = "CEX"   # CEX | DEX | MIXED
    # Risk / yield profile in plain English — what an investor stands
    # to gain or lose from holding this token relative to its peg or
    # NAV. Short, hedged, no advice. Renders inside Commentary.
    pl_lens: str = ""
    # Payout / redemption timeline — how quickly a holder can convert
    # to USD via the issuer's mint/redeem path. Different mechanisms
    # have very different time-to-cash economics: a PSM swap is
    # instant, a tokenised-treasury redeem can be 40 days, a CEX
    # withdrawal is same-day to T+1. Critical for sizing and
    # liquidity planning.
    payout_timeline_label: str = ""    # short tag e.g. "same-day", "T+1", "instant on-chain"
    payout_details: str = ""           # one-sentence explanation


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
        backing_short="Staked USDe — NAV climbs above $1 as yield accrues; secondary market can trade above OR below NAV",
        expected_peg=1.0,  # NAV climbs; secondary market trades around it
        cadence="on-chain real-time", auditor="Chaos Labs",
        transparency_url="https://app.ethena.fi/dashboards/transparency",
        watchlist_signal="sUSDe/USDe ratio (the yield curve)",
        structural_one_liner=(
            "sUSDe is the staked wrapper over USDe — its NAV climbs "
            "above $1 as yield accrues. The secondary-market price can "
            "trade above OR below NAV depending on demand for instant "
            "exit vs the 7-day unstake cooldown. NOT a 1:1 peg target."
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
        backing_short="Tokenised note; NAV climbs above $1 as yield accrues, secondary market can trade either side",
        expected_peg=1.0,  # NAV climbs; secondary market trades around it
        cadence="NAV-rebased daily", auditor="Ankura",
        transparency_url="https://ondo.finance/usdy",
        watchlist_signal="NAV stale-feed (>24h)",
        structural_one_liner=(
            "USDY is a yield-bearing tokenised note — its NAV climbs "
            "above $1.00 by design as Treasury yield accrues. The "
            "secondary-market price can trade above OR below NAV; "
            "with a 40-day initial lockup, holders who want short-"
            "dated liquidity usually pay a discount in the secondary "
            "market. NOT a 1:1 peg target."
        ),
        cone_thresholds_bps=(50.0, 150.0),  # wide because the NAV anchor drifts
    ),
    "USDM": TokenContext(
        symbol="USDM", issuer="Mountain Protocol (Bermuda BMA)",
        backing_model="tokenised_treasury",
        backing_short="UST-backed; NAV climbs above $1 as yield accrues, secondary market often trades at a small discount",
        expected_peg=1.0,  # NAV climbs; secondary market trades around it
        cadence="daily NAV", auditor="Nephila Capital",
        transparency_url="https://mountainprotocol.com/",
        watchlist_signal="NAV-feed staleness",
        structural_one_liner=(
            "USDM is Bermuda-regulated and yield-bearing — its NAV "
            "climbs above $1 as Treasury yield accrues. The Curve "
            "USDM/USDC pool typically trades at a small DISCOUNT to "
            "NAV: investors who want instant cash exit accept a "
            "liquidity premium rather than wait for the issuer's "
            "redemption queue. Treat as a tokenised MMF, not a peg."
        ),
        cone_thresholds_bps=(40.0, 120.0),
    ),
}


# v3.2 overlay — extends entries above with three new fields without
# rewriting every constructor. Empty strings / False / "CEX" use the
# dataclass defaults.
_OVERLAY: dict[str, dict] = {
    "USDC": {
        "venue_type": "MIXED",
        "pl_lens": (
            "Holders earn nothing directly; the issuer captures "
            "Treasury yield. Loss happens only if reserves fail an "
            "attestation or Circle is unable to honour 1:1 redemption."
        ),
        "payout_timeline_label": "same-day",
        "payout_details": (
            "Circle Mint redeems 1:1 to USD same-day for qualified "
            "accounts (~10-30min via CEX off-ramps). On-chain swap "
            "via Curve 3pool is instant at <5bp slippage."
        ),
    },
    "USDT": {
        "venue_type": "MIXED",
        "pl_lens": (
            "Holders earn nothing directly; Tether keeps reserve "
            "yield. Loss tail is reserve-mix opacity (commercial "
            "paper, secured loans). Cone widens during Asia-session "
            "stress windows."
        ),
        "payout_timeline_label": "T+0 to T+2",
        "payout_details": (
            "Tether direct-redeem requires KYC + $100k minimum; "
            "settlement T+0 to T+2 depending on banking rail. "
            "Retail exit via CEX off-ramps (Binance / Bitfinex / "
            "Bybit) is same-day."
        ),
    },
    "DAI": {
        "venue_type": "DEX",
        "pl_lens": (
            "Holders earn the Dai Savings Rate via sDAI; raw DAI is "
            "yield-passive. Loss happens if the PSM USDC anchor "
            "depegs or PSM caps fill."
        ),
        "payout_timeline_label": "instant on-chain",
        "payout_details": (
            "MakerDAO PSM (USDC/DAI) swaps 1:1 instantly at zero fee "
            "until the cap fills. Conversion to USD then depends on "
            "USDC's off-ramp (same-day via Circle)."
        ),
    },
    "PYUSD": {
        "pl_lens": (
            "Holders earn nothing directly; Paxos captures Treasury "
            "yield. Loss is concentrated in PayPal payment-rail "
            "availability and NYDFS posture."
        ),
        "payout_timeline_label": "same-day",
        "payout_details": (
            "Paxos direct-redeem same-day for qualified accounts. "
            "PayPal users can convert to USD inside the app (T+0)."
        ),
    },
    "USDP": {
        "pl_lens": (
            "Holders earn nothing; Paxos captures yield. Float has "
            "been shrinking — secondary-market liquidity is thinner "
            "than peers, widening exit slippage."
        ),
        "payout_timeline_label": "same-day",
        "payout_details": (
            "Paxos direct-redeem same-day for qualified accounts. "
            "Thinner secondary liquidity means wider exit spread "
            "for size; T+0 settlement once the swap clears."
        ),
    },
    "TUSD": {
        "pl_lens": (
            "Holders earn nothing; issuer captures yield. Wider "
            "structural cone reflects 2023-24 transparency events; "
            "treat any sustained depeg above 15bp as a redemption "
            "stress signal."
        ),
        "payout_timeline_label": "T+1 to T+2",
        "payout_details": (
            "Techteryx direct-redeem subject to bank settlement (T+1 "
            "to T+2). Real-time PoR feed is informational, not "
            "operational — it does not accelerate USD payout."
        ),
    },
    "FDUSD": {
        "venue_type": "CEX",
        "pl_lens": (
            "Holders earn nothing; First Digital captures yield. "
            "Single-venue concentration on Binance means any HK "
            "regulatory event hits liquidity faster than diversified "
            "peers."
        ),
        "payout_timeline_label": "T+1",
        "payout_details": (
            "First Digital Trust direct-redeem T+1 typical (HK "
            "banking hours). Retail exit via Binance USDT pair is "
            "fastest in practice; off-ramp speed depends on the "
            "destination."
        ),
    },
    "GUSD": {
        "pl_lens": (
            "Holders earn nothing; Gemini captures yield. Small "
            "float — useful for low-slippage settlement but not "
            "deep liquidity at scale."
        ),
        "payout_timeline_label": "same-day",
        "payout_details": (
            "Gemini exchange redeems 1:1 same-day for verified "
            "users. Float is small ($300M-ish) so deep-size exits "
            "may need to be staged."
        ),
    },
    "USDe": {
        "venue_type": "DEX",
        "pl_lens": (
            "Raw USDe is yield-passive; the yield lives in sUSDe. "
            "Loss tail is perp-funding inversion: when basis turns "
            "negative the delta-neutral position bleeds and the peg "
            "is structurally exposed."
        ),
        "payout_timeline_label": "instant on-chain",
        "payout_details": (
            "Ethena mint/redeem is instant on-chain via the basis-"
            "trade harvest mechanism (no cooldown for raw USDe). "
            "USD conversion then via Curve / Uniswap (instant) → "
            "USDC → Circle off-ramp."
        ),
    },
    "sUSDe": {
        "yield_bearing": True,
        "venue_type": "DEX",
        "pl_lens": (
            "Staked wrapper that accrues USDe yield by drifting "
            "above $1.00. Returns reflect Ethena's delta-neutral "
            "harvest; loss is unwind risk if funding inverts for "
            "an extended window."
        ),
        "payout_timeline_label": "7-day cooldown",
        "payout_details": (
            "Unstaking sUSDe → USDe is gated by a 7-day cooldown "
            "(protocol-enforced). Then USDe redeems instantly. "
            "Secondary-market exit via Curve sUSDe/USDe pool is "
            "available but at a discount to NAV."
        ),
    },
    "FRAX": {
        "venue_type": "DEX",
        "pl_lens": (
            "Yield-passive at the FRAX level (sFRAX is the wrapper). "
            "Loss tail is collateral-ratio decline below 100% — the "
            "v3 design has shrunk this surface but it is not zero."
        ),
        "payout_timeline_label": "instant on-chain",
        "payout_details": (
            "v3 collateral redemption via Frax AMO is instant on-"
            "chain. Practical exit: swap to USDC via Curve FRAX/"
            "USDC pool, then Circle off-ramp same-day."
        ),
    },
    "GHO": {
        "venue_type": "DEX",
        "pl_lens": (
            "Yield-passive; borrowers pay variable rate to Aave "
            "treasury. Loss tail is facilitator-cap saturation and "
            "GHO trading persistently below peg as borrowers monetise "
            "the discount."
        ),
        "payout_timeline_label": "AMM exit only",
        "payout_details": (
            "No direct holder-redeem mechanism — exit via Balancer "
            "GHO/USDC pool or Aave repay (for borrowers). Liquidity "
            "thinner than peers; size-impact matters."
        ),
    },
    "crvUSD": {
        "venue_type": "DEX",
        "pl_lens": (
            "Yield-passive; LLAMMA soft-liquidations are the peg "
            "defence. Loss tail is band integrity failure on the "
            "largest collateral market (wstETH today)."
        ),
        "payout_timeline_label": "AMM exit only",
        "payout_details": (
            "No direct redemption — exit via Curve crvUSD/USDC pool "
            "or LLAMMA repay (for borrowers). LLAMMA bands provide "
            "soft liquidation; not the same as a 1:1 redeem."
        ),
    },
    "LUSD": {
        "venue_type": "DEX",
        "pl_lens": (
            "Yield-passive; Stability Pool depositors capture "
            "liquidation gains. LUSD frequently trades at a premium "
            "(redemption fee pricing) — premium is normal, not a "
            "depeg."
        ),
        "payout_timeline_label": "instant on-chain (premium)",
        "payout_details": (
            "Liquity protocol redeems LUSD for ETH at the minimum "
            "collateralisation ratio — instant on-chain but holders "
            "absorb a redemption fee (0.5%+). Curve LUSD/3pool is "
            "the practical exit path."
        ),
    },
    "USDD": {
        "venue_type": "DEX",
        "pl_lens": (
            "Yield-passive; TRON DAO subsidises via PSM. Loss tail "
            "is TRX/BTC reserve mark-to-market — when reserves "
            "underperform a sustained discount emerges."
        ),
        "payout_timeline_label": "PSM swap",
        "payout_details": (
            "TRON DAO PSM swaps USDD for USDT on JustLend (subject "
            "to caps). Tron-native, so off-ramping to USD requires "
            "a second hop (USDD→USDT→USD via CEX), typically same-"
            "day to T+1."
        ),
    },
    "USDS": {
        "venue_type": "DEX",
        "pl_lens": (
            "Yield-passive at the USDS level; sUSDS captures the Sky "
            "Savings Rate. Loss tail is PSM imbalance and any "
            "DAI-conversion stress."
        ),
        "payout_timeline_label": "instant on-chain",
        "payout_details": (
            "Sky PSM swaps USDS for DAI 1:1 instantly (no fee). DAI "
            "then redeems via MakerDAO PSM to USDC, then Circle off-"
            "ramp same-day."
        ),
    },
    "RLUSD": {
        "pl_lens": (
            "Holders earn nothing; Ripple captures Treasury yield. "
            "New launch (late 2024) means liquidity is still thin "
            "vs majors — exit slippage matters for size."
        ),
        "payout_timeline_label": "same-day",
        "payout_details": (
            "Standard Custody (NYDFS trust) redeems 1:1 same-day "
            "for qualified accounts via BNY Mellon. Thin secondary "
            "liquidity means wider exit spread; this will improve "
            "as the float grows."
        ),
    },
    "USDY": {
        "yield_bearing": True,
        "venue_type": "CEX",
        "pl_lens": (
            "NAV-rebased tokenised note; holders earn underlying "
            "Treasury yield directly via daily NAV adjustment. Loss "
            "tail is NAV-feed staleness (>24h) and Ondo's Bermuda-"
            "domiciled custody chain."
        ),
        "payout_timeline_label": "40-day lockup, then daily",
        "payout_details": (
            "Ondo USDY has a 40-day initial lockup from mint, then "
            "daily NAV-rebased redemption (T+1 banking settlement). "
            "Not a fast-exit asset — secondary market is the only "
            "way to get short-dated liquidity, and it usually trades "
            "at a discount to NAV."
        ),
    },
    "USDM": {
        "yield_bearing": True,
        "venue_type": "DEX",
        "pl_lens": (
            "Daily NAV rebase; holders earn Treasury yield directly. "
            "Bermuda BMA regulated. Loss tail is NAV-feed staleness "
            "and any Mountain Protocol custody-chain event."
        ),
        "payout_timeline_label": "T+0 mint/redeem (capped)",
        "payout_details": (
            "Mountain Protocol direct mint/redeem T+0 for qualified "
            "accounts, subject to daily caps. Secondary market "
            "(Curve USDM/USDC) trades at a discount to NAV — that's "
            "the liquidity-premium economics for yield-bearing tokens."
        ),
    },
}


def _apply_overlay(ctx: TokenContext) -> TokenContext:
    """Merge overlay fields into a base TokenContext if present.
    Returns a NEW frozen instance — never mutates the registry."""
    over = _OVERLAY.get(ctx.symbol)
    if not over:
        # Case-insensitive overlay lookup so 'CRVUSD' matches 'crvUSD'.
        sym_l = ctx.symbol.lower()
        for k, v in _OVERLAY.items():
            if k.lower() == sym_l:
                over = v
                break
    if not over:
        return ctx
    from dataclasses import replace
    return replace(ctx, **over)


def get_context(symbol: str) -> TokenContext | None:
    """Look up the cheat sheet for a symbol. The registry uses
    mixed-case keys (crvUSD, sUSDe) because that's how the issuers
    capitalise their tickers. Callers may send any case; we try
    exact, then case-insensitive across all keys."""
    if symbol in _REGISTRY:
        return _apply_overlay(_REGISTRY[symbol])
    sym_l = symbol.lower()
    for k, v in _REGISTRY.items():
        if k.lower() == sym_l:
            return _apply_overlay(v)
    return None


def all_known_symbols() -> list[str]:
    """All tokens with a structural context entry."""
    return list(_REGISTRY.keys())
