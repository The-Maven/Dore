"""The voting ledger. VOTES_PATH is isolated per-test by conftest."""
import pytest

from sca import votes
from sca.corpus.sources import all_sources


def test_source_decision_recorded():
    votes.record_source_decision("mica-title-iii", "approved")
    assert votes.source_status_overrides() == {"mica-title-iii": "approved"}


def test_later_vote_wins():
    votes.record_source_decision("mica-title-iii", "approved")
    votes.record_source_decision("mica-title-iii", "rejected")
    assert votes.source_status_overrides()["mica-title-iii"] == "rejected"


def test_address_decision_recorded():
    votes.record_address_decision("PYUSD", "ethereum", "verified")
    assert votes.address_verified_overrides()[("PYUSD", "ethereum")] is True


def test_rejected_address_is_false():
    votes.record_address_decision("TUSD", "ethereum", "rejected")
    assert votes.address_verified_overrides()[("TUSD", "ethereum")] is False


def test_invalid_decisions_rejected():
    with pytest.raises(ValueError):
        votes.record_source_decision("x", "maybe")
    with pytest.raises(ValueError):
        votes.record_address_decision("X", "ethereum", "maybe")


def test_empty_ledger():
    assert votes.source_status_overrides() == {}
    assert votes.address_verified_overrides() == {}


def test_vote_flows_into_source_registry():
    # Sources ship as 'proposed'; a vote must override that to 'approved'.
    before = next(s for s in all_sources() if s.id == "mica-title-iii")
    assert not before.approved

    votes.record_source_decision("mica-title-iii", "approved")
    all_sources.cache_clear()

    after = next(s for s in all_sources() if s.id == "mica-title-iii")
    assert after.approved
