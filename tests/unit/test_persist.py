"""Atomic write helpers — crash-safety for our durable state.

Without atomic_write, a crash between `truncate` and `write` leaves the
target file empty or truncated; the next startup reads it and either
loses state or errors. Tests below simulate the failure modes and
prove that the original file is preserved when the write fails."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sca.persist import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
)


def test_atomic_write_text_creates_file(tmp_path: Path):
    target = tmp_path / "state.json"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_atomic_write_text_overwrites_existing(tmp_path: Path):
    target = tmp_path / "state.json"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_json_roundtrip(tmp_path: Path):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"b": 2, "a": 1})
    on_disk = json.loads(target.read_text())
    assert on_disk == {"a": 1, "b": 2}


def test_atomic_write_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "nested" / "deep" / "state.json"
    atomic_write_json(target, {"ok": True})
    assert target.exists()


def test_failure_during_write_preserves_original(tmp_path: Path):
    """The whole point: if write fails mid-flight, the original is intact."""
    target = tmp_path / "state.json"
    atomic_write_text(target, "original content")

    # Simulate a disk-full / kill -9 by making os.replace raise.
    with patch("sca.persist.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            atomic_write_text(target, "new content that never lands")

    # The original is still there, fully intact.
    assert target.read_text() == "original content"


def test_failure_during_write_cleans_up_temp(tmp_path: Path):
    target = tmp_path / "state.json"
    with patch("sca.persist.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            atomic_write_text(target, "doomed")
    # No .tmp.NNNN debris left behind
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".state")]
    assert leftovers == []


def test_atomic_write_bytes_handles_binary(tmp_path: Path):
    target = tmp_path / "blob.bin"
    payload = bytes(range(256))
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload
