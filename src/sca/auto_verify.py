"""Auto-verification ledger — facts the agent established on its own.

A separate channel from `votes.py` on purpose:

  - votes.yaml is the human curation ledger — append-only audit trail of
    decisions a person made. It must not be polluted with machine writes.
  - This file is the machine's verification cache — what the on-chain
    self-report check (`sca.tools.address_verify`) established this run.
    A human vote always wins over an auto-verification of the same
    (symbol, chain), so the human lever is preserved.

When the address-verifier publishes a clean on-chain match it lands here.
The deployment loader (`sca.config.stablecoins`) overlays both ledgers
and surfaces the verification method on the deployment so the UI can show
a calmer "auto" vs "human" badge instead of the old "UNVERIFIED" wall.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sca import config

# Overridable in tests; mirrors the data/attestation_cache.json pattern.
AUTO_VERIFICATIONS_PATH = config.DATA_DIR / "auto_verifications.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    """Return the on-disk ledger as a dict keyed by 'symbol:chain'."""
    if not AUTO_VERIFICATIONS_PATH.exists():
        return {}
    try:
        return json.loads(AUTO_VERIFICATIONS_PATH.read_text()) or {}
    except json.JSONDecodeError:
        return {}


def _save(data: dict) -> None:
    from sca.persist import atomic_write_json
    atomic_write_json(AUTO_VERIFICATIONS_PATH, data)


def record_auto_verification(
    symbol: str,
    chain: str,
    *,
    on_chain_symbol: str,
    on_chain_decimals: int,
    method: str = "auto: on-chain symbol match",
) -> None:
    """Persist a successful auto-verification for (symbol, chain).

    Overwrites any prior auto entry for the same key — the latest reading
    wins, same as the human ledger's later-vote-wins semantics.
    """
    data = _load()
    data[f"{symbol}:{chain}"] = {
        "symbol": symbol,
        "chain": chain,
        "method": method,
        "on_chain_symbol": on_chain_symbol,
        "on_chain_decimals": on_chain_decimals,
        "at": _now(),
    }
    _save(data)


def clear_auto_verification(symbol: str, chain: str) -> None:
    """Drop the auto entry for (symbol, chain) — e.g. after a re-run mismatch."""
    data = _load()
    key = f"{symbol}:{chain}"
    if key in data:
        del data[key]
        _save(data)


def auto_verified_overrides() -> dict[tuple[str, str], bool]:
    """All auto-verified (symbol, chain) keys → True. Mirrors the votes API."""
    return {
        (entry["symbol"], entry["chain"]): True
        for entry in _load().values()
    }


def auto_verification_records() -> dict[tuple[str, str], dict]:
    """Full auto-verification records keyed by (symbol, chain)."""
    return {(entry["symbol"], entry["chain"]): entry for entry in _load().values()}


def reset_for_tests(path: Path) -> None:
    """Test helper: rebind the on-disk path so tests stay hermetic."""
    global AUTO_VERIFICATIONS_PATH
    AUTO_VERIFICATIONS_PATH = path
