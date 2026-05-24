"""Runtime config for the movement simulator.

User-facing knobs:
  - tick interval (default 10 min)
  - prediction horizon (default 60 min)
  - tracked symbols (default: top 5 fiat-backed by supply)
  - enabled kinds (peg_deviation, net_flow_direction)

Persisted in DATA_DIR/movement_config.json so a config change survives
restart. Read once at module load + on every ticker cycle so the
admin can change cadence without rebooting.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from sca.config import DATA_DIR

_CONFIG_PATH = DATA_DIR / "movement_config.json"

# Conservative defaults. The 10-minute tick is the user's spec; the
# 60-minute horizon is what the methodology research backs as the
# smallest defensible prediction window for trading-flow signals.
DEFAULTS: dict = {
    "enabled": True,
    "tick_interval_minutes": 10,
    "horizon_minutes": 60,
    "symbols": ["USDC", "USDT", "DAI", "PYUSD", "USDP"],
    "kinds": ["peg_deviation", "net_flow_direction"],
    # Cap the archive write rate so a config bug can't flood the
    # store. The ticker will skip when too many predictions are
    # in-flight for one symbol.
    "max_in_flight_per_symbol": 24,
}


def load() -> dict:
    """Read the persisted config + fill in defaults. Idempotent."""
    if not _CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(_CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    # Merge defaults so a new field shipped in code doesn't break a
    # config file from an older release.
    merged = dict(DEFAULTS)
    if isinstance(data, dict):
        merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged


def save(config: dict) -> dict:
    """Validate + persist a new config dict. Returns the merged
    config the caller should treat as current. Raises ValueError on
    out-of-range values so the API endpoint can return 400."""
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})
    _validate(merged)
    from sca.persist import atomic_write_json
    atomic_write_json(_CONFIG_PATH, merged)
    return merged


def _validate(c: dict) -> None:
    t = c.get("tick_interval_minutes")
    h = c.get("horizon_minutes")
    if not isinstance(t, int) or t < 1 or t > 1440:
        raise ValueError("tick_interval_minutes must be int in [1, 1440]")
    if not isinstance(h, int) or h < 1 or h > 1440:
        raise ValueError("horizon_minutes must be int in [1, 1440]")
    if not isinstance(c.get("symbols"), list) or not c["symbols"]:
        raise ValueError("symbols must be a non-empty list")
    if not all(isinstance(s, str) and s.strip() for s in c["symbols"]):
        raise ValueError("symbols must be non-empty strings")
    if not isinstance(c.get("kinds"), list) or not c["kinds"]:
        raise ValueError("kinds must be a non-empty list")
    valid_kinds = {"peg_deviation", "net_flow_direction"}
    bad = set(c["kinds"]) - valid_kinds
    if bad:
        raise ValueError(f"unknown prediction kind(s): {bad}")
