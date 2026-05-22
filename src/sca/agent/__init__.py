"""Agent layer — synthesis of facts + corpus into cited analyses.

Three compliance surfaces, one shape each: deterministic facts, guardrails,
corpus-grounded synthesis, citation verification.
  analyze            — attestation vs on-chain supply
  screen_token       — OFAC sanctions screening
  assess_redemption  — redemption capacity
"""
from __future__ import annotations

from .analyze import analyze
from .redemptions import assess_redemption
from .sanctions import screen_token
from .synthesis import skill_prompt, synthesize, synthesize_surface

__all__ = [
    "analyze",
    "screen_token",
    "assess_redemption",
    "synthesize",
    "synthesize_surface",
    "skill_prompt",
]
