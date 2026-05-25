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
