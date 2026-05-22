"""Persistence abstraction layer.

`get_store()` returns the process-singleton backend: `SupabaseStore` when
Supabase credentials are configured, else `FileStore` (offline / tests).
Call sites depend only on the `Store` interface.
"""
from __future__ import annotations

from sca import config
from sca.store.base import Store
from sca.store.file_store import FileStore

__all__ = ["Store", "FileStore", "get_store", "reset_store"]

_store: Store | None = None


def get_store() -> Store:
    """Return the process-wide store singleton, constructing it on first use."""
    global _store
    if _store is None:
        if config.supabase_configured():
            # Imported lazily so the supabase dep is only needed in prod.
            from sca.store.supabase_store import SupabaseStore

            _store = SupabaseStore()
        else:
            _store = FileStore()
    return _store


def reset_store() -> None:
    """Drop the cached singleton — for tests that swap configuration."""
    global _store
    _store = None
