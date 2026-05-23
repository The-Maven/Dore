"""Per-IP token bucket — keeps one noisy client from burning the LLM quota.

Pattern: classic token bucket per (key) with a refill rate. Heavy
endpoints (analyze / sanctions / redemption) consume one token; the
bucket refills at REFILL_PER_S until CAPACITY. When empty, return 429
with a Retry-After header so the SPA can back off cleanly.

In-memory implementation — fine for single-replica deploys. For
multi-replica, swap _Buckets for a Redis-backed equivalent without
touching the dependency surface (the call site only sees `consume()`).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

# Defaults: 30 heavy-endpoint calls per minute per IP, burst of 10. Tuned
# for "one human exploring freely is never throttled" and "one runaway
# script gets backed off in seconds." Override via env.
_CAPACITY = float(os.environ.get("SCA_RATE_BURST", "10"))
_REFILL_PER_S = float(os.environ.get("SCA_RATE_REFILL_PER_S", "0.5"))


@dataclass
class _Bucket:
    tokens: float
    last: float


_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()


def _client_key(request: Request) -> str:
    """Best-effort client identity.

    `X-Forwarded-For` is only honoured when `SCA_TRUST_PROXY` is set to "1"
    (or "true"/"yes") — production behind a trusted reverse proxy like
    Cloudflare/Railway. Otherwise we use the socket peer, so a hostile
    client cannot spoof a header to claim a fresh bucket and evade the
    rate limit. Default is paranoid (proxy NOT trusted): production must
    opt in explicitly.
    """
    if _trust_proxy():
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _trust_proxy() -> bool:
    """Whether to honour X-Forwarded-For. Read fresh so tests can flip it."""
    return os.environ.get("SCA_TRUST_PROXY", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _take_token(key: str) -> tuple[bool, float]:
    """Refill the bucket lazily; deduct one token if any remain.

    Returns (allowed, retry_after_seconds). retry_after is meaningful
    only when not allowed."""
    now = time.time()
    with _lock:
        b = _buckets.get(key)
        if b is None:
            b = _Bucket(tokens=_CAPACITY, last=now)
            _buckets[key] = b
        # Refill
        elapsed = now - b.last
        b.tokens = min(_CAPACITY, b.tokens + elapsed * _REFILL_PER_S)
        b.last = now
        if b.tokens >= 1:
            b.tokens -= 1
            return True, 0.0
        # Tokens to wait for: 1 - current; at REFILL_PER_S tokens/sec.
        deficit = 1 - b.tokens
        return False, round(deficit / _REFILL_PER_S, 1)


def enforce(request: Request) -> None:
    """FastAPI dependency: rejects the request with 429 if the bucket is
    empty for this IP. Wire into heavy endpoints via `Depends(enforce)`."""
    allowed, retry_after = _take_token(_client_key(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                "Rate limit exceeded. The Doré compute pool is bounded so "
                "no single client can starve others of LLM quota. Please "
                f"retry in ~{retry_after:.0f} seconds."
            ),
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def reset_buckets_for_tests() -> None:
    """Clear all in-memory state — tests call this between cases."""
    with _lock:
        _buckets.clear()
