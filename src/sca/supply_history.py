"""Persisted per-chain supply history — drives the jump detector.

A misbehaving RPC could plausibly return a supply that's off by 10x — and
without history we'd have no baseline to question it. We persist the last
known supply per (symbol, chain) and flag any read that deviates beyond a
sanity threshold. First read for a (symbol, chain) is treated as the
baseline and never flags. Persistence is best-effort and never breaks the
caller.

The threshold is intentionally generous (2x up or down) — stablecoin
supplies do legitimately swing on mints/burns, especially on day one of
an unwind. The point is to catch *implausible* swings (a misread,
hijacked RPC, or wrong contract address), not normal trading flow.
"""
from __future__ import annotations

import json
from pathlib import Path

from sca.config import DATA_DIR
from sca.observability import log_event

# Above the upper bound or below the lower bound = "implausible" swing.
JUMP_UPPER = 2.0  # >2x previous = suspicious
JUMP_LOWER = 0.5  # <0.5x previous = suspicious

HISTORY_PATH = DATA_DIR / "supply_history.json"


def _load() -> dict:
    if not HISTORY_PATH.exists():
        return {}
    try:
        return json.loads(HISTORY_PATH.read_text())
    except Exception:  # noqa: BLE001 - corrupt history is non-fatal
        return {}


def _save(data: dict) -> None:
    try:
        from sca.persist import atomic_write_json
        atomic_write_json(HISTORY_PATH, data, sort_keys=False)
    except Exception:  # noqa: BLE001 - persistence never breaks the run
        pass


def _key(symbol: str, chain: str) -> str:
    return f"{symbol}:{chain}"


def last_known(symbol: str, chain: str) -> float | None:
    """Most recent persisted supply for (symbol, chain), or None."""
    return _load().get(_key(symbol, chain))


def record(symbol: str, chain: str, supply: float) -> None:
    """Persist the new reading. Idempotent; last-write-wins."""
    data = _load()
    data[_key(symbol, chain)] = supply
    _save(data)


def check_jump(
    symbol: str, chain: str, new_supply: float,
) -> tuple[bool, float | None]:
    """Compare `new_supply` against history. Returns (is_jump, prior).

    `is_jump=True` means the read exceeds the sanity bounds vs the prior
    reading. `prior=None` means no history — never flags as jump (this is
    the first read; we have nothing to compare against).

    Does NOT update history — callers decide when to commit (e.g. after
    the rest of the integrity checks pass).
    """
    prior = last_known(symbol, chain)
    if prior is None or prior == 0:
        return False, prior
    ratio = new_supply / prior
    is_jump = ratio >= JUMP_UPPER or ratio <= JUMP_LOWER
    if is_jump:
        log_event(
            "supply.jump", level="warn",
            symbol=symbol, chain=chain,
            prior=prior, new=new_supply, ratio=round(ratio, 4),
        )
    return is_jump, prior
