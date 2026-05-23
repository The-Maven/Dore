"""Case-insensitive symbol lookup — regression guard for the USDe/USDf/crvUSD
bug where uppercasing the URL parameter caused 404s for the (correctly-cased)
mixed-case registry keys."""
from __future__ import annotations

import pytest

from sca import config


def test_canonical_case_returns_token():
    coin = config.get_stablecoin("USDe")
    assert coin.symbol == "USDe"


def test_lowercase_resolves_to_canonical():
    coin = config.get_stablecoin("usde")
    assert coin.symbol == "USDe"  # canonical case preserved


def test_uppercase_resolves_to_canonical():
    """The most common bug — URL params get uppercased and would miss USDe."""
    coin = config.get_stablecoin("USDE")
    assert coin.symbol == "USDe"


def test_mixed_case_uppercase_resolves():
    coin = config.get_stablecoin("CRVUSD")
    assert coin.symbol == "crvUSD"


def test_unknown_symbol_raises():
    with pytest.raises(ValueError, match="unknown stablecoin"):
        config.get_stablecoin("NOTREAL")


def test_canonical_lookup_passes_through_for_simple_cases():
    """USDC is upper-case in the registry; should resolve from any input."""
    assert config.get_stablecoin("USDC").symbol == "USDC"
    assert config.get_stablecoin("usdc").symbol == "USDC"
