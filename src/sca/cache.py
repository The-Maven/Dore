"""A tiny thread-safe TTL cache.

Speed: repeated RPC / fetch calls within a short window return instantly
instead of re-hitting the network. Used by the on-chain supply tool.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULT_TTL = 60.0


class TTLCache:
    def __init__(self, ttl: float = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._store: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: Any, producer: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        value = producer()  # produced outside the lock
        with self._lock:
            self._store[key] = (time.monotonic(), value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class AnalysisCache:
    """Caches completed analyses by an input fingerprint, so an unchanged
    analysis is not recomputed — no repeat LLM extraction or synthesis.

    The fingerprint buckets supply to 3 significant figures (block-to-block
    drift is not a material change) and includes a corpus version (approving
    a source invalidates the cache). In-memory for now — this class is the
    deliberate seam where a persistent store (Supabase) slots in later;
    callers never change.
    """

    def __init__(self, ttl: float = 3600.0) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def fingerprint(
        symbol: str, total_supply: float, corpus_version: str
    ) -> str:
        bucket = f"{total_supply:.3g}" if total_supply else "0"
        return f"{symbol}|{bucket}|{corpus_version}"

    def get(self, fingerprint: str) -> Any | None:
        with self._lock:
            hit = self._store.get(fingerprint)
            if hit is not None and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
        return None

    def put(self, fingerprint: str, value: Any) -> None:
        with self._lock:
            self._store[fingerprint] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
