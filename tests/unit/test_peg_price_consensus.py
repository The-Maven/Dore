"""Multi-source peg consensus tests (audit #7).

Pins the agreement-gate contract: two sources within tolerance →
agreed; two sources outside tolerance → disputed (with audit
log); one source → single (with hedged narrative downstream);
zero sources → None (stay silent).
"""
from __future__ import annotations

import pytest

from sca import peg_price


class _Resp:
    def __init__(self, payload, *, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def _coinbase_payload(price: float) -> dict:
    return {"data": {"base": "USDC", "currency": "USD",
                      "amount": str(price)}}


def _kraken_payload(price: float, pair: str = "USDCUSD") -> dict:
    return {"error": [],
            "result": {pair: {"c": [str(price), "0"], "a": [], "b": []}}}


def test_consensus_agreed_when_sources_within_tolerance(monkeypatch):
    """Coinbase says 1.0001, Kraken says 1.0002. Spread = 1bp < 5bp
    tolerance → consensus_kind='agreed', consensus_price = mean."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()

    def fake_get(url, *a, **kw):
        if "coinbase" in url:
            return _Resp(_coinbase_payload(1.0001))
        if "kraken" in url:
            return _Resp(_kraken_payload(1.0002))
        raise AssertionError(f"unexpected url: {url}")

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    tick = peg_price.fetch_consensus("USDC")
    assert tick is not None
    assert tick.consensus_kind == "agreed"
    assert abs(tick.consensus_price - 1.00015) < 1e-6
    assert tick.max_disagreement_bps == pytest.approx(1.0, abs=0.01)
    assert {s["name"] for s in tick.sources} == {"coinbase", "kraken"}


def test_consensus_disputed_when_sources_disagree(monkeypatch):
    """Coinbase says 1.0001, Kraken says 1.0050. Spread = 49bp > 5bp
    → consensus_kind='disputed' (audit-flagged but the row is still
    written so the calibration archive shows reality)."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()

    def fake_get(url, *a, **kw):
        if "coinbase" in url:
            return _Resp(_coinbase_payload(1.0001))
        if "kraken" in url:
            return _Resp(_kraken_payload(1.0050))
        raise AssertionError(f"unexpected url: {url}")

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    tick = peg_price.fetch_consensus("USDC")
    assert tick is not None
    assert tick.consensus_kind == "disputed"
    assert tick.max_disagreement_bps == pytest.approx(49.0, abs=0.1)


def test_consensus_single_when_one_source_fails(monkeypatch):
    """Coinbase OK, Kraken returns API error → consensus_kind='single'
    citing coinbase. The single-source path is honest and used; the
    resolver surfaces it in the narrative."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()

    def fake_get(url, *a, **kw):
        if "coinbase" in url:
            return _Resp(_coinbase_payload(0.9998))
        if "kraken" in url:
            return _Resp({"error": ["EQuery:Unknown asset pair"]})
        raise AssertionError(f"unexpected url: {url}")

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    tick = peg_price.fetch_consensus("USDC")
    assert tick is not None
    assert tick.consensus_kind == "single"
    assert len(tick.sources) == 1
    assert tick.sources[0]["name"] == "coinbase"
    assert tick.max_disagreement_bps == 0.0


def test_consensus_returns_none_when_all_sources_fail(monkeypatch):
    """Both sources fail → return None. The orchestrator stays silent
    rather than fabricating ground truth."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()

    def fake_get(url, *a, **kw):
        return _Resp({"error": "boom"}, status=500)

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    tick = peg_price.fetch_consensus("USDC")
    assert tick is None


def test_consensus_disabled_returns_none(monkeypatch):
    """Hermetic mode: SCA_PEG_PRICE_DISABLED=1 short-circuits before
    any network call."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "1")
    peg_price._CACHE.clear()

    def fail_get(*a, **kw):
        raise AssertionError("network called despite disabled flag")

    import requests
    monkeypatch.setattr(requests, "get", fail_get)

    assert peg_price.fetch_consensus("USDC") is None


def test_consensus_cached_within_ttl(monkeypatch):
    """A second call within 60s must hit the cache and not re-fetch."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()
    call_count = {"n": 0}

    def fake_get(url, *a, **kw):
        call_count["n"] += 1
        if "coinbase" in url:
            return _Resp(_coinbase_payload(1.0))
        return _Resp(_kraken_payload(1.0))

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    peg_price.fetch_consensus("USDC")
    n_after_first = call_count["n"]
    peg_price.fetch_consensus("USDC")
    assert call_count["n"] == n_after_first  # second hit cached


def test_coingecko_fallback_picks_up_defi_native(monkeypatch):
    """CoinGecko fills the gap when CB + Kraken don't list a
    DeFi-native token (FRAX, GHO, crvUSD, etc). Three sources
    should all report; consensus_kind should be 'agreed' when
    within tolerance."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()

    def fake_get(url, *a, **kw):
        if "coinbase" in url:
            return _Resp({"error": "not listed"}, status=404)
        if "kraken" in url:
            return _Resp({"error": ["EQuery:Unknown asset pair"]})
        if "coingecko" in url:
            # Return a price for any CG id that's in the request URL
            for sym, gid in peg_price._COINGECKO_ID_MAP.items():
                if gid in url:
                    return _Resp({gid: {"usd": 0.9998}})
            return _Resp({})
        raise AssertionError(f"unexpected url: {url}")

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    tick = peg_price.fetch_consensus("FRAX")
    assert tick is not None
    assert tick.consensus_kind == "single"
    assert len(tick.sources) == 1
    assert tick.sources[0]["name"] == "coingecko"
    assert abs(tick.consensus_price - 0.9998) < 1e-6


def test_coingecko_case_insensitive_lookup(monkeypatch):
    """Config can send 'crvUSD' (mixed case) while the CG ID map
    keys on 'CRVUSD'. The lookup must try upper / lower fallbacks."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()

    def fake_get(url, *a, **kw):
        if "coingecko" in url and "crvusd" in url:
            return _Resp({"crvusd": {"usd": 1.0001}})
        # CB + Kraken don't list crvUSD
        return _Resp({"error": "no"}, status=404)

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    # Lower-case symbol — caller might send 'crvUSD'
    tick = peg_price.fetch_consensus("crvUSD")
    assert tick is not None, "crvUSD lookup should hit CG case-insensitively"
    assert tick.consensus_kind == "single"


def test_fetch_price_returns_combined_source_label(monkeypatch):
    """The backwards-compatible fetch_price wrapper labels the
    source as 'coinbase+kraken' when both responded so legacy
    audit-trail readers still see who answered."""
    monkeypatch.setenv(peg_price._HTTP_DISABLED_ENV, "")
    peg_price._CACHE.clear()

    def fake_get(url, *a, **kw):
        if "coinbase" in url:
            return _Resp(_coinbase_payload(1.0))
        return _Resp(_kraken_payload(1.0))

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    tick = peg_price.fetch_price("USDC")
    assert tick is not None
    assert tick.source == "coinbase+kraken"
