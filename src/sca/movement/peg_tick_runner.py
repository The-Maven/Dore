"""Refresh peg ticks for a list of symbols at the start of each cycle.

Thin wrapper over sca.peg_price so the ticker stays clean. The peg
ticks accumulate into the store and become the input for the next
forecast tick. Hermetic-friendly: when SCA_PEG_PRICE_DISABLED is set,
fetch_price returns None and we skip persistence silently.
"""
from __future__ import annotations

from sca.observability import log_event
from sca.peg_price import fetch_price, persist_tick


def refresh_for_symbols(symbols: list[str]) -> dict:
    """Fetch + persist one tick per symbol. Returns a summary dict
    for observability. Best-effort: an upstream failure on one symbol
    never stops the others."""
    summary = {"requested": len(symbols), "wrote": 0, "skipped": 0}
    for sym in symbols:
        tick = fetch_price(sym)
        if tick is None:
            summary["skipped"] += 1
            continue
        try:
            persist_tick(tick)
            summary["wrote"] += 1
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.peg_tick.persist_failed", level="warn",
                symbol=sym, error_class=type(exc).__name__,
            )
    return summary
