"""Token context + AI Commentary tests.

Pins the cheat-sheet → LLM → card pipeline. Hermetic — the LLM is
stubbed; we test that:
  * every registered token returns a coherent context
  * mixed-case lookups work (crvUSD, sUSDe)
  * the deterministic fallback path produces honest n/a when LLM
    is unavailable
  * the LLM happy-path returns a card with citations
  * voice rules strip em-dashes + weasel words
"""
from __future__ import annotations

from sca.movement import commentary, token_context


class _FakeLLM:
    """Deterministic LLM stub for testing the commentary pipeline."""
    def __init__(self, response: str = ""):
        self._response = response

    def complete(self, *, system, prompt, max_tokens=300):
        return self._response

    def extract_json(self, *, system, prompt, schema, max_tokens=2048):
        raise NotImplementedError


# ── token context registry ───────────────────────────────────────────
def test_every_major_token_has_context():
    """USDC / USDT / DAI / PYUSD / FRAX / USDe / USDS all need a
    cheat sheet because the simulator focuses these tokens most."""
    for sym in ("USDC", "USDT", "DAI", "PYUSD", "FRAX", "USDe",
                "USDS", "GHO", "crvUSD"):
        ctx = token_context.get_context(sym)
        assert ctx is not None, f"missing token context for {sym}"
        assert ctx.symbol == sym or ctx.symbol.upper() == sym.upper()
        assert ctx.issuer  # non-empty
        assert ctx.backing_model  # non-empty
        assert ctx.transparency_url.startswith("http")
        assert ctx.cone_thresholds_bps[0] > 0
        assert ctx.cone_thresholds_bps[1] > ctx.cone_thresholds_bps[0]


def test_mixed_case_lookup_handles_crvusd():
    """The 'crvUSD' / 'sUSDe' tokens are case-sensitive in some
    upstream registries. The lookup should accept the raw symbol
    and an upper-case variant."""
    assert token_context.get_context("crvUSD") is not None
    assert token_context.get_context("CRVUSD") is not None
    assert token_context.get_context("sUSDe") is not None
    assert token_context.get_context("SUSDE") is not None


def test_unknown_token_returns_none():
    """An unregistered ticker must NOT silently return a fake context."""
    assert token_context.get_context("FAKECOIN") is None


# ── deterministic fallback ──────────────────────────────────────────
def test_deterministic_fallback_when_llm_unavailable():
    """When no LLM is configured (or a real call fails), the
    commentary card must STILL render — using the cheat-sheet
    alone, with honest hedging. The fallback is the safety net."""
    ctx = token_context.get_context("USDC")
    out = commentary._deterministic_fallback(
        ctx, {"current_bps": -1.5, "cone_p80_bps": 2.0})
    assert out is not None
    assert out.symbol == "USDC"
    assert "Circle" in out.body or "money-market" in out.body
    # Falls back to the structural one-liner
    assert ctx.structural_one_liner in out.body \
        or out.body.startswith(ctx.structural_one_liner[:40])
    # Citations point to transparency URL
    assert out.citations
    assert out.citations[0]["url"].startswith("http")


def test_llm_happy_path_produces_card(tmp_path, monkeypatch):
    """A well-formed LLM JSON response → headline + body + citations."""
    monkeypatch.setattr(commentary, "_CACHE_PATH",
                        tmp_path / "comm.json")
    raw = (
        '{"headline": "USDT · liquid microstructure",'
        ' "body": "USDT runs a quarterly-attested mix; today\'s cone is'
        ' inside its 30-day band. Watch for negative Curve basis [1]."}'
    )
    out = commentary.get_commentary(
        "USDT", {"current_bps": -3.0, "cone_p80_bps": 4.0,
                  "d1h": -0.5},
        llm_client=_FakeLLM(raw),
    )
    assert out is not None
    assert out.symbol == "USDT"
    assert "liquid microstructure" in out.headline.lower() \
        or "USDT" in out.headline
    assert "[1]" in out.body
    assert out.citations
    assert "tether" in out.citations[0]["url"].lower()


def test_llm_parse_failure_falls_back_gracefully(tmp_path, monkeypatch):
    """An LLM that returns garbage → deterministic fallback, not
    crash. Honest 'n/a' over a fake-looking card."""
    monkeypatch.setattr(commentary, "_CACHE_PATH",
                        tmp_path / "comm.json")
    out = commentary.get_commentary(
        "DAI", {"current_bps": 1.0, "cone_p80_bps": 3.0},
        llm_client=_FakeLLM("not json at all"),
    )
    assert out is not None
    assert out.symbol == "DAI"
    # Fell back to deterministic, so the body includes the cheat
    # sheet's one-liner.
    assert "PSM" in out.body or "Sky" in out.body


def test_voice_rules_strip_weasels_and_em_dashes(tmp_path, monkeypatch):
    """The post-clean pass strips banned modal verbs + em-dashes,
    same discipline as judge.py."""
    monkeypatch.setattr(commentary, "_CACHE_PATH",
                        tmp_path / "comm.json")
    raw = (
        '{"headline": "USDC — likely stable",'
        ' "body": "USDC may stay tight — could widen perhaps if [1] flips."}'
    )
    out = commentary.get_commentary(
        "USDC", {"current_bps": -1.0, "cone_p80_bps": 2.0},
        llm_client=_FakeLLM(raw),
    )
    assert "—" not in out.headline
    assert "—" not in out.body
    low = out.body.lower()
    assert "may" not in low
    assert "could" not in low
    assert "perhaps" not in low


def test_yield_bearing_tokens_carry_the_tag():
    """USDY / sUSDe / USDM drift above $1 by design as yield accrues.
    The token context registry must mark them so the UI does not read
    them as deeply-depegged stablecoins."""
    for sym in ("USDY", "sUSDe", "USDM"):
        ctx = token_context.get_context(sym)
        assert ctx is not None, f"missing context for {sym}"
        assert ctx.yield_bearing is True, (
            f"{sym} is yield-bearing by design — must be flagged")


def test_venue_type_classification():
    """Venue type drives the source-tooltip copy: CEX / DEX / MIXED.
    USDC + USDT are MIXED (both venues), DAI + crvUSD are DEX-native,
    GUSD + RLUSD are primarily CEX."""
    assert token_context.get_context("USDC").venue_type == "MIXED"
    assert token_context.get_context("USDT").venue_type == "MIXED"
    assert token_context.get_context("DAI").venue_type == "DEX"
    assert token_context.get_context("crvUSD").venue_type == "DEX"
    assert token_context.get_context("USDe").venue_type == "DEX"
    assert token_context.get_context("GUSD").venue_type == "CEX"
    assert token_context.get_context("RLUSD").venue_type == "CEX"


def test_payout_timeline_set_on_every_token():
    """Every registered token must carry a payout_timeline_label +
    payout_details so the UI's PAYOUT chip never goes blank. Time-
    to-cash is a real trading consideration; we don't ship "unknown"."""
    for sym in token_context.all_known_symbols():
        ctx = token_context.get_context(sym)
        assert ctx is not None
        assert ctx.payout_timeline_label, (
            f"{sym} missing payout_timeline_label — every registered "
            "token needs a redemption-timeline annotation")
        assert ctx.payout_details, (
            f"{sym} missing payout_details — short tag is not enough; "
            "we owe the user a sentence on the exact mechanism")


def test_payout_timeline_reflects_real_mechanisms():
    """Sanity-check the payout taxonomy is consistent with each
    token's actual redemption mechanism. A few spot-checks against
    publicly-documented facts:
      - USDC, PYUSD, GUSD: same-day via issuer
      - USDT: T+0 to T+2 depending on banking
      - DAI: instant on-chain via PSM
      - sUSDe: 7-day cooldown (Ethena protocol)
      - USDY: 40-day lockup, then daily NAV redemption
    """
    assert token_context.get_context("USDC").payout_timeline_label == "same-day"
    assert "T+" in token_context.get_context("USDT").payout_timeline_label
    assert "instant" in token_context.get_context("DAI").payout_timeline_label
    assert "cooldown" in token_context.get_context("sUSDe").payout_timeline_label
    assert "lockup" in token_context.get_context("USDY").payout_timeline_label


def test_pl_lens_present_on_every_token():
    """Every token in the registry must have a P&L lens — short,
    hedged framing on what holders gain or lose. This is what powers
    the path-to-profitability/loss commentary block."""
    for sym in token_context.all_known_symbols():
        ctx = token_context.get_context(sym)
        assert ctx is not None
        assert ctx.pl_lens, (
            f"{sym} missing pl_lens — every registered token needs "
            "a path-to-profitability framing for the commentary card")
        # Hedged language discipline — no absolute promises
        low = ctx.pl_lens.lower()
        for banned in ("guaranteed", "will earn", "always", "risk-free"):
            assert banned not in low, (
                f"{sym}.pl_lens contains '{banned}' — must hedge")


def test_deterministic_fallback_includes_pl_lens():
    """The fallback card must surface the P&L lens so investors see
    the risk posture even when the LLM is unavailable."""
    ctx = token_context.get_context("USDC")
    out = commentary._deterministic_fallback(
        ctx, {"current_bps": -1.5, "cone_p80_bps": 2.0})
    assert "P&L lens" in out.body
    # USDC pl_lens mentions reserves
    assert "reserves" in out.body.lower() or "redemption" in out.body.lower()


def test_yield_bearing_fallback_names_the_drift():
    """For yield-bearing tokens, the deterministic fallback must
    explicitly tell the reader that the bp figure is design drift,
    not a depeg signal."""
    ctx = token_context.get_context("USDY")
    out = commentary._deterministic_fallback(
        ctx, {"current_bps": 1300.0, "cone_p80_bps": 80.0})
    low = out.body.lower()
    assert "yield-bearing" in low
    assert "drift" in low
    assert "peg-deviation lens does not apply" in low \
        or "design" in low


def test_wait_for_llm_false_serves_cache_instantly(tmp_path, monkeypatch):
    """The UX-fast path must serve a cached entry from ANY regime
    bucket if one exists — no LLM call, no wait. This is the fix for
    the slow "loading…" commentary card."""
    monkeypatch.setattr(commentary, "_CACHE_PATH",
                        tmp_path / "comm.json")
    call_count = {"n": 0}

    class _CountingLLM(_FakeLLM):
        def complete(self, *, system, prompt, max_tokens=300):
            call_count["n"] += 1
            return '{"headline": "x", "body": "y see [1]."}'

    # Prime the cache with a 'tight' regime entry.
    commentary.get_commentary(
        "USDC", {"current_bps": -1.0, "cone_p80_bps": 2.0},
        llm_client=_CountingLLM())
    primed_calls = call_count["n"]
    assert primed_calls == 1

    # Request the SAME symbol in a DIFFERENT regime (alert: very wide cone).
    # With wait_for_llm=False the cached tight-regime entry should serve
    # instantly without burning another LLM call.
    out = commentary.get_commentary(
        "USDC", {"current_bps": -1.0, "cone_p80_bps": 50.0},
        llm_client=_CountingLLM(),
        wait_for_llm=False,
    )
    assert out is not None
    assert out.symbol == "USDC"
    # No new LLM call on the foreground path.
    assert call_count["n"] == primed_calls, (
        "wait_for_llm=False must NOT block on the LLM")


def test_wait_for_llm_false_falls_back_to_deterministic_on_cold_cache(
        tmp_path, monkeypatch):
    """Cold cache + wait_for_llm=False → return the deterministic
    fallback immediately. The UI never sees 'loading…' for more than
    a network round-trip."""
    monkeypatch.setattr(commentary, "_CACHE_PATH",
                        tmp_path / "cold.json")

    out = commentary.get_commentary(
        "DAI", {"current_bps": -0.3, "cone_p80_bps": 1.5},
        llm_client=_FakeLLM('{"headline": "x", "body": "y [1]."}'),
        wait_for_llm=False,
    )
    assert out is not None
    assert out.symbol == "DAI"
    # The body should be the deterministic fallback (uses ctx.structural_one_liner)
    ctx = token_context.get_context("DAI")
    assert ctx.structural_one_liner[:30] in out.body


def test_commentary_malformed_context_returns_none_not_crash(monkeypatch):
    """Audit-fix regression: a registry entry with cone_thresholds_bps
    set to None / single-element / non-iterable used to crash inside a
    bare except clause and silently log a cache miss. Now it logs
    `movement.commentary.malformed_context` and returns None — caller
    falls through to deterministic without burning an LLM call."""
    # Build a fake context with broken thresholds. commentary.py
    # imports get_context with `from X import Y` — patch the bound
    # reference inside commentary's namespace, not token_context's.
    from dataclasses import replace
    real_usdc = token_context.get_context("USDC")
    broken = replace(real_usdc, cone_thresholds_bps=())  # empty tuple
    monkeypatch.setattr(
        commentary, "get_context",
        lambda sym: broken if sym.upper() == "USDC" else None,
    )
    out = commentary.get_commentary(
        "USDC", {"current_bps": -1.0, "cone_p80_bps": 2.0},
        llm_client=_FakeLLM('{"headline": "x", "body": "y [1]."}'),
    )
    assert out is None  # fail-closed, not crash


def test_commentary_backoff_state_evicts_stale_entries():
    """Audit-fix regression: _REFRESH_NEXT_OK_AT / _REFRESH_BACKOFF_FAILURES
    are module-level dicts. Without eviction they would grow unbounded
    over weeks of operation as symbols come and go. The eviction
    helper drops entries older than 2× max-backoff."""
    import time
    # Inject ancient entries
    commentary._REFRESH_NEXT_OK_AT["ANCIENT-1"] = time.time() - 100_000
    commentary._REFRESH_NEXT_OK_AT["ANCIENT-2"] = time.time() - 100_000
    commentary._REFRESH_BACKOFF_FAILURES["ANCIENT-1"] = 5
    commentary._REFRESH_BACKOFF_FAILURES["ANCIENT-2"] = 5
    commentary._REFRESH_NEXT_OK_AT["FRESH"] = time.time() + 60  # still active

    commentary._evict_stale_backoff_entries(time.time())

    assert "ANCIENT-1" not in commentary._REFRESH_NEXT_OK_AT
    assert "ANCIENT-2" not in commentary._REFRESH_NEXT_OK_AT
    assert "ANCIENT-1" not in commentary._REFRESH_BACKOFF_FAILURES
    assert "FRESH" in commentary._REFRESH_NEXT_OK_AT  # not stale


def test_cache_returns_quickly_on_second_call(tmp_path, monkeypatch):
    """Second invocation in the same regime bucket must hit cache —
    no second LLM call."""
    monkeypatch.setattr(commentary, "_CACHE_PATH",
                        tmp_path / "comm.json")
    call_count = {"n": 0}

    class _CountingLLM(_FakeLLM):
        def complete(self, *, system, prompt, max_tokens=300):
            call_count["n"] += 1
            return ('{"headline": "x", "body": "y see [1]."}')

    llm = _CountingLLM()
    commentary.get_commentary(
        "USDC", {"current_bps": -1.0, "cone_p80_bps": 2.0},
        llm_client=llm)
    commentary.get_commentary(
        "USDC", {"current_bps": -1.0, "cone_p80_bps": 2.0},
        llm_client=llm)
    assert call_count["n"] == 1, "second call should hit cache"
