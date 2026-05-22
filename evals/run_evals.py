#!/usr/bin/env python3
"""Run the eval harness with themed output.

Thin wrapper so `python evals/run_evals.py` keeps working — it routes
through the themed CLI (equivalent to `sca evals`).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sca.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["evals"]))
