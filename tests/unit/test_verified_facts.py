"""Immutable, object-agnostic fact store (Phase 2).

The contract pinned here:

- `record_verified_fact` is append-only — no in-place edits.
- A second call with identical (value, sources) produces a different row
  (the column we DEDUP on at the writer side is content_hash; the table
  itself never blocks the second write — the writer just learns it can
  skip if hashes match).
- `latest_verified_fact` returns the most-recent row for a
  (claim_type, subject) pair.
- The schema is generic — same shape for reserves (Lens 1) and the
  future agent-payment claims (Lens 2). The test exercises both.
- Point-in-time replay: `as_of_lte` returns the snapshot that was
  current at a past timestamp.
"""
from __future__ import annotations

import time

from sca.store import get_store


def test_record_and_latest_simple():
    store = get_store()
    store.record_verified_fact(
        claim_type="reserves",
        subject="USDC",
        value={"total_reserves_usd": 42_000_000_000.0,
               "tokens_outstanding": 41_500_000_000.0},
        sources=[{"kind": "attestation", "url": "https://x/a.pdf"}],
        status="verified",
        as_of="2026-05-01T00:00:00Z",
    )
    out = store.latest_verified_fact("reserves", "USDC")
    assert out is not None
    assert out["status"] == "verified"
    assert out["value"]["total_reserves_usd"] == 42_000_000_000.0
    assert out["content_hash"]  # populated


def test_append_only_last_wins_on_latest():
    """Two writes for the same subject — latest_verified_fact returns the
    most recent observed_at."""
    store = get_store()
    store.record_verified_fact(
        claim_type="reserves", subject="USDC",
        value={"total_reserves_usd": 40_000_000_000.0},
        sources=[{"url": "https://x/old.pdf"}],
        status="verified",
    )
    time.sleep(0.01)  # ensure observed_at differs
    store.record_verified_fact(
        claim_type="reserves", subject="USDC",
        value={"total_reserves_usd": 41_000_000_000.0},
        sources=[{"url": "https://x/new.pdf"}],
        status="verified",
    )
    out = store.latest_verified_fact("reserves", "USDC")
    assert out["value"]["total_reserves_usd"] == 41_000_000_000.0


def test_object_agnostic_lens2_shape():
    """The same table is the home for Lens 2 (agent payments — future).
    Today we just prove the shape accepts a generic claim_type +
    arbitrary value blob — no schema change needed."""
    store = get_store()
    store.record_verified_fact(
        claim_type="agent_payment",
        subject="agent:0xabc",
        value={
            "amount_usd": 12.5,
            "from": "agent:0xabc",
            "to": "merchant:0xdef",
            "authorisation_ref": "ap2:auth-xyz",
        },
        sources=[
            {"kind": "x402", "ref": "tx:0x123"},
            {"kind": "on_chain", "ref": "0x456789", "chain": "ethereum"},
        ],
        status="verified",
        chain="ethereum",
        block_number=21_900_000,
    )
    out = store.latest_verified_fact("agent_payment", "agent:0xabc")
    assert out is not None
    assert out["claim_type"] == "agent_payment"
    assert out["block_number"] == 21_900_000
    assert out["value"]["authorisation_ref"] == "ap2:auth-xyz"


def test_content_hash_stable_for_equal_inputs():
    """Identical (value, sources) produce identical content_hash; the
    writer can use this to skip a redundant write."""
    store = get_store()
    store.record_verified_fact(
        claim_type="supply", subject="USDC:ethereum",
        value={"supply": 40_000_000_000.0, "decimals": 6},
        sources=[{"kind": "rpc", "url": "https://rpc"}],
        status="verified",
    )
    store.record_verified_fact(
        claim_type="supply", subject="USDC:ethereum",
        value={"supply": 40_000_000_000.0, "decimals": 6},
        sources=[{"kind": "rpc", "url": "https://rpc"}],
        status="verified",
    )
    rows = store.list_verified_facts(
        claim_type="supply", subject="USDC:ethereum",
    )
    assert len(rows) == 2  # append-only; both rows persisted
    assert rows[0]["content_hash"] == rows[1]["content_hash"]


def test_point_in_time_reproducibility():
    """as_of_lte returns the latest fact <= a past timestamp — i.e.
    what would have been reported then."""
    store = get_store()
    store.record_verified_fact(
        claim_type="reserves", subject="USDT",
        value={"total_reserves_usd": 150_000_000_000.0},
        sources=[{"url": "https://x/q1.pdf"}], status="verified",
    )
    pin = store.latest_verified_fact("reserves", "USDT")["observed_at"]
    time.sleep(0.01)
    store.record_verified_fact(
        claim_type="reserves", subject="USDT",
        value={"total_reserves_usd": 200_000_000_000.0},
        sources=[{"url": "https://x/q4.pdf"}], status="verified",
    )
    # As-of the pin: the Q1 figure is the latest known
    historical = store.latest_verified_fact(
        "reserves", "USDT", as_of_lte=pin,
    )
    assert historical["value"]["total_reserves_usd"] == 150_000_000_000.0


def test_status_validation():
    """Status must be one of the three allowed values."""
    store = get_store()
    try:
        store.record_verified_fact(
            claim_type="reserves", subject="USDC",
            value={}, sources=[], status="wishful",
        )
    except ValueError as exc:
        assert "verified|unverified|assumed" in str(exc)
    else:
        raise AssertionError("expected ValueError on bad status")
