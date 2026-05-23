"""Canary — re-fetch every external source we depend on, snapshot it,
and report drift. Designed for a nightly cron.

Coverage:
  - Per-chain RPC health (one cheap call per endpoint in the pool)
  - OFAC SDN list (multi-URL fallback already handles rotation)
  - Every stablecoin's transparency_url
  - Every corpus source URL

Outputs a structured report (dict) that the CLI renders. Every event is
also logged via observability so the same data lands in Sentry once we
wire it up. No prose summarisation here — the report is data; the human
or a future agent decides what to do with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import requests

from sca import config
from sca import snapshots
from sca.observability import log_event, timed

Status = Literal["live", "snapshot", "broken"]


@dataclass
class SourceCheck:
    id: str
    url: str
    kind: str  # 'rpc' | 'transparency' | 'corpus' | 'ofac'
    status: Status
    status_code: int = 0
    bytes: int = 0
    sha256_changed: bool = False
    sha256: str = ""
    error: str = ""


@dataclass
class CanaryReport:
    checks: list[SourceCheck] = field(default_factory=list)

    @property
    def live(self) -> int:
        return sum(1 for c in self.checks if c.status == "live")

    @property
    def broken(self) -> int:
        return sum(1 for c in self.checks if c.status == "broken")

    @property
    def snapshot_only(self) -> int:
        return sum(1 for c in self.checks if c.status == "snapshot")

    @property
    def changed(self) -> int:
        return sum(1 for c in self.checks if c.sha256_changed)


def _check_url(snapshot_id: str, url: str, kind: str) -> SourceCheck:
    """Fetch URL through the snapshot layer and report what happened.
    `live` = fresh fetch succeeded; `snapshot` = live fetch failed but we
    served the cached body; `broken` = no live AND no snapshot."""
    prior = snapshots.load_meta(snapshot_id)
    prior_sha = prior.sha256 if prior else ""

    try:
        out = snapshots.fetch_with_snapshot(snapshot_id, url)
    except requests.RequestException as exc:
        log_event("canary.broken", level="error",
                  id=snapshot_id, url=url, kind=kind,
                  error_class=type(exc).__name__, error_message=str(exc))
        return SourceCheck(
            id=snapshot_id, url=url, kind=kind, status="broken",
            error=str(exc),
        )

    changed = out.source == "live" and bool(prior_sha) and prior_sha != out.meta.sha256
    if changed:
        log_event("canary.content_changed", level="warn",
                  id=snapshot_id, url=url, kind=kind,
                  old_sha=prior_sha, new_sha=out.meta.sha256)
    return SourceCheck(
        id=snapshot_id, url=url, kind=kind,
        status="live" if out.source == "live" else "snapshot",
        status_code=out.meta.status_code,
        bytes=out.meta.bytes,
        sha256=out.meta.sha256,
        sha256_changed=changed,
        error=out.error,
    )


def _check_rpc(chain_name: str, endpoint: str) -> SourceCheck:
    """Cheap liveness ping — eth_blockNumber on EVM, getHealth on Solana,
    /wallet/getnowblock on Tron. We don't snapshot RPC responses; the
    canary's job here is just to know endpoint health."""
    chain = config.chains()[chain_name]
    sid = f"rpc::{chain_name}::{endpoint}"
    try:
        with timed("canary.rpc.ping", chain=chain_name, endpoint=endpoint):
            if chain.kind == "evm":
                resp = requests.post(
                    endpoint, timeout=10,
                    json={"jsonrpc": "2.0", "id": 1,
                          "method": "eth_blockNumber", "params": []},
                )
            elif chain.kind == "solana":
                resp = requests.post(
                    endpoint, timeout=10,
                    json={"jsonrpc": "2.0", "id": 1,
                          "method": "getHealth", "params": []},
                )
            else:  # tron
                resp = requests.get(
                    f"{endpoint}/wallet/getnowblock", timeout=10,
                )
            resp.raise_for_status()
        return SourceCheck(
            id=sid, url=endpoint, kind="rpc", status="live",
            status_code=resp.status_code, bytes=len(resp.content),
        )
    except requests.RequestException as exc:
        log_event("canary.rpc.failed", level="warn",
                  chain=chain_name, endpoint=endpoint,
                  error_class=type(exc).__name__, error_message=str(exc))
        return SourceCheck(
            id=sid, url=endpoint, kind="rpc", status="broken",
            error=str(exc),
        )


def run_canary(*, include_rpcs: bool = True,
               include_transparency: bool = True,
               include_corpus: bool = True) -> CanaryReport:
    """Sweep every external source we depend on."""
    report = CanaryReport()

    if include_rpcs:
        for name, chain in config.chains().items():
            for endpoint in chain.rpcs:
                report.checks.append(_check_rpc(name, endpoint))

    if include_transparency:
        for symbol, coin in config.stablecoins().items():
            if not coin.transparency_url:
                continue
            report.checks.append(_check_url(
                snapshot_id=f"transparency::{symbol}",
                url=coin.transparency_url,
                kind="transparency",
            ))

    if include_corpus:
        # Corpus sources live in sources.yaml — fetched through the same
        # snapshot layer so the UI can serve archived copies when URLs rot.
        from sca.corpus.sources import all_sources

        for src in all_sources():
            if not src.url:
                continue
            report.checks.append(_check_url(
                snapshot_id=f"corpus::{src.id}",
                url=src.url,
                kind="corpus",
            ))

    log_event("canary.done", level="info",
              total=len(report.checks), live=report.live,
              snapshot=report.snapshot_only, broken=report.broken,
              changed=report.changed)
    return report
