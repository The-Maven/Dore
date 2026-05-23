"""Paxos attestation URL resolver — hermetic, no real network calls."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

import sca.tools.paxos_resolver as pr
from sca.tools.paxos_resolver import (
    PAXOS_TOKENS,
    resolve_paxos_attestation_url,
)


class _FakeResp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _fixed_today(monkeypatch, year: int, month: int, day: int = 15) -> date:
    """Pin date.today() inside paxos_resolver to a known calendar day."""
    fixed = date(year, month, day)

    class _FakeDate(date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr(pr, "date", _FakeDate)
    return fixed


# ── URL construction sanity ──────────────────────────────────────────
def test_candidate_url_shape_april_2026(monkeypatch):
    """The April-2026 attestation is published in May 2026 — verify the path."""
    _fixed_today(monkeypatch, 2026, 5, 20)
    candidates = pr._candidate_urls("PYUSD")
    # Most recent publication month first.
    first_url, first_month = candidates[0]
    assert first_url == (
        "https://www.paxos.com/wp-content/uploads/2026/05/"
        "PYUSD-Attestation-Report-April-2026.pdf"
    )
    assert first_month == "2026-04"


def test_candidate_url_january_rolls_back_to_december(monkeypatch):
    """Jan publication month -> December of prior year as attestation period."""
    _fixed_today(monkeypatch, 2026, 1, 10)
    url, attestation_month = pr._candidate_url("USDP", date(2026, 1, 1))
    assert url == (
        "https://www.paxos.com/wp-content/uploads/2026/01/"
        "USDP-Attestation-Report-December-2025.pdf"
    )
    assert attestation_month == "2025-12"


# ── miss path: non-Paxos symbol never makes an HTTP call ─────────────
def test_non_paxos_symbol_returns_none_without_http(monkeypatch):
    head = MagicMock()
    get = MagicMock()
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", get)
    assert resolve_paxos_attestation_url("USDC") is None
    assert resolve_paxos_attestation_url("DAI") is None
    head.assert_not_called()
    get.assert_not_called()


# ── happy path: first probe wins ─────────────────────────────────────
def test_first_url_hits(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(return_value=_FakeResp(200))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())

    url = resolve_paxos_attestation_url("PYUSD")
    assert url == (
        "https://www.paxos.com/wp-content/uploads/2026/05/"
        "PYUSD-Attestation-Report-April-2026.pdf"
    )
    # Only ONE HEAD request — first probe was a hit.
    assert head.call_count == 1


# ── fallback path: probe walks back through the lookback window ──────
def test_sixth_url_hits_after_five_misses(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    responses = [_FakeResp(404)] * 5 + [_FakeResp(200)]
    head = MagicMock(side_effect=responses)
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())

    url = resolve_paxos_attestation_url("USDG")
    assert url is not None
    # The 6th probe corresponds to publication month = 2025-12 -> attestation
    # period November-2025.
    assert url == (
        "https://www.paxos.com/wp-content/uploads/2025/12/"
        "USDG-Attestation-Report-November-2025.pdf"
    )
    assert head.call_count == 6


def test_all_six_404_returns_none(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(return_value=_FakeResp(404))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())

    assert resolve_paxos_attestation_url("PYUSD") is None
    assert head.call_count == 6


# ── transport errors are swallowed (best-effort contract) ────────────
def test_transport_error_returns_none(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(side_effect=requests.ConnectionError("network down"))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())

    # Resolver must NEVER raise — best-effort, returns None on full failure.
    assert resolve_paxos_attestation_url("USDP") is None


# ── HEAD-not-honoured: fall back to a ranged GET ─────────────────────
def test_head_405_falls_back_to_get(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(return_value=_FakeResp(405))
    get = MagicMock(return_value=_FakeResp(206))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", get)

    url = resolve_paxos_attestation_url("PYUSD")
    assert url == (
        "https://www.paxos.com/wp-content/uploads/2026/05/"
        "PYUSD-Attestation-Report-April-2026.pdf"
    )
    # HEAD was tried, then GET fallback fired.
    assert head.call_count == 1
    assert get.call_count == 1
    # GET fallback used a tiny Range header to avoid downloading the body.
    _, kwargs = get.call_args
    assert kwargs["headers"].get("Range") == "bytes=0-0"


# ── cache short-circuits a second resolve ────────────────────────────
def test_cache_short_circuits_second_call(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(return_value=_FakeResp(200))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())

    first = resolve_paxos_attestation_url("PYUSD")
    second = resolve_paxos_attestation_url("PYUSD")
    assert first == second
    # Only the first call probed the network. The second call read the cache.
    assert head.call_count == 1


def test_cache_per_symbol_is_independent(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(return_value=_FakeResp(200))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())

    pyusd = resolve_paxos_attestation_url("PYUSD")
    usdp = resolve_paxos_attestation_url("USDP")
    assert pyusd != usdp
    assert "PYUSD" in pyusd
    assert "USDP" in usdp
    assert head.call_count == 2


def test_cache_invalidated_when_calendar_month_rolls(monkeypatch):
    """A cached entry from a prior calendar month is re-probed."""
    # Seed a cache entry stamped to "last month".
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(return_value=_FakeResp(200))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())
    resolve_paxos_attestation_url("PYUSD")
    assert head.call_count == 1

    # Roll the clock forward into a new calendar month.
    _fixed_today(monkeypatch, 2026, 6, 15)
    resolve_paxos_attestation_url("PYUSD")
    # Cache from May was stale; June re-probed.
    assert head.call_count == 2


# ── module exports the public surface the issue specified ────────────
def test_paxos_tokens_constant():
    assert PAXOS_TOKENS == {"PYUSD", "USDP", "USDG"}


# ── case-insensitive symbol input ────────────────────────────────────
def test_lowercase_symbol_accepted(monkeypatch):
    _fixed_today(monkeypatch, 2026, 5, 20)
    head = MagicMock(return_value=_FakeResp(200))
    monkeypatch.setattr(pr.requests, "head", head)
    monkeypatch.setattr(pr.requests, "get", MagicMock())

    url = resolve_paxos_attestation_url("pyusd")
    assert url is not None
    assert "PYUSD" in url


# ── integration: attestation_fetch chain uses the resolver ───────────
def test_attestation_fetch_wires_paxos_resolver(monkeypatch, tmp_path):
    """When seed is broken and symbol is Paxos, resolver runs before locator."""
    import sca.tools.attestation_fetch as af

    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")

    # Seed HEAD check fails so we skip past the seed branch.
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: "resolved.pdf" in url)
    monkeypatch.setattr(
        af, "resolve_paxos_attestation_url",
        lambda s: "https://www.paxos.com/wp-content/uploads/2026/05/"
                  "PYUSD-resolved.pdf",
    )

    # Locator should NOT be called when the resolver succeeds.
    def boom(*a, **k):
        raise AssertionError("locator must not run when paxos resolver hits")

    monkeypatch.setattr(af, "resolve_attestation_url", boom)

    out = af.resolve_url("PYUSD", refresh=True)
    assert out["via"] == "paxos_resolver"
    assert out["url"].endswith("PYUSD-resolved.pdf")
