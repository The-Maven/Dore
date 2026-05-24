"""Spot price + peg deviation source — the movement simulator's clock.

LAYER: facts. Deterministic, no LLM. Single chokepoint for fetching a
spot price for a stablecoin against its quoted peg (USD, EUR). The
price is the resolution rail for peg-deviation predictions, and the
deviation series is one of the leading signals the engine attributes
calls to.

Source discipline: Coinbase's public v2 spot endpoint is our v1 because
it is (a) free, (b) needs no auth, (c) is the most-cited reference
price among the cited industry research (Kaiko / Coin Metrics tend to
use it as a quote anchor). Adding Kraken / Binance / a DEX-derived
quote is a future migration — peg_ticks.source captures which adapter
wrote the row so the calibration story can compare across.

Hermetic discipline: every test path is offline. Live HTTP is gated
behind `_HTTP_DISABLED` (set in conftest); when disabled, `fetch_price`
returns None and the caller persists nothing. The persistence layer
treats None as "no tick this cycle" — not "the price is zero".
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from sca.observability import log_event

# 60s in-memory cache so a flurry of /api/supply polls doesn't burn
# Coinbase rate limit. The ticker thread writes the persistent row at
# its own cadence (default once per minute per symbol).
_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (price, fetched_at)
_CACHE_TTL_S = 60.0

# Tests / hermetic CI set this to disable network. Set by conftest.
_HTTP_DISABLED_ENV = "SCA_PEG_PRICE_DISABLED"

# Coinbase v2 spot — no auth, ~5 req/sec public budget, JSON shape:
#   {"data": {"base": "USDC", "currency": "USD", "amount": "0.99987"}}
_COINBASE_URL = "https://api.coinbase.com/v2/prices/{base}-{quote}/spot"
_HTTP_TIMEOUT = 5.0


@dataclass(frozen=True)
class PegTick:
    """One sampled spot price. price is the actual quote; deviation_bps
    is the signed gap from peg in basis points (price 1.0001 vs USD
    peg = +1.0 bps, price 0.9985 = -15.0 bps). The bps unit is the
    natural quote for stablecoin micro-deviations — sub-100bps is
    routine, >100bps is news."""
    symbol: str
    source: str
    price: float
    deviation_bps: float
    fetched_at: float  # unix seconds


def _peg_target(peg: str) -> tuple[str, float]:
    """Return (quote currency, target price). Defaults to USD = 1.0.
    Honest non-USD support is a future move; for now we name the gap
    and assume parity. The deviation calculation uses this anchor."""
    if peg.upper() == "EUR":
        return "EUR", 1.0
    return "USD", 1.0


def _disabled() -> bool:
    return os.environ.get(_HTTP_DISABLED_ENV, "").lower() in ("1", "true", "yes")


def fetch_price(symbol: str, *, peg: str = "USD") -> Optional[PegTick]:
    """Fetch a single spot price tick for `symbol`. None on any failure
    — caller persists nothing on None and the row simply doesn't exist.

    Cached for 60s so repeated calls within a UI refresh window don't
    re-hit the upstream. The cache is keyed by symbol so two symbols
    update independently.
    """
    if _disabled():
        return None

    now = time.time()
    cache_key = f"{symbol.upper()}:{peg.upper()}"
    cached = _CACHE.get(cache_key)
    if cached is not None and (now - cached[1]) < _CACHE_TTL_S:
        price = cached[0]
        _, anchor = _peg_target(peg)
        return PegTick(
            symbol=symbol.upper(),
            source="coinbase_cached",
            price=price,
            deviation_bps=(price - anchor) * 10_000.0,
            fetched_at=cached[1],
        )

    quote, anchor = _peg_target(peg)
    url = _COINBASE_URL.format(base=symbol.upper(), quote=quote.upper())
    try:
        import requests
        resp = requests.get(
            url, timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Dore/peg-tick)"},
        )
        if resp.status_code != 200:
            log_event(
                "peg_price.fetch.non_200", level="info",
                symbol=symbol, status=resp.status_code,
            )
            return None
        data = resp.json() or {}
        amount = (data.get("data") or {}).get("amount")
        if amount is None:
            return None
        price = float(amount)
    except (ImportError, ValueError, KeyError, TypeError) as exc:
        log_event(
            "peg_price.fetch.parse_failed", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
            error_message=str(exc)[:160],
        )
        return None
    except Exception as exc:  # noqa: BLE001 - network failures are best-effort
        log_event(
            "peg_price.fetch.failed", level="info",
            symbol=symbol, error_class=type(exc).__name__,
            error_message=str(exc)[:160],
        )
        return None

    _CACHE[cache_key] = (price, now)
    deviation_bps = (price - anchor) * 10_000.0
    return PegTick(
        symbol=symbol.upper(),
        source="coinbase",
        price=price,
        deviation_bps=deviation_bps,
        fetched_at=now,
    )


def persist_tick(tick: PegTick) -> None:
    """Write a peg_tick row through the store. Best-effort: a store
    failure is logged but never raised, so the ticker thread keeps
    running. Idempotent at the table level — the schema doesn't
    deduplicate, so the caller decides cadence (default: once per
    minute per symbol in the ticker thread)."""
    if tick is None:
        return
    try:
        from sca.store import get_store
        store = get_store()
        if hasattr(store, "insert_peg_tick"):
            store.insert_peg_tick(
                symbol=tick.symbol,
                source=tick.source,
                price=tick.price,
                deviation_bps=tick.deviation_bps,
            )
    except Exception as exc:  # noqa: BLE001 - persistence never breaks the run
        log_event(
            "peg_price.persist_failed", level="warn",
            symbol=tick.symbol, error_class=type(exc).__name__,
            error_message=str(exc)[:160],
        )
