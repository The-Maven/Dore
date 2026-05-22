"""Deterministic tool: on-chain token supply.

LAYER: facts. No LLM. Reads token supply directly from every chain a token
is deployed on — concurrently — and sums it.

Chain kinds are pluggable: each kind (evm / solana / tron) has a reader in
the registry. Adding a chain kind = adding one reader. Speed: per-deployment
reads run in parallel; results are TTL-cached for 60s.

A wrong contract address silently returns a wrong number, so unverified
addresses are skipped unless allow_unverified=True — and even then flagged.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from sca import config
from sca.cache import TTLCache
from sca.config import Chain, Deployment
from sca.models import ChainSupply, SupplyResult

# ERC-20 selectors (also referenced by the test suite's RPC mock)
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_DECIMALS = "0x313ce567"
# A valid Tron address used as the caller for read-only constant calls.
_TRON_OWNER = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"

_cache = TTLCache(ttl=60.0)


def _json_rpc(url: str, method: str, params: list):
    resp = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body.get("result")


# ── readers — one per chain kind ──────────────────────────────────────
def _chain_supply(dep: Deployment, raw: int, decimals: int) -> ChainSupply:
    """Build a ChainSupply, carrying the deployment's provenance."""
    return ChainSupply(
        chain=dep.chain,
        contract=dep.contract,
        raw=raw,
        decimals=decimals,
        supply=raw / (10 ** decimals),
        kind=dep.kind,
        verified=dep.verified,
    )


def _read_evm(chain: Chain, dep: Deployment) -> ChainSupply:
    def call(selector: str) -> int:
        result = _json_rpc(
            chain.rpc, "eth_call",
            [{"to": dep.contract, "data": selector}, "latest"],
        )
        if result in (None, "0x", ""):
            raise RuntimeError("empty result (wrong address or chain?)")
        return int(result, 16)

    raw = call(SEL_TOTAL_SUPPLY)
    decimals = call(SEL_DECIMALS)
    return _chain_supply(dep, raw, decimals)


def _read_solana(chain: Chain, dep: Deployment) -> ChainSupply:
    result = _json_rpc(chain.rpc, "getTokenSupply", [dep.contract])
    value = (result or {}).get("value")
    if not value:
        raise RuntimeError("getTokenSupply returned no value")
    raw, decimals = int(value["amount"]), int(value["decimals"])
    return _chain_supply(dep, raw, decimals)


def _tron_constant(chain: Chain, contract: str, selector: str) -> int:
    resp = requests.post(
        f"{chain.rpc}/wallet/triggerconstantcontract",
        json={
            "owner_address": _TRON_OWNER,
            "contract_address": contract,
            "function_selector": selector,
            "visible": True,
        },
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    results = body.get("constant_result")
    if not results:
        raise RuntimeError(f"tron call failed: {body.get('result', body)}")
    return int(results[0], 16)


def _read_tron(chain: Chain, dep: Deployment) -> ChainSupply:
    raw = _tron_constant(chain, dep.contract, "totalSupply()")
    decimals = _tron_constant(chain, dep.contract, "decimals()")
    return _chain_supply(dep, raw, decimals)


_READERS = {"evm": _read_evm, "solana": _read_solana, "tron": _read_tron}


def _compute(symbol: str, allow_unverified: bool) -> SupplyResult:
    token = config.get_stablecoin(symbol)
    chains = config.chains()
    warnings: list[str] = []
    jobs: list[tuple] = []

    for dep in token.deployments:
        if not dep.verified:
            if not allow_unverified:
                warnings.append(
                    f"{symbol}/{dep.chain}: contract address unverified — "
                    "skipped. Verify it (sca curate) or allow_unverified=True."
                )
                continue
            warnings.append(
                f"{symbol}/{dep.chain}: contract address UNVERIFIED but "
                "included — verify before trusting this figure."
            )
        chain = chains.get(dep.chain)
        if chain is None:
            warnings.append(f"{symbol}/{dep.chain}: chain not configured")
            continue
        reader = _READERS.get(chain.kind)
        if reader is None:
            warnings.append(
                f"{symbol}/{dep.chain}: no reader for chain kind "
                f"'{chain.kind}'"
            )
            continue
        jobs.append((chain, dep, reader))

    per_chain: list[ChainSupply] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = {
                pool.submit(reader, chain, dep): dep
                for chain, dep, reader in jobs
            }
            for fut in as_completed(futures):
                dep = futures[fut]
                try:
                    per_chain.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - report, don't swallow
                    warnings.append(f"{symbol}/{dep.chain}: {exc}")

    per_chain.sort(key=lambda c: c.chain)
    native = sum(c.supply for c in per_chain if c.kind == "native")
    bridged = sum(c.supply for c in per_chain if c.kind == "bridged")
    return SupplyResult(
        symbol=symbol,
        # Headline figure is native issuance only — bridged copies are
        # collateralised by locked native supply, so summing both double-counts.
        total_supply=native,
        per_chain=per_chain,
        warnings=warnings,
        native_supply=native,
        bridged_supply=bridged,
        read_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def get_onchain_supply(
    symbol: str, *, allow_unverified: bool = False, use_cache: bool = True
) -> SupplyResult:
    """Total on-chain supply for `symbol`, summed across deployments."""
    if not use_cache:
        return _compute(symbol, allow_unverified)
    return _cache.get_or_set(
        (symbol, allow_unverified), lambda: _compute(symbol, allow_unverified)
    )
