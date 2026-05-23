"""Per-IP token-bucket rate limiter.

A burst of N within the bucket capacity passes; the next one returns
429. The bucket refills over time at the configured rate."""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from web import rate_limit


class _FakeRequest:
    def __init__(self, ip: str = "1.2.3.4", fwd: str | None = None):
        self.headers = {"x-forwarded-for": fwd} if fwd else {}

        class _C:
            host = ip
        self.client = _C()


@pytest.fixture(autouse=True)
def _reset():
    rate_limit.reset_buckets_for_tests()
    yield
    rate_limit.reset_buckets_for_tests()


def test_first_call_allowed():
    rate_limit.enforce(_FakeRequest())  # no raise


def test_burst_of_capacity_allowed():
    """First CAPACITY calls in quick succession all pass."""
    for _ in range(int(rate_limit._CAPACITY)):
        rate_limit.enforce(_FakeRequest())


def test_one_past_capacity_rejected():
    for _ in range(int(rate_limit._CAPACITY)):
        rate_limit.enforce(_FakeRequest())
    with pytest.raises(HTTPException) as exc:
        rate_limit.enforce(_FakeRequest())
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


def test_per_ip_isolation():
    """One client exhausting their bucket doesn't affect another IP."""
    for _ in range(int(rate_limit._CAPACITY)):
        rate_limit.enforce(_FakeRequest(ip="9.9.9.9"))
    # Different IP: still allowed
    rate_limit.enforce(_FakeRequest(ip="1.1.1.1"))


def test_x_forwarded_for_first_value_used():
    """Proxy-aware: trust the first XFF entry."""
    for _ in range(int(rate_limit._CAPACITY)):
        rate_limit.enforce(_FakeRequest(fwd="203.0.113.5, 10.0.0.1"))
    # Another client behind the same proxy is independent
    rate_limit.enforce(_FakeRequest(fwd="203.0.113.99"))


def test_bucket_refills_over_time(monkeypatch):
    """After enough elapsed time, the bucket refills enough for one more call."""
    base = time.time()
    counter = [0]

    def fake_time():
        counter[0] += 1
        if counter[0] <= int(rate_limit._CAPACITY) + 1:
            return base  # all initial calls at t=0
        return base + 10  # 10 seconds later for the recovery call

    monkeypatch.setattr(rate_limit.time, "time", fake_time)

    for _ in range(int(rate_limit._CAPACITY)):
        rate_limit.enforce(_FakeRequest())
    # Bucket empty — t=0 still
    with pytest.raises(HTTPException):
        rate_limit.enforce(_FakeRequest())
    # 10 seconds later refills enough for another
    rate_limit.enforce(_FakeRequest())
