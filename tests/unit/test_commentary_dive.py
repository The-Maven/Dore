"""Commentary DIVE — structured deeper-read layer.

Pins: caching by inputs-hash, deterministic fallback on LLM failure,
preserved citations across the deeper layer."""
from __future__ import annotations

import json
import pytest

from sca.movement import commentary_dive as cd


@pytest.fixture(autouse=True)
def _isolated_dive_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "DIVE_DIR", tmp_path)


def _patch_llm(monkeypatch, payload):
    monkeypatch.setattr(cd, "_llm_extract_json",
                         lambda prompt: payload)


_CITES = [
    {"n": 1, "url": "https://sky.money", "label": "Sky Protocol docs"},
    {"n": 2, "url": "https://makerdao.com", "label": "MakerDAO archive"},
]


def test_dive_generates_and_caches(monkeypatch):
    """First call hits the LLM; second call with identical inputs
    returns the cached file without re-asking."""
    _patch_llm(monkeypatch, {
        "intro": "USDS deeper read: quiet regime today.",
        "sections": [{
            "heading": "Breaking down the concepts",
            "items": [{
                "quote": "USDS is Sky's flagship dollar [1]",
                "explanations": [
                    {"label": "The Token",
                      "body": "Sky Protocol rebranded from MakerDAO. [1, 2]"},
                ],
            }],
        }],
    })
    dive = cd.generate_dive(
        symbol="USDS",
        headline="USDS is decentralized.",
        body="USDS is Sky's flagship dollar [1]",
        pl_lens="Yield-passive at USDS level; sUSDS captures SSR. [1]",
        citations=_CITES,
        live={"current_bps": -0.6, "cone_p80_bps": 1.4},
        ctx_extras={"backing_model": "crypto + RWA",
                     "watchlist_signal": "USDS/DAI conversion"},
    )
    assert dive.symbol == "USDS"
    assert "Breaking down" in dive.sections[0]["heading"]
    assert dive.citations == _CITES  # preserved across the deeper layer
    assert not dive.fallback

    # Cache hit — LLM should not be called again
    monkeypatch.setattr(cd, "_llm_extract_json",
                         lambda prompt: pytest.fail("cache should hit"))
    dive2 = cd.generate_dive(
        symbol="USDS",
        headline="USDS is decentralized.",
        body="USDS is Sky's flagship dollar [1]",
        pl_lens="Yield-passive at USDS level; sUSDS captures SSR. [1]",
        citations=_CITES,
        live={"current_bps": -0.6, "cone_p80_bps": 1.4},
        ctx_extras={"backing_model": "crypto + RWA",
                     "watchlist_signal": "USDS/DAI conversion"},
    )
    assert dive2.intro == dive.intro
    assert dive2.sections == dive.sections


def test_dive_falls_back_on_llm_failure(monkeypatch):
    """LLM down → deterministic skeleton, fallback flag set, the
    shallow commentary is still cited so a reader can fall back to it."""
    _patch_llm(monkeypatch, None)
    dive = cd.generate_dive(
        symbol="USDC",
        headline="USDC is fiat-backed.",
        body="Cash + short-dated Treasuries with monthly attestations [1]",
        pl_lens="No yield at USDC level.",
        citations=[{"n": 1, "url": "https://circle.com",
                     "label": "Circle transparency"}],
        live={"current_bps": -1.0, "cone_p80_bps": 3.2},
        ctx_extras={},
    )
    assert dive.fallback is True
    assert "USDC" in dive.intro
    assert dive.citations  # citations from shallow card flow through


def test_dive_cache_invalidates_when_body_changes(monkeypatch):
    """When the upstream commentary body changes (regime bucket flip,
    cheat-sheet edit), the inputs_hash changes and the LLM is
    re-asked — not served stale."""
    _patch_llm(monkeypatch, {
        "intro": "first version",
        "sections": [{"heading": "Breaking down the concepts",
                       "items": [{"explanations": []}]}],
    })
    cd.generate_dive(
        symbol="USDC",
        headline="USDC is fiat-backed.",
        body="version one of the body",
        pl_lens="",
        citations=[],
        live={},
        ctx_extras={},
    )
    _patch_llm(monkeypatch, {
        "intro": "second version after upstream commentary changed",
        "sections": [{"heading": "Breaking down the concepts",
                       "items": [{"explanations": []}]}],
    })
    dive_v2 = cd.generate_dive(
        symbol="USDC",
        headline="USDC is fiat-backed.",
        body="version two — the cheat sheet was edited",  # different
        pl_lens="",
        citations=[],
        live={},
        ctx_extras={},
    )
    assert "second version" in dive_v2.intro, (
        "different inputs must invalidate the cache")
