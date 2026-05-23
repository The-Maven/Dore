"""Atomic write helpers — our durable state must survive a crash mid-write.

Every JSON-state file Doré keeps (auto-verify ledger, votes, supply
history, discovery ledger, snapshot meta, etc.) is read at startup. If
the process crashes between `write_text("")` (truncate) and the actual
content landing, the file is corrupt and the next startup either loses
state or errors. Atomic rename eliminates that race.

Pattern: write to a uniquely-named temp file in the same directory,
fsync the file, then `os.replace` it onto the target — POSIX guarantees
that's atomic on the same filesystem. The temp filename includes the
PID so concurrent processes don't trample each other's temp files.

For body bytes (snapshots, SDN XML, attestation PDFs) the same pattern
applies — use `atomic_write_bytes`.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically — same FS, same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # `tempfile.NamedTemporaryFile(delete=False)` gives us a unique
    # filename in the same directory; we own its cleanup via os.replace.
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # POSIX atomic
    except Exception:
        # If anything went wrong, clean up the temp file rather than
        # leave .tmp.NNNN debris around. The original target is
        # untouched because we never wrote to it directly.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write `text` to `path` atomically."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, obj: Any, *, indent: int = 2,
                      sort_keys: bool = True) -> None:
    """Write `obj` to `path` atomically as JSON."""
    atomic_write_text(path, json.dumps(obj, indent=indent, sort_keys=sort_keys))
