"""Refresh peg ticks for a list of symbols at the start of each cycle.

Thin wrapper over sca.peg_price.fetch_consensus so the ticker stays
clean. Each call goes through the multi-source orchestrator
(audit #7) so the persisted row carries consensus_kind + per-source
audit trail. Hermetic-friendly: when SCA_PEG_PRICE_DISABLED is set,
fetch_consensus returns None and we skip persistence silently.
"""
from __future__ import annotations

from sca.observability import log_event
from sca.peg_price import fetch_consensus, persist_consensus


def refresh_for_symbols(symbols: list[str]) -> dict:
    """Fetch + persist one consensus tick per symbol. Returns a
    summary with per-source-kind counts for observability so an
    operator can see how often sources agreed vs disputed.

    Best-effort: an upstream failure on one symbol never stops the
    others. Bricked individual sources degrade to single-source
    consensus; total source failure (no source responded) skips."""
    summary = {
        "requested": len(symbols),
        "wrote": 0, "skipped": 0,
        "consensus_kinds": {"agreed": 0, "single": 0, "disputed": 0},
    }
    for sym in symbols:
        tick = fetch_consensus(sym)
        if tick is None:
            summary["skipped"] += 1
            continue
        try:
            persist_consensus(tick)
            summary["wrote"] += 1
            ck = tick.consensus_kind
            if ck in summary["consensus_kinds"]:
                summary["consensus_kinds"][ck] += 1
            if ck == "disputed":
                log_event(
                    "movement.peg_tick.dispute_persisted", level="warn",
                    symbol=sym,
                    max_disagreement_bps=round(tick.max_disagreement_bps, 2),
                    source_count=len(tick.sources),
                )
        except Exception as exc:  # noqa: BLE001
            log_event(
                "movement.peg_tick.persist_failed", level="warn",
                symbol=sym, error_class=type(exc).__name__,
            )
    return summary
