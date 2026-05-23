"""The voting ledger. VOTES_PATH is isolated per-test by conftest."""
import pytest

from sca import votes
from sca.corpus.sources import all_sources


def test_source_exclusion_recorded():
    votes.record_source_decision("mica-title-iii", "excluded")
    assert votes.source_status_overrides() == {"mica-title-iii": "excluded"}


def test_later_vote_wins():
    votes.record_source_decision("mica-title-iii", "excluded")
    votes.record_source_decision("mica-title-iii", "included")
    assert votes.source_status_overrides()["mica-title-iii"] == "included"


def test_verified_vote_keeps_source_included():
    # 'verified' is a signal — the source stays included, and gains a badge.
    votes.record_source_decision("genius-act", "verified")
    assert votes.source_status_overrides()["genius-act"] == "included"
    assert votes.source_verified_overrides()["genius-act"] is True


def test_address_decision_recorded():
    votes.record_address_decision("PYUSD", "ethereum", "verified")
    assert votes.address_verified_overrides()[("PYUSD", "ethereum")] is True


def test_rejected_address_is_false():
    votes.record_address_decision("TUSD", "ethereum", "rejected")
    assert votes.address_verified_overrides()[("TUSD", "ethereum")] is False


def test_invalid_decisions_rejected():
    with pytest.raises(ValueError):
        votes.record_source_decision("x", "approved")
    with pytest.raises(ValueError):
        votes.record_address_decision("X", "ethereum", "maybe")


def test_empty_ledger():
    assert votes.source_status_overrides() == {}
    assert votes.source_verified_overrides() == {}
    assert votes.address_verified_overrides() == {}


def test_exclusion_flows_into_source_registry():
    # Sources ship 'included'; an exclude vote must override that.
    before = next(s for s in all_sources() if s.id == "mica-title-iii")
    assert before.included

    votes.record_source_decision("mica-title-iii", "excluded")
    all_sources.cache_clear()

    after = next(s for s in all_sources() if s.id == "mica-title-iii")
    assert after.excluded


def test_verify_flows_into_source_registry():
    before = next(s for s in all_sources() if s.id == "genius-act")
    assert not before.verified

    votes.record_source_decision("genius-act", "verified")
    all_sources.cache_clear()

    after = next(s for s in all_sources() if s.id == "genius-act")
    assert after.included and after.verified
