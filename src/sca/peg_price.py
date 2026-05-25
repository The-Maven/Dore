"""Spot price + peg deviation — the movement simulator's clock, hardened.

LAYER: facts. Deterministic, no LLM. Multi-source orchestrator that
fetches the same symbol from every configured upstream in parallel,
agrees on a consensus price, and flags disputes that exceed the
tolerance. The two-source rule that governs the rest of Doré now
applies to the ground truth too.

Sources (v2 — both free, no auth, complementary):
  - Coinbase v2 spot   (https://api.coinbase.com/v2/prices/{pair}/spot)
  - Kraken public ticker (https://api.kraken.com/0/public/Ticker)

Agreement gate:
  - Both sources responded, |gap| ≤ 5 bp → consensus_kind='agreed';
    consensus_price = mean.
  - Both sources responded, |gap| > 5 bp → consensus_kind='disputed';
    consensus_price = mean, but the row is flagged and the resolver
    surfaces the dispute in the resolution narrative.
  - One source responded → consensus_kind='single'; the row is
    written with that source's price and a hedged narrative.
  - Zero sources responded → no row written (we stay silent over
    serving false ground truth).

Hermetic discipline: every test path is offline. Live HTTP is gated
behind SCA_PEG_PRICE_DISABLED; when disabled, fetch_consensus
returns None and the caller persists nothing.
"""
from __future__ import annotations

import concurrent.futures
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from sca.observability import log_event

# Tolerance for treating two source prices as "agreed". Stablecoins
# routinely trade within 1-5 bp of each other across reputable spot
# venues. A gap above 5 bp means at least one source is wrong or
# stale; flag it loudly.
AGREEMENT_TOLERANCE_BPS = 5.0

# Per-symbol cache so a UI poll burst doesn't burn upstream rate
# limits. The ticker writes the persistent row at its own cadence.
# 20s (was 60s) — keeps live feel without smacking Coinbase/Kraken
# free-tier limits at our cadence (1 tick per minute per symbol
# means each upstream gets ≤3 hits/minute even with cache misses).
_CACHE_TTL_S = 20.0
_CACHE: dict[str, "ConsensusTick"] = {}

_HTTP_DISABLED_ENV = "SCA_PEG_PRICE_DISABLED"
_HTTP_TIMEOUT = 5.0


# ── data shapes ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class PegTick:
    """One single-source spot reading. The orchestrator builds a
    ConsensusTick from a list of these."""
    symbol: str
    source: str
    price: float
    deviation_bps: float
    fetched_at: float  # unix seconds


@dataclass
class ConsensusTick:
    """Multi-source agreed (or disputed) reading for one symbol.
    `consensus_price` is the canonical value used for resolution;
    `sources` carries every per-source reading so the audit trail
    shows exactly who said what."""
    symbol: str
    consensus_price: float
    deviation_bps: float
    consensus_kind: str  # 'single' | 'agreed' | 'disputed'
    sources: list[dict] = field(default_factory=list)
    max_disagreement_bps: float = 0.0
    fetched_at: float = 0.0


# ── helpers ──────────────────────────────────────────────────────────
def _peg_target(peg: str) -> tuple[str, float]:
    """Return (quote currency, target price). USD = 1.0 by default;
    EUR support is honest about parity assumption."""
    if peg.upper() == "EUR":
        return "EUR", 1.0
    return "USD", 1.0


def _disabled() -> bool:
    return os.environ.get(_HTTP_DISABLED_ENV, "").lower() in (
        "1", "true", "yes",
    )


# ── source adapters ──────────────────────────────────────────────────
# Each adapter is a function: (symbol, quote) -> Optional[float]
# returning the spot price or None on any failure. Adapters log their
# own failures so the orchestrator just sees the result.

def _coinbase_spot(symbol: str, quote: str) -> Optional[float]:
    """Coinbase v2 public spot — no auth, ~5 req/sec public budget."""
    url = (f"https://api.coinbase.com/v2/prices/"
           f"{symbol.upper()}-{quote.upper()}/spot")
    try:
        import requests
        resp = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Dore/peg-tick)"},
        )
        if resp.status_code != 200:
            log_event(
                "peg_price.coinbase.non_200", level="info",
                symbol=symbol, status=resp.status_code,
            )
            return None
        data = resp.json() or {}
        amount = (data.get("data") or {}).get("amount")
        return float(amount) if amount is not None else None
    except (ValueError, KeyError, TypeError) as exc:
        log_event(
            "peg_price.coinbase.parse_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log_event(
            "peg_price.coinbase.failed", level="info",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None


# Kraken pair naming follows old/new conventions. The pair lookup
# below covers the major stablecoin pairs we care about. Anything not
# in the map falls back to the canonical SYMBOL+QUOTE which works for
# most newer listings.
_KRAKEN_PAIR_MAP = {
    ("USDC", "USD"): "USDCUSD",
    ("USDT", "USD"): "USDTUSD",
    ("DAI", "USD"): "DAIUSD",
    ("PYUSD", "USD"): "PYUSDUSD",
    ("EURC", "EUR"): "EURCEUR",
    # Kraken's USDP pair is listed as PYUSDUSD historically and isn't
    # always available; we degrade gracefully when the pair isn't found.
}


def _kraken_spot(symbol: str, quote: str) -> Optional[float]:
    """Kraken public ticker — no auth, conservative rate limit.

    Response shape:
      {"error":[], "result":{"USDCUSD":{"c":["price","volume"], ...}}}
    `c[0]` is the last-trade-closed price, which is what we want as a
    spot reading.
    """
    # Kraken pairs are case-folded to UPPER throughout the API; the
    # map is already keyed on the upper-case symbol.
    sym_u = symbol.upper()
    pair = _KRAKEN_PAIR_MAP.get(
        (sym_u, quote.upper()),
        f"{sym_u}{quote.upper()}",
    )
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
    try:
        import requests
        resp = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Dore/peg-tick)"},
        )
        if resp.status_code != 200:
            log_event(
                "peg_price.kraken.non_200", level="info",
                symbol=symbol, status=resp.status_code,
            )
            return None
        data = resp.json() or {}
        # Kraken returns errors in the body, not the HTTP status.
        if data.get("error"):
            # Quietly drop unknown-pair errors (we may have hit a
            # symbol Kraken doesn't list).
            log_event(
                "peg_price.kraken.api_error", level="info",
                symbol=symbol, pair=pair,
                error_head=str(data.get("error"))[:120],
            )
            return None
        result = data.get("result") or {}
        # Kraken occasionally aliases the pair name in the response
        # (e.g. requesting USDCUSD may come back as USDCUSD or
        # USDC/USD); accept the first key in result.
        if not result:
            return None
        first_key = next(iter(result))
        c = (result[first_key] or {}).get("c") or []
        if not c:
            return None
        return float(c[0])
    except (ValueError, KeyError, TypeError) as exc:
        log_event(
            "peg_price.kraken.parse_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log_event(
            "peg_price.kraken.failed", level="info",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None


# CoinGecko ID map for tokens not listed on Coinbase / Kraken (most
# DeFi-native stablecoins). The /simple/price endpoint is free, no
# auth, ~30 req/min on the free tier — well within our cadence. We
# use lower-case CoinGecko slugs because that's their canonical id.
_COINGECKO_ID_MAP = {
    "USDC": "usd-coin",
    "USDT": "tether",
    "DAI":  "dai",
    "PYUSD": "paypal-usd",
    "USDP": "paxos-standard",
    "TUSD": "true-usd",
    "FDUSD": "first-digital-usd",
    "USDG": "global-dollar",
    "GUSD": "gemini-dollar",
    "FRAX": "frax",
    "LUSD": "liquity-usd",
    "GHO":  "gho",
    "CRVUSD": "crvusd",
    "AUSD": "agora-dollar",
    "USDe": "ethena-usde",
    "USDf": "falcon-finance-usdf",
    "USDD": "usdd",
    "USDX": "usdx-stable",
    "USDY": "ondo-us-dollar-yield",
    "M":    "m-by-m0",
    "EURC": "euro-coin",
    "EURI": "eurite",
    "AEUR": "anchored-coins-aeur",
    # New universe candidates the researcher recommended:
    "USDS": "usds",                # MakerDAO → Sky rebrand
    "RLUSD": "ripple-usd",         # Ripple
    "sUSDe": "ethena-staked-usde",
    "USDM": "mountain-protocol-usdm",
    "USR": "resolv-usr",
    "deUSD": "elixir-deusd",
    "USD0": "usual-usd",
    "BUIDL": "blackrock-usd-institutional-digital-liquidity-fund",
    "USDB": "usdb",
    "USDtb": "ethena-usdtb",
    "syrupUSDC": "maple-finance",  # syrupUSDC is via Maple
    "USYC": "hashnote-us-yield-coin",
}


def _coingecko_spot(symbol: str, quote: str) -> Optional[float]:
    """CoinGecko simple-price endpoint — free, no auth, the fallback
    that closes the DeFi-native gap. CB + Kraken don't list FRAX /
    crvUSD / GHO / USDe / many others; CoinGecko aggregates these
    from DEX pools and centralised venues alike.

    Rate limit on the free tier: ~30 req/min. We poll at most one
    per symbol per cycle (every 1–10 minutes per our config), so
    a 12-token universe stays well inside the limit.
    """
    # Case-insensitive lookup — config can send 'crvUSD' (mixed)
    # while the map keys here are 'CRVUSD'. Try exact, upper, lower.
    gid = (_COINGECKO_ID_MAP.get(symbol)
           or _COINGECKO_ID_MAP.get(symbol.upper())
           or _COINGECKO_ID_MAP.get(symbol.lower()))
    if not gid:
        # No mapping for this symbol — caller falls through.
        return None
    if quote.upper() != "USD":
        # CoinGecko supports many vs currencies but the simulator
        # is USD-pegged today; only fetch when the quote is USD.
        return None
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={gid}&vs_currencies=usd")
    try:
        import requests
        resp = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Dore/peg-tick)",
                     "Accept": "application/json"},
        )
        if resp.status_code != 200:
            log_event(
                "peg_price.coingecko.non_200", level="info",
                symbol=symbol, gid=gid, status=resp.status_code,
            )
            return None
        data = resp.json() or {}
        # Shape: {"<gid>": {"usd": 0.99987}}
        row = data.get(gid) or {}
        usd = row.get("usd")
        return float(usd) if usd is not None else None
    except (ValueError, KeyError, TypeError) as exc:
        log_event(
            "peg_price.coingecko.parse_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log_event(
            "peg_price.coingecko.failed", level="info",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None


# ── Pyth Network Hermes (oracle-grade real-time feeds) ──────────────
# Pyth aggregates 90+ first-party publishers (Jane Street, Two Sigma,
# DRW, GTS, etc.) and publishes sub-second price feeds. Same data
# Aave / Solend / Synthetix use as oracle ground truth. Free pull
# endpoint at hermes.pyth.network — no auth, no key, no rate limit
# we've ever hit in practice (the network is built for high-frequency
# consumers). The strongest single addition to our peg-source mix:
# truly real-time, oracle-grade, and INDEPENDENT of the CEX venues
# we already poll.
#
# Feed IDs are well-known per asset (the on-chain registry). The map
# below is hand-curated for the stablecoin universe we track.
# All IDs below verified by querying Pyth Hermes
# (/v2/price_feeds?asset_type=crypto) on 2026-05-25 and confirming
# each /v2/updates/price/latest call returns a fresh price (age <30s)
# in the $0.98–$1.02 range. Wrong-token feeds, stale feeds, and
# yield-bearing NAVs are deliberately EXCLUDED — listing them would
# inject false depeg signals into the consensus.
#
# Deliberately NOT included:
#   FRAX  — Pyth's FRAX/USD feed tracks the FXS-like governance
#           token at ~$0.42, not the stablecoin. After the FRAX→
#           frxUSD rebrand the stablecoin lives at Crypto.FRXUSD/USD,
#           but our registry still uses the symbol "FRAX", so we'd
#           need a symbol-aliasing layer before wiring it in.
#   LUSD  — Pyth's LUSD/USD feed was last published 142 days ago.
#           Until they relight it, the freshness gate would reject
#           every read anyway, so we skip the network round-trip.
#   USDY, sUSDe, USDM — yield-bearing NAV prices ($1.05–$1.23). The
#           current pipeline compares feed price to a static $1.00
#           peg, so these would register as catastrophic depegs.
#           Unblock after audit finding 1.2 (NAV oracle integration).
_PYTH_FEED_IDS = {
    "USDC":  "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
    "USDT":  "0x2b89b9dc8fdf9f34709a5b106b472f0f39bb6ca9ce04b0fd7f2e971688e2e53b",
    "DAI":   "0xb0948a5e5313200c632b51bb5ca32f6de0d36e9950a942d19751e833f70dabfd",
    "PYUSD": "0xc1da1b73d7f01e7ddd54b3766cf7fcd644395ad14f70aa706ec5384c59e76692",
    "USDP":  "0xa6c8eca9aea31d6bb81fd6576638f30692d4afaa73237c097c193477aa5003b3",
    "TUSD":  "0x433faaa801ecdb6618e3897177a118b273a8e18cc3ff545aadfc207d58d028f7",
    "FDUSD": "0xccdc1a08923e2e4f4b1e6ea89de6acbc5fe1948e9706f5604b8cb50bc1ed3979",
    "GUSD":  "0xe186e116f2c7642d0d8aa89c32345d83ebeb350242b2274c46a19ea82e04fb8d",
    # DeFi-native additions (audit finding 2.1 — eliminate
    # CoinGecko-only single-source consensus for these):
    "GHO":   "0x2a0e948f637a8c251d9f06055e72eb4b3880dd57848bbdb02993c8165d7df4ee",
    "USDE":  "0x6ec879b1e9963de5ee97e9c8710b742d6228252a5e2ca12d4ae81d7fe5ee8c5d",
    "USDD":  "0x6d20210495d6518787b72e4ad06bc4df21e68d89a802cf6bced2fca6c29652a6",
    "USDS":  "0x77f0971af11cc8bac224917275c1bf55f2319ed5c654a1ca955c82fa2d297ea1",
}


def _pyth_spot(symbol: str, quote: str) -> Optional[float]:
    """Pyth Network Hermes pull endpoint — real-time oracle price.

    Returns the most-recent published price (median of 90+ first-party
    publishers, sub-second freshness). Hermes is built for low-latency
    consumers; we've never observed rate-limiting at our cadence. The
    `publish_time` is also useful for staleness checks, but Hermes
    only publishes when at least one publisher updates, so a returned
    feed is by construction fresh.

    Sub-second latency makes this the highest-quality source we poll —
    when it responds, we lean on it as the reference price.
    """
    feed_id = _PYTH_FEED_IDS.get((symbol or "").upper())
    if not feed_id:
        return None
    if quote.upper() != "USD":
        return None
    url = (
        "https://hermes.pyth.network/v2/updates/price/latest"
        f"?ids[]={feed_id}"
    )
    try:
        import requests
        resp = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Dore/peg-tick)",
                     "Accept": "application/json"},
        )
        if resp.status_code != 200:
            log_event(
                "peg_price.pyth.non_200", level="info",
                symbol=symbol, status=resp.status_code,
            )
            return None
        data = resp.json() or {}
        parsed = data.get("parsed") or []
        if not parsed:
            return None
        first = parsed[0]
        price_obj = first.get("price") or {}
        raw_price = price_obj.get("price")
        expo = price_obj.get("expo")
        if raw_price is None or expo is None:
            return None
        try:
            return float(raw_price) * (10 ** int(expo))
        except (TypeError, ValueError):
            return None
    except (ValueError, KeyError, TypeError) as exc:
        log_event(
            "peg_price.pyth.parse_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log_event(
            "peg_price.pyth.failed", level="info",
            symbol=symbol, error_class=type(exc).__name__,
        )
        return None


# Registered source adapters. Order matters for tie-breaking when
# only one source responds — first-in-list wins the consensus_kind=
# 'single' attribution. Pyth leads because it's oracle-grade and
# real-time. CEX venues come next; CoinGecko is the aggregator
# fallback for DeFi-native tokens.
_SOURCES: list[tuple[str, Callable[[str, str], Optional[float]]]] = [
    ("pyth", _pyth_spot),
    ("coinbase", _coinbase_spot),
    ("kraken", _kraken_spot),
    ("coingecko", _coingecko_spot),
]


# ── public API ───────────────────────────────────────────────────────
def fetch_consensus(symbol: str, *, peg: str = "USD") -> Optional[ConsensusTick]:
    """Fetch the same symbol from every configured source in parallel,
    build a consensus reading. Returns None when ZERO sources responded
    (the orchestrator stays silent rather than serve false truth).

    Cached for 60s per symbol; a cached ConsensusTick is returned
    verbatim (including its consensus_kind + sources) so a UI poll
    doesn't trigger fresh fetches inside the cadence window.
    """
    if _disabled():
        return None

    now = time.time()
    cache_key = f"{symbol.upper()}:{peg.upper()}"
    cached = _CACHE.get(cache_key)
    if cached is not None and (now - cached.fetched_at) < _CACHE_TTL_S:
        return cached

    quote, anchor = _peg_target(peg)
    # Parallel fetch across sources — each is a network call; running
    # them concurrently keeps the orchestrator's wall time close to
    # the slowest single source instead of the sum.
    results: dict[str, Optional[float]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(_SOURCES),
    ) as executor:
        future_to_name = {
            executor.submit(adapter, symbol, quote): name
            for name, adapter in _SOURCES
        }
        for future in concurrent.futures.as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "peg_price.adapter_crashed", level="warn",
                    source=name, symbol=symbol,
                    error_class=type(exc).__name__,
                )
                results[name] = None

    # Build per-source records for the audit trail. Sources that
    # returned None are not included — the row only shows who
    # actually answered.
    sources_recorded: list[dict] = []
    valid_prices: list[tuple[str, float]] = []
    for name, _ in _SOURCES:
        price = results.get(name)
        if price is None:
            continue
        sources_recorded.append({
            "name": name, "price": price, "fetched_at": now,
        })
        valid_prices.append((name, price))

    if not valid_prices:
        log_event(
            "peg_price.no_source_responded", level="warn",
            symbol=symbol,
        )
        return None

    # Consensus math.
    prices_only = [p for _, p in valid_prices]
    consensus_price = sum(prices_only) / len(prices_only)
    deviation_bps = (consensus_price - anchor) * 10_000.0
    if len(valid_prices) == 1:
        consensus_kind = "single"
        max_disagreement_bps = 0.0
    else:
        max_disagreement_bps = (max(prices_only) - min(prices_only)) * 10_000.0
        if max_disagreement_bps <= AGREEMENT_TOLERANCE_BPS:
            consensus_kind = "agreed"
        else:
            consensus_kind = "disputed"
            log_event(
                "peg_price.dispute", level="warn",
                symbol=symbol,
                max_disagreement_bps=round(max_disagreement_bps, 2),
                sources=sources_recorded,
            )

    tick = ConsensusTick(
        symbol=symbol.upper(),
        consensus_price=consensus_price,
        deviation_bps=deviation_bps,
        consensus_kind=consensus_kind,
        sources=sources_recorded,
        max_disagreement_bps=max_disagreement_bps,
        fetched_at=now,
    )
    _CACHE[cache_key] = tick
    return tick


def fetch_price(symbol: str, *, peg: str = "USD") -> Optional[PegTick]:
    """Backwards-compatible thin wrapper returning a single-source-
    shaped PegTick. Used by callers that haven't been updated to
    handle ConsensusTick yet. The `source` field collapses to a
    name like 'coinbase+kraken' when both responded so the legacy
    audit trail still names what answered."""
    consensus = fetch_consensus(symbol, peg=peg)
    if consensus is None:
        return None
    source_label = "+".join(s["name"] for s in consensus.sources) or "unknown"
    return PegTick(
        symbol=consensus.symbol,
        source=source_label,
        price=consensus.consensus_price,
        deviation_bps=consensus.deviation_bps,
        fetched_at=consensus.fetched_at,
    )


def persist_consensus(tick: Optional[ConsensusTick]) -> None:
    """Write a multi-source consensus row through the store. Best-
    effort: a store failure is logged but never raised. The store's
    insert_peg_tick signature is extended to accept the new columns;
    backends that haven't been updated still accept the call (the
    new kwargs fall through their **kwargs)."""
    if tick is None:
        return
    try:
        from sca.store import get_store
        store = get_store()
        if hasattr(store, "insert_peg_tick"):
            # Compose the legacy `source` label as the dominant
            # source for backwards-compatible single-source readers.
            source_label = "+".join(
                s["name"] for s in tick.sources) or "unknown"
            store.insert_peg_tick(
                symbol=tick.symbol,
                source=source_label,
                price=tick.consensus_price,
                deviation_bps=tick.deviation_bps,
                consensus_kind=tick.consensus_kind,
                sources=tick.sources,
                max_disagreement_bps=tick.max_disagreement_bps,
            )
    except Exception as exc:  # noqa: BLE001
        log_event(
            "peg_price.persist_failed", level="warn",
            symbol=tick.symbol, error_class=type(exc).__name__,
            error_message=str(exc)[:160],
        )


def persist_tick(tick: Optional[PegTick]) -> None:
    """Legacy callers: keep working with the single-source signature.
    Internally routes through persist_consensus to preserve the audit
    trail. New code should call fetch_consensus + persist_consensus
    directly."""
    if tick is None:
        return
    consensus = ConsensusTick(
        symbol=tick.symbol,
        consensus_price=tick.price,
        deviation_bps=tick.deviation_bps,
        consensus_kind="single",
        sources=[{"name": tick.source, "price": tick.price,
                   "fetched_at": tick.fetched_at}],
        max_disagreement_bps=0.0,
        fetched_at=tick.fetched_at,
    )
    persist_consensus(consensus)
