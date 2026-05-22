"""OFAC sanctions tool — SDN parsing, screening, guardrails."""
from datetime import date, timedelta

import pytest

import sca.tools.sanctions as sanc
from sca.models import SanctionsScreen, SdnAddress, SupplyResult
from sca.tools.sanctions import SanctionsUnavailable, SdnList, screen
from sca.validation import validate_sanctions

_SDN_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<sdnList xmlns="http://tempuri.org/sdnList.xsd">
  <publshInformation><Publish_Date>05/21/2026</Publish_Date></publshInformation>
  <sdnEntry>
    <uid>9999</uid><firstName>BAD</firstName><lastName>ACTOR</lastName>
    <idList>
      <id><uid>1</uid><idType>Digital Currency Address - ETH</idType>
        <idNumber>0xBADaddr01</idNumber></id>
      <id><uid>2</uid><idType>Digital Currency Address - TRX</idType>
        <idNumber>TBadTron01</idNumber></id>
      <id><uid>3</uid><idType>Passport</idType><idNumber>X123</idNumber></id>
    </idList>
  </sdnEntry>
</sdnList>"""


def test_parse_extracts_digital_currency_addresses():
    sdn = sanc._parse(_SDN_XML)
    assert sdn.publish_date == "05/21/2026"
    assert set(sdn.addresses) == {"0xbadaddr01", "tbadtron01"}
    eth = sdn.addresses["0xbadaddr01"]
    assert eth.currency == "ETH"
    assert eth.sdn_name == "BAD ACTOR"


def test_parse_no_crypto_addresses_raises():
    xml = b'<sdnList xmlns="x"><sdnEntry><uid>1</uid></sdnEntry></sdnList>'
    with pytest.raises(SanctionsUnavailable):
        sanc._parse(xml)


def test_staleness_days():
    published = (date.today() - timedelta(days=5)).strftime("%m/%d/%Y")
    assert SdnList(publish_date=published, addresses={}).staleness_days == 5


def test_screen_matches_case_insensitive(monkeypatch):
    monkeypatch.setattr(sanc, "load_sdn", lambda **k: sanc._parse(_SDN_XML))
    hits = screen(["0xBADADDR01", "0xCLEAN", "TBadTron01"])
    assert {h.address for h in hits} == {"0xBADaddr01", "TBadTron01"}


def test_screen_clean(monkeypatch):
    monkeypatch.setattr(sanc, "load_sdn", lambda **k: sanc._parse(_SDN_XML))
    assert screen(["0xCLEAN1", "0xCLEAN2"]) == []


def _screen(hits=(), count=796, stale=1) -> SanctionsScreen:
    return SanctionsScreen(
        symbol="X",
        supply=SupplyResult(symbol="X", total_supply=1.0),
        hits=list(hits),
        sdn_address_count=count,
        sdn_publish_date="05/21/2026",
        sdn_staleness_days=stale,
    )


def test_validate_sanctions_clean_passes():
    assert all(c.passed for c in validate_sanctions(_screen()))


def test_validate_sanctions_hit_is_critical():
    hit = SdnAddress("0xbad", "ETH", "BAD ACTOR", "9999")
    chk = next(
        c for c in validate_sanctions(_screen(hits=[hit]))
        if c.name == "no_sanctioned_addresses"
    )
    assert not chk.passed and chk.severity == "critical"


def test_validate_sanctions_stale_list_warns():
    chk = next(
        c for c in validate_sanctions(_screen(stale=90))
        if c.name == "sdn_list_fresh"
    )
    assert not chk.passed and chk.severity == "warn"
