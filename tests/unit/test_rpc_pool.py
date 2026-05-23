"""Multi-RPC pool: cross-validation, fallback rotation, partial-result gate."""
from __future__ import annotations

import pytest

from sca.config import Chain
from sca.tools.onchain_supply import _pool_call


def _chain(rpcs: tuple[str, ...]) -> Chain:
    return Chain(name="test", kind="evm", rpcs=rpcs)


def test_pool_single_endpoint_succeeds():
    chain = _chain(("https://primary",))
    value, endpoint, consensus = _pool_call(
        chain, lambda url: 42, cross_check=True
    )
    assert value == 42
    assert endpoint == "https://primary"
    assert consensus == "1/1 single source"


def test_pool_sequential_uses_primary_when_it_works():
    chain = _chain(("https://primary", "https://fallback"))
    value, endpoint, consensus = _pool_call(
        chain, lambda url: 42, cross_check=False
    )
    assert (value, endpoint, consensus) == (42, "https://primary", "primary")


def test_pool_sequential_falls_back_on_primary_failure():
    chain = _chain(("https://primary", "https://fallback"))

    def maker(url):
        if "primary" in url:
            raise RuntimeError("primary down")
        return 99

    value, endpoint, consensus = _pool_call(chain, maker, cross_check=False)
    assert value == 99
    assert endpoint == "https://fallback"
    assert consensus.startswith("fallback")


def test_pool_corroborated_two_agree():
    chain = _chain(("https://primary", "https://fallback"))
    value, endpoint, consensus = _pool_call(
        chain, lambda url: 42, cross_check=True
    )
    assert value == 42
    assert consensus == "2/2 agree"


def test_pool_corroborated_one_fails_degrades_gracefully():
    """If the fallback is unreachable, return primary's value with degraded
    consensus — the system is honest about the lack of corroboration."""
    chain = _chain(("https://primary", "https://fallback"))

    def maker(url):
        if "fallback" in url:
            raise RuntimeError("fallback down")
        return 42

    value, _, consensus = _pool_call(chain, maker, cross_check=True)
    assert value == 42
    assert consensus == "1/2 single source"


def test_pool_corroborated_disagreement_resolved_by_majority():
    """Two-vs-one disagreement: tertiary breaks the tie. Majority wins."""
    chain = _chain(("https://primary", "https://fallback", "https://tertiary"))

    def maker(url):
        # primary says 100, fallback says 200, tertiary agrees with primary
        return {"primary": 100, "fallback": 200, "tertiary": 100}[
            url.split("//")[-1]
        ]

    value, _, consensus = _pool_call(chain, maker, cross_check=True)
    assert value == 100
    assert consensus == "2/3 majority"


def test_pool_corroborated_three_way_disagreement_returns_primary_loudly():
    """No majority = no truth. Primary returned, consensus flagged DISAGREEMENT."""
    chain = _chain(("https://primary", "https://fallback", "https://tertiary"))

    def maker(url):
        return {"primary": 1, "fallback": 2, "tertiary": 3}[
            url.split("//")[-1]
        ]

    value, _, consensus = _pool_call(chain, maker, cross_check=True)
    assert value == 1
    assert "DISAGREEMENT" in consensus


def test_pool_all_endpoints_fail_raises():
    chain = _chain(("https://a", "https://b"))

    def maker(url):
        raise RuntimeError(f"{url} down")

    with pytest.raises(RuntimeError):
        _pool_call(chain, maker, cross_check=True)
