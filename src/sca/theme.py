"""The consultancy design system, for the terminal.

Gold-on-dark — one source of truth so every interface adheres to it.
Truecolor ANSI; degrades to plain text when stdout is not a TTY or when
NO_COLOR is set. Mirrors docs/architecture-booklet.html.
"""
from __future__ import annotations

import os
import sys

RGB = tuple[int, int, int]

# ── palette (matches the consultancy booklet) ─────────────────────────
INK: RGB = (10, 11, 14)        # #0A0B0E
INK_2: RGB = (17, 19, 23)      # #111317
PAPER: RGB = (239, 234, 224)   # #EFEAE0
GOLD: RGB = (212, 162, 74)     # #D4A24A
GREEN: RGB = (127, 224, 166)   # #7FE0A6
AMBER: RGB = (247, 185, 85)    # #F7B955
ROSE: RGB = (248, 113, 113)    # #F87171
MUTED: RGB = (150, 146, 138)
MUTED_2: RGB = (108, 105, 99)

RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"


def _enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(text: str, rgb: RGB, *, bold: bool = False, dim: bool = False) -> str:
    if not _enabled():
        return text
    seq = f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if bold:
        seq += _BOLD
    if dim:
        seq += _DIM
    return f"{seq}{text}{RESET}"


def on_gold(text: str) -> str:
    """Ink text on a gold field — the booklet's numbered-tag look."""
    if not _enabled():
        return text
    return (
        f"\033[48;2;{GOLD[0]};{GOLD[1]};{GOLD[2]}m"
        f"\033[38;2;{INK[0]};{INK[1]};{INK[2]}m{_BOLD}{text}{RESET}"
    )


# ── color iconography (used across every interface) ───────────────────
ICONS: dict[str, tuple[str, RGB]] = {
    "ok": ("●", GREEN),
    "warn": ("▲", AMBER),
    "error": ("✗", ROSE),
    "info": ("◆", GOLD),
    "supply": ("⛓", GOLD),
    "reserve": ("▣", GOLD),
    "corpus": ("❖", GOLD),
    "agent": ("◈", GOLD),
    "metric": ("∑", GOLD),
    "doc": ("▤", GOLD),
    "cite": ("§", MUTED),
    "arrow": ("→", MUTED),
    "bullet": ("·", MUTED),
}


def icon(name: str) -> str:
    glyph, rgb = ICONS.get(name, ICONS["info"])
    return paint(glyph, rgb)


# ── layout primitives ─────────────────────────────────────────────────
def rule(width: int = 66) -> str:
    return paint("─" * width, MUTED_2, dim=True)


def brandmark() -> str:
    return on_gold(" RS ")


def heading(num: str, title: str) -> str:
    return f"{on_gold(f' {num} ')}  {paint(title.upper(), PAPER, bold=True)}"


def eyebrow(text: str) -> str:
    return paint(text.upper(), GOLD)


def statusline(*segments: str) -> str:
    return paint("  ·  ", MUTED_2).join(segments)


def kv(label: str, value: str, *, width: int = 24) -> str:
    dots = paint("." * max(1, width - len(label)), MUTED_2, dim=True)
    return f"  {paint(label, MUTED)} {dots} {value}"


def coverage_badge(ratio: float | None) -> str:
    """Visual cue only — interpretation belongs to the agent, not the UI."""
    if ratio is None:
        return f"{icon('warn')} {paint('n/a', MUTED)}"
    pct = f"{ratio * 100:.2f}%"
    if ratio >= 1.0:
        return f"{icon('ok')} {paint(pct, GREEN)}"
    if ratio >= 0.98:
        return f"{icon('warn')} {paint(pct, AMBER)}"
    return f"{icon('error')} {paint(pct, ROSE)}"
