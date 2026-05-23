"""Supply jump detector — guards against misbehaving RPCs / wrong addresses."""
from __future__ import annotations

from sca import supply_history


def test_first_read_never_flags():
    """No history = no baseline; the first read sets it without flagging."""
    is_jump, prior = supply_history.check_jump("USDC", "ethereum", 50_000_000_000)
    assert is_jump is False
    assert prior is None


def test_stable_supply_doesnt_flag():
    supply_history.record("USDC", "ethereum", 50_000_000_000)
    is_jump, prior = supply_history.check_jump("USDC", "ethereum", 51_000_000_000)
    assert is_jump is False
    assert prior == 50_000_000_000


def test_2x_upward_jump_flags():
    supply_history.record("USDC", "ethereum", 50_000_000_000)
    is_jump, prior = supply_history.check_jump("USDC", "ethereum", 100_000_000_001)
    assert is_jump is True
    assert prior == 50_000_000_000


def test_halving_flags():
    supply_history.record("USDC", "ethereum", 50_000_000_000)
    is_jump, prior = supply_history.check_jump("USDC", "ethereum", 25_000_000_000)
    assert is_jump is True


def test_within_bounds_doesnt_flag():
    """1.99x is borderline — not yet a jump."""
    supply_history.record("USDC", "ethereum", 50_000_000_000)
    is_jump, _ = supply_history.check_jump("USDC", "ethereum", 99_000_000_000)
    assert is_jump is False


def test_per_chain_isolation():
    """Ethereum history doesn't affect Polygon's baseline."""
    supply_history.record("USDC", "ethereum", 50_000_000_000)
    is_jump, prior = supply_history.check_jump("USDC", "polygon", 100)
    assert is_jump is False
    assert prior is None
