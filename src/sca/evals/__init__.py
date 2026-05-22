"""Eval harness — the expert-in-the-loop socket.

A domain operator defines what good output looks like; each definition
becomes a case in evals/cases.yaml, graded here.
"""
from __future__ import annotations

from .harness import grade_case, load_cases, run_evals

__all__ = ["grade_case", "load_cases", "run_evals"]
