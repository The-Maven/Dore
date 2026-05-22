import time

from sca.cache import TTLCache


def test_caches_and_reuses_value():
    cache = TTLCache(ttl=10)
    calls: list[int] = []

    def produce() -> int:
        calls.append(1)
        return 42

    assert cache.get_or_set("k", produce) == 42
    assert cache.get_or_set("k", produce) == 42
    assert len(calls) == 1  # producer ran once


def test_value_expires():
    cache = TTLCache(ttl=0.01)
    cache.get_or_set("k", lambda: 1)
    time.sleep(0.02)
    assert cache.get_or_set("k", lambda: 2) == 2


def test_clear_drops_entries():
    cache = TTLCache(ttl=10)
    cache.get_or_set("k", lambda: 1)
    cache.clear()
    assert cache.get_or_set("k", lambda: 2) == 2
