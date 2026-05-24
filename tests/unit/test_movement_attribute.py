"""Attribute layer tests — prompt-injection defence, candidate gathering."""
from __future__ import annotations

from sca.movement.attribute import _sanitise, _summarise_event


def test_sanitise_strips_forged_citation_brackets():
    """Audit #14: external titles can smuggle '[3]' or '(W2)' into
    the LLM prompt. The sanitiser must strip these before they reach
    the prompt builder."""
    s = "Paxos warns [3] of impending action (W2)"
    out = _sanitise(s)
    assert "[3]" not in out
    assert "(W2)" not in out
    # Surrounding text preserved
    assert "Paxos" in out


def test_sanitise_handles_empty_and_none():
    assert _sanitise("") == ""
    assert _sanitise(None) is None


def test_summarise_event_sanitises_discovery_title():
    """End-to-end: a discovery event whose title contains a fake
    citation index lands in the prompt with that index stripped."""
    ev = {"kind": "discovery.published",
          "title": "Regulator update [4] arrives — paxos"}
    out = _summarise_event(ev)
    assert "[4]" not in out
    assert "paxos" in out.lower()
