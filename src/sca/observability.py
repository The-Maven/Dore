"""Structured logging — JSON events on stderr today, Sentry tomorrow.

Every external dependency call (RPC, OFAC, attestation HTTP, LLM, snapshot,
canary, ingest) goes through `log_event`. The shape is intentionally
flat and field-stable so a future agent — or Sentry, or any log
aggregator — can correlate failures without parsing prose.

Why this is its own module: a downstream Sentry/OTLP integration is one
function swap, not a refactor. And in tests we capture events to assert
on observability behaviour, not just outcomes.

Levels follow standard syslog naming. `kind` is the verb-noun of the
event (`rpc.call`, `sdn.fetch`, `supply.jump`). Fields are free-form.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

# A correlation id threaded across a single user-facing operation
# (one analysis, one screen, one verify run). Lets Sentry stitch
# the call tree together later.
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def correlation_id() -> str:
    """Current correlation id, or empty string if none is set."""
    return _correlation_id.get()


@contextmanager
def correlation(name: str = "") -> Iterator[str]:
    """Bind a correlation id for the duration of the with-block.

    A new id is generated unless `name` is supplied. Nested blocks
    inherit the outer id — only the outermost block creates a new one.
    """
    cur = _correlation_id.get()
    if cur:
        yield cur
        return
    cid = name or uuid.uuid4().hex[:12]
    token = _correlation_id.set(cid)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)


# Pluggable sink — tests swap this in to capture events. Sentry would
# install its own sink here without touching call sites.
_SINK: Callable[[dict], None] = lambda event: sys.stderr.write(  # noqa: E731
    json.dumps(event, default=str) + "\n"
)


def set_sink(sink: Callable[[dict], None]) -> Callable[[dict], None]:
    """Install a new sink; return the previous one for restoration."""
    global _SINK
    prev = _SINK
    _SINK = sink
    return prev


def _enabled() -> bool:
    """Off in tests by default; on in production. Override with SCA_LOG=1."""
    flag = os.environ.get("SCA_LOG", "").strip().lower()
    if flag in ("0", "false", "off"):
        return False
    if flag in ("1", "true", "on"):
        return True
    # Default: silent under pytest, loud otherwise — observability without
    # noise in the test suite.
    return "pytest" not in sys.modules


def log_event(kind: str, level: str = "info", **fields: Any) -> None:
    """Emit a structured event. No-op when disabled."""
    if not _enabled():
        return
    event = {
        "ts": time.time(),
        "kind": kind,
        "level": level,
        "correlation_id": _correlation_id.get(),
    }
    event.update(fields)
    try:
        _SINK(event)
    except Exception:  # noqa: BLE001 - observability never breaks the caller
        pass


@contextmanager
def timed(kind: str, **fields: Any) -> Iterator[dict]:
    """Time a block and emit one event at the end.

    The yielded dict can be mutated to attach result fields the caller
    wants on the event (e.g. status_code, bytes, hash). On exception,
    the event is still emitted with `level=error` and `error_class` set.
    """
    extras: dict[str, Any] = {}
    start = time.perf_counter()
    try:
        yield extras
        log_event(
            kind,
            level=extras.pop("level", "info"),
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            **{**fields, **extras},
        )
    except Exception as exc:
        log_event(
            kind,
            level="error",
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            error_class=type(exc).__name__,
            error_message=str(exc),
            **{**fields, **extras},
        )
        raise
