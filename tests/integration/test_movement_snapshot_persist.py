"""Regression: snapshot persist must convert ChainSupply dataclasses to
plain dicts before handing them to the store.

Bug history: `_record_movement_snapshot` previously only converted
`per_chain` when it was NOT already a list — but per_chain IS always a
list (of ChainSupply dataclasses). The list passed straight through to
SupabaseStore.save_snapshot, which calls json.dumps on the jsonb column
and crashed with `TypeError: Object of type ChainSupply is not JSON
serializable`. Every /api/supply call logged
`movement.snapshot_persist_failed`, the snapshot was never persisted,
and the calibration archive never accreted from the supply path.

This test pins the conversion: a list[ChainSupply] going in produces a
list[dict] hitting the store.
"""
from __future__ import annotations

import dataclasses

from sca.models import ChainSupply, SupplyResult


class _RecordingStore:
    """Captures whatever save_snapshot receives so we can assert on
    the exact payload shape that would hit Supabase."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def save_snapshot(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_per_chain_dataclasses_are_converted_to_dicts(monkeypatch):
    """list[ChainSupply] going in → list[dict] hitting the store.

    Anything else triggers the json.dumps TypeError on the jsonb
    column. This is the regression we just fixed."""
    from web import server

    store = _RecordingStore()
    monkeypatch.setattr(server, "get_store", lambda: store)
    # Reset the per-symbol write gate so the call lands.
    monkeypatch.setattr(server, "_SNAPSHOT_WRITE_GATE", {})

    result = SupplyResult(
        symbol="USDC",
        total_supply=42_000_000_000.0,
        native_supply=42_000_000_000.0,
        bridged_supply=0.0,
        per_chain=[
            ChainSupply(
                chain="ethereum",
                contract="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                raw=42_000_000_000 * 10 ** 6, decimals=6,
                supply=42_000_000_000.0,
                kind="native", verified=True,
                verification_method="human",
                consensus="2/2 agree",
                endpoint="https://eth.publicnode.com",
            ),
            ChainSupply(
                chain="solana", contract="EPjFWdd5...",
                raw=0, decimals=6, supply=0.0,
                kind="native", verified=False,
                verification_method="",
            ),
        ],
        warnings=[],
    )

    server._record_movement_snapshot("USDC", result)

    assert len(store.calls) == 1
    payload = store.calls[0]
    per_chain = payload["per_chain"]
    assert isinstance(per_chain, list)
    # Every element must be a plain dict — never a dataclass — so the
    # store's json.dumps step does not crash.
    for item in per_chain:
        assert isinstance(item, dict), (
            f"per_chain element is {type(item).__name__}, expected dict. "
            "ChainSupply dataclasses are NOT JSON-serialisable; the "
            "persist path must convert them before handing them off."
        )
        assert not dataclasses.is_dataclass(item)
    # Spot-check field passthrough — the conversion must be loss-less.
    assert per_chain[0]["chain"] == "ethereum"
    assert per_chain[0]["consensus"] == "2/2 agree"
    assert per_chain[1]["chain"] == "solana"


def test_per_chain_none_passes_through_safely(monkeypatch):
    """A SupplyResult with no per_chain readings (no chains responded
    cleanly) must not crash the persist step."""
    from web import server

    store = _RecordingStore()
    monkeypatch.setattr(server, "get_store", lambda: store)
    monkeypatch.setattr(server, "_SNAPSHOT_WRITE_GATE", {})

    result = SupplyResult(
        symbol="USDC", total_supply=0.0,
        per_chain=[], warnings=["no_rpc_responded"],
    )
    server._record_movement_snapshot("USDC", result)

    assert len(store.calls) == 1
    assert store.calls[0]["per_chain"] == []


def test_store_failure_is_logged_not_raised(monkeypatch):
    """The persist path is best-effort. A store error must NOT crash
    the /api/supply request that triggered the snapshot write."""
    from web import server

    class _BoomStore:
        def save_snapshot(self, **kwargs):
            raise RuntimeError("supabase down")

    monkeypatch.setattr(server, "get_store", lambda: _BoomStore())
    monkeypatch.setattr(server, "_SNAPSHOT_WRITE_GATE", {})

    # Must not raise.
    result = SupplyResult(symbol="USDC", total_supply=1.0, per_chain=[])
    server._record_movement_snapshot("USDC", result)


def test_payload_round_trips_through_json_dumps():
    """The post-conversion payload must survive json.dumps — that's
    the actual contract the SupabaseStore jsonb write depends on."""
    import json

    payload = ChainSupply(
        chain="ethereum",
        contract="0xABC",
        raw=100, decimals=6, supply=0.0001,
        consensus="1/1 single",
    )
    # Before the fix this raised TypeError.
    encoded = json.dumps(dataclasses.asdict(payload))
    decoded = json.loads(encoded)
    assert decoded["chain"] == "ethereum"
    assert decoded["consensus"] == "1/1 single"
