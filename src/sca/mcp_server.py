"""Doré MCP server — the agent's guardrailed window onto the system.

This server is the ONLY surface the Hermes agent is given. It is a deliberate
security boundary, not a convenience:

  - Read-only. The agent can run analyses, screen sanctions, read on-chain
    supply, search the corpus and read analysis history. It CANNOT write
    database tables, run SQL, read secrets, or reach the filesystem — none of
    that is exposed here, so none of it is reachable.
  - The human curation gate holds. There is no tool to approve a corpus
    source or verify an address; curation stays human.
  - Every tool runs Doré's existing deterministic, guardrailed pipeline, so
    the agent receives cited facts and structured guardrail results — never
    raw model opinion dressed as data.
  - Every call is audit-logged.

The analysis tools append a row to the analysis history (via Doré's own
pipeline, exactly as a human run would) — that is append-only provenance,
not the agent manipulating data.

Hermes connects to this over stdio — see agent/config.yaml -> mcp_servers.
Run standalone: `python -m sca.mcp_server` (or the `dore-mcp` console script).
"""
from __future__ import annotations

import dataclasses
import logging

from mcp.server.fastmcp import FastMCP

from sca import config
from sca.agent import analyze, assess_redemption, screen_token
from sca.corpus import retrieve
from sca.corpus.sources import all_sources
from sca.store import get_store
from sca.tools import get_onchain_supply

_log = logging.getLogger("dore.mcp")

mcp = FastMCP("dore")


def _audit(tool: str, **fields: object) -> None:
    """Audit-log an agent tool call — it is a compliance tool; calls matter."""
    _log.info("agent tool call · %s · %s", tool, fields)


def _plain(obj: object) -> object:
    """Serialise a dataclass result to a plain dict for the wire."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


@mcp.tool()
def list_stablecoins() -> list[dict]:
    """List every stablecoin Doré tracks — symbol, name, issuer and chains."""
    _audit("list_stablecoins")
    return [
        {
            "symbol": symbol,
            "name": coin.name,
            "issuer": coin.issuer,
            "chains": [d.chain for d in coin.deployments],
        }
        for symbol, coin in config.stablecoins().items()
    ]


@mcp.tool()
def get_supply(symbol: str) -> dict:
    """Read a stablecoin's live on-chain supply across every chain it is
    deployed on. Deterministic — a direct read from chain RPCs."""
    _audit("get_supply", symbol=symbol)
    return _plain(get_onchain_supply(symbol.upper(), allow_unverified=True))


@mcp.tool()
def run_attestation_analysis(symbol: str) -> dict:
    """Run Doré's full attestation analysis for a stablecoin: the issuer's
    reserve attestation reconciled against live on-chain supply, with
    deterministic guardrail checks, structured gaps and a cited narrative.
    Returns the complete analysis."""
    _audit("run_attestation_analysis", symbol=symbol)
    return _plain(analyze(symbol.upper()))


@mcp.tool()
def run_sanctions_screen(symbol: str) -> dict:
    """Screen a stablecoin's contract addresses against the official OFAC SDN
    list. Returns the screen result, guardrails and a cited narrative."""
    _audit("run_sanctions_screen", symbol=symbol)
    return _plain(screen_token(symbol.upper()))


@mcp.tool()
def run_redemption_assessment(symbol: str) -> dict:
    """Assess a stablecoin's redemption capacity — liquid coverage, the
    reserve-liquidity tiers, net redemption flow — with guardrails and a
    cited narrative."""
    _audit("run_redemption_assessment", symbol=symbol)
    return _plain(assess_redemption(symbol.upper()))


@mcp.tool()
def search_corpus(query: str) -> list[dict]:
    """Search Doré's regulatory corpus — the reasoning frame. Returns
    passages from every included source; a source a human has explicitly
    excluded is omitted. Use this to ground a judgement in cited regulation.
    Each passage carries `source_verified` — True when a human has
    explicitly reviewed the source (a quality signal, not a gate)."""
    _audit("search_corpus", query=query)
    return [_plain(p) for p in retrieve(query)]


@mcp.tool()
def list_corpus_sources() -> list[dict]:
    """List the corpus source registry — each source's id, title, tier,
    status (`included` by default / `excluded` if a human opted it out),
    `verified` (human-reviewed signal) and summary. Every included source
    is citable."""
    _audit("list_corpus_sources")
    return [
        {
            "id": s.id,
            "title": s.title,
            "tier": s.tier,
            "status": s.status,
            "verified": s.verified,
            "summary": s.summary,
        }
        for s in all_sources()
    ]


@mcp.tool()
def get_analysis_history(symbol: str = "", limit: int = 20) -> list[dict]:
    """Read recent analyses recorded in the durable store, newest first —
    optionally filtered to a single stablecoin symbol."""
    _audit("get_analysis_history", symbol=symbol, limit=limit)
    return get_store().list_analyses(
        symbol=symbol.upper() or None, limit=limit
    )


def main() -> None:
    """Entry point — runs the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
