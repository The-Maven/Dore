"""Deterministic tool: on-chain token supply.

LAYER: facts. No LLM. Reads token supply directly from every chain a token
is deployed on — concurrently — and sums it.

Chain kinds are pluggable: each kind (evm / solana / tron) has a reader in
the registry. Adding a chain kind = adding one reader. Speed: per-deployment
reads run in parallel; results are TTL-cached for 60s.

Multi-RPC reliability: every chain carries a pool of endpoints. Supply
reads (the highest-stakes figure we publish) are corroborated against a
second endpoint when one is available — disagreement triggers a tertiary
check and a loud structured log event. A wrong "fully backed" judgement
from a misbehaving RPC is the worst class of bug we can ship; cross-
validation is the integrity gate that prevents it.

A wrong contract address silently returns a wrong number, so unverified
addresses are skipped unless allow_unverified=True — and even then flagged.
"""
from __future__ import annotations

import operator
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

import requests

from sca import config
from sca.cache import TTLCache
from sca.config import Chain, Deployment
from sca.models import ChainSupply, SupplyResult
from sca.observability import log_event, timed

# ERC-20 selectors (also referenced by the test suite's RPC mock)
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_DECIMALS = "0x313ce567"
# A valid Tron address used as the caller for read-only constant calls.
_TRON_OWNER = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"

_cache = TTLCache(ttl=60.0)


def _tron_headers() -> dict:
    """Honour an optional TronGrid API key for higher rate limits."""
    key = os.environ.get("TRON_PRO_API_KEY", "").strip()
    return {"TRON-PRO-API-KEY": key} if key else {}


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


# ── RPC pool: fallback + cross-validation ─────────────────────────────
T = TypeVar("T")


def _pool_call(
    chain: Chain,
    make_request: Callable[[str], T],
    *,
    cross_check: bool = False,
    label: str = "",
    agree: Callable[[T, T], bool] = operator.eq,
) -> tuple[T, str, str]:
    """Call `make_request(rpc_url)` across the chain's RPC pool.

    cross_check=False (default): sequential — first success wins, rotate
    on failure. Use for immutable reads (decimals, symbol) where one
    honest endpoint is enough.

    cross_check=True: corroborated — call primary + first fallback in
    parallel, require agreement. On disagreement, call tertiary and
    majority-vote. Use for high-stakes reads (totalSupply) where a single
    misbehaving endpoint must not poison the result.

    Returns `(value, endpoint_used, consensus_label)`.
        consensus_label is one of:
          "1/1 single source"     — only one endpoint configured
          "primary"               — primary succeeded (sequential mode)
          "fallback k/N"          — kth fallback used (sequential mode)
          "2/2 agree"             — primary + secondary returned same value
          "1/2 single source"     — fallback unreachable; primary alone
          "2/3 majority"          — tertiary broke a 1-vs-1 tie
          "DISAGREEMENT"          — no majority; primary returned, but loud

    Raises the last exception if every endpoint failed.
    """
    rpcs = list(chain.rpcs)
    if not rpcs:
        raise RuntimeError(f"chain '{chain.name}' has no RPC endpoints")

    if not cross_check or len(rpcs) == 1:
        return _try_sequential(rpcs, chain.name, make_request, label=label)
    return _try_corroborated(rpcs, chain.name, make_request, label=label,
                              agree=agree)


def _try_sequential(
    rpcs: list[str], chain_name: str, make_request: Callable[[str], T],
    *, label: str,
) -> tuple[T, str, str]:
    """First-success-wins. Fast, no corroboration."""
    last_exc: Exception | None = None
    for idx, rpc in enumerate(rpcs):
        try:
            with timed("rpc.call", chain=chain_name, endpoint=rpc, label=label,
                       mode="sequential") as ev:
                value = make_request(rpc)
                ev["ok"] = True
            consensus = (
                "1/1 single source" if len(rpcs) == 1
                else ("primary" if idx == 0 else f"fallback {idx}/{len(rpcs) - 1}")
            )
            return value, rpc, consensus
        except Exception as exc:  # noqa: BLE001 - log + rotate
            last_exc = exc
            log_event("rpc.call.failed", level="warn",
                      chain=chain_name, endpoint=rpc, label=label,
                      attempt=idx + 1, of=len(rpcs),
                      error_class=type(exc).__name__,
                      error_message=str(exc))
    assert last_exc is not None
    raise last_exc


def _try_corroborated(
    rpcs: list[str], chain_name: str, make_request: Callable[[str], T],
    *, label: str, agree: Callable[[T, T], bool],
) -> tuple[T, str, str]:
    """Call primary + first fallback concurrently. Require agreement;
    fall back to a tertiary majority vote on disagreement."""
    primary, secondary, *rest = rpcs + [""] * (2 - len(rpcs))
    pair = [u for u in (primary, secondary) if u]

    results: dict[str, T] = {}
    errors: dict[str, Exception] = {}
    with ThreadPoolExecutor(max_workers=len(pair)) as pool:
        futs = {pool.submit(_timed_request, u, chain_name, label,
                            make_request): u for u in pair}
        for fut in as_completed(futs):
            url = futs[fut]
            try:
                results[url] = fut.result()
            except Exception as exc:  # noqa: BLE001 - log + degrade
                errors[url] = exc

    if not results:
        # All endpoints failed — raise the first error
        first = next(iter(errors.values()))
        log_event("rpc.pool.exhausted", level="error",
                  chain=chain_name, label=label,
                  endpoints=list(errors.keys()))
        raise first

    if len(results) == 1:
        url, value = next(iter(results.items()))
        log_event("rpc.consensus.degraded", level="warn",
                  chain=chain_name, label=label,
                  endpoint=url, failed=list(errors.keys()))
        return value, url, "1/2 single source"

    # Both succeeded — check agreement
    values = list(results.items())
    if agree(values[0][1], values[1][1]):
        return values[0][1], primary, "2/2 agree"

    # Disagreement — escalate to tertiary
    log_event("rpc.consensus.disagreement", level="error",
              chain=chain_name, label=label,
              results={u: str(v) for u, v in results.items()})

    tertiary = next((u for u in rest if u), "")
    if not tertiary:
        return values[0][1], primary, "DISAGREEMENT (no tertiary)"

    try:
        third = _timed_request(tertiary, chain_name, label, make_request)
    except Exception as exc:  # noqa: BLE001 - log + return primary
        log_event("rpc.call.failed", level="warn",
                  chain=chain_name, endpoint=tertiary, label=label,
                  error_class=type(exc).__name__, error_message=str(exc))
        return values[0][1], primary, "DISAGREEMENT (tertiary failed)"

    all_results = {**results, tertiary: third}
    # Bucket values by equality (using `agree` semantics rather than ==)
    buckets: list[tuple[T, list[str]]] = []
    for url, v in all_results.items():
        for bucket_value, urls in buckets:
            if agree(v, bucket_value):
                urls.append(url)
                break
        else:
            buckets.append((v, [url]))
    buckets.sort(key=lambda b: -len(b[1]))
    top_value, top_urls = buckets[0]
    if len(top_urls) >= 2:
        log_event("rpc.consensus.majority", level="warn",
                  chain=chain_name, label=label,
                  agreed=top_urls,
                  outliers=[u for u in all_results if u not in top_urls])
        return top_value, top_urls[0], "2/3 majority"

    return values[0][1], primary, "DISAGREEMENT (no majority)"


def _timed_request(
    url: str, chain_name: str, label: str,
    make_request: Callable[[str], T],
) -> T:
    """Tiny wrapper so the parallel path also gets a timed log event."""
    with timed("rpc.call", chain=chain_name, endpoint=url, label=label,
               mode="corroborated") as ev:
        v = make_request(url)
        ev["ok"] = True
        return v


# ── readers — one per chain kind ──────────────────────────────────────
def _chain_supply(
    dep: Deployment, raw: int, decimals: int,
    *, consensus: str = "", endpoint: str = "",
) -> ChainSupply:
    """Build a ChainSupply, carrying the deployment's provenance + the
    multi-RPC reliability metadata (which endpoint, whether endpoints agreed)."""
    return ChainSupply(
        chain=dep.chain,
        contract=dep.contract,
        raw=raw,
        decimals=decimals,
        supply=raw / (10 ** decimals),
        kind=dep.kind,
        verified=dep.verified,
        verification_method=dep.verification_method,
        consensus=consensus,
        endpoint=endpoint,
    )


def _read_evm(chain: Chain, dep: Deployment) -> ChainSupply:
    # totalSupply: high stakes — corroborate across endpoints.
    # decimals: immutable — single endpoint is sufficient and saves a round trip.
    def supply_call(url: str) -> int:
        r = _json_rpc(url, "eth_call",
                      [{"to": dep.contract, "data": SEL_TOTAL_SUPPLY}, "latest"])
        if r in (None, "0x", ""):
            raise RuntimeError("empty result (wrong address or chain?)")
        return int(r, 16)

    def decimals_call(url: str) -> int:
        r = _json_rpc(url, "eth_call",
                      [{"to": dep.contract, "data": SEL_DECIMALS}, "latest"])
        if r in (None, "0x", ""):
            raise RuntimeError("empty result (wrong address or chain?)")
        return int(r, 16)

    raw, endpoint, consensus = _pool_call(
        chain, supply_call, cross_check=True,
        label=f"{dep.chain}:{dep.contract[:10]}:totalSupply",
    )
    decimals, _, _ = _pool_call(
        chain, decimals_call, cross_check=False,
        label=f"{dep.chain}:{dep.contract[:10]}:decimals",
    )
    return _chain_supply(dep, raw, decimals, consensus=consensus, endpoint=endpoint)


def _read_solana(chain: Chain, dep: Deployment) -> ChainSupply:
    # Solana returns both amount + decimals in one call. Corroborate the
    # amount; decimals naturally rides along. We compare only the amount.
    def call(url: str) -> dict:
        r = _json_rpc(url, "getTokenSupply", [dep.contract])
        value = (r or {}).get("value")
        if not value:
            raise RuntimeError("getTokenSupply returned no value")
        return value

    value, endpoint, consensus = _pool_call(
        chain, call, cross_check=True,
        label=f"{dep.chain}:{dep.contract[:10]}:tokenSupply",
        agree=lambda a, b: a.get("amount") == b.get("amount"),
    )
    raw = int(value["amount"])
    decimals = int(value["decimals"])
    return _chain_supply(dep, raw, decimals, consensus=consensus, endpoint=endpoint)


def _tron_call_at(
    rpc_url: str, contract: str, selector: str,
) -> str:
    """Raw hex of a TRC20 constant call against a specific RPC endpoint.

    TronGrid throttles aggressively on the public tier — back off and
    retry on 429s. Set TRON_PRO_API_KEY in .env to lift the limit.
    Returns the full ABI return hex (preserves variable-length payloads).
    """
    payload = {
        "owner_address": _TRON_OWNER,
        "contract_address": contract,
        "function_selector": selector,
        "visible": True,
    }
    url = f"{rpc_url}/wallet/triggerconstantcontract"
    headers = _tron_headers()
    backoff = 1.0
    for attempt in range(4):
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code == 429 and attempt < 3:
            time.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        body = resp.json()
        results = body.get("constant_result")
        if not results:
            raise RuntimeError(f"tron call failed: {body.get('result', body)}")
        return "0x" + results[0]
    resp.raise_for_status()
    return "0x"  # unreachable; raise_for_status above will throw


def _tron_constant_hex(
    chain: Chain, contract: str, selector: str,
) -> str:
    """Raw hex via the primary Tron endpoint — for symbol() (verifier path)."""
    return _tron_call_at(chain.rpc, contract, selector)


def _tron_constant(chain: Chain, contract: str, selector: str) -> int:
    """Numeric TRC20 read for fixed-size returns (`uint256`, `uint8`)."""
    return int(_tron_constant_hex(chain, contract, selector), 16)


def _read_tron(chain: Chain, dep: Deployment) -> ChainSupply:
    def supply_call(url: str) -> int:
        return int(_tron_call_at(url, dep.contract, "totalSupply()"), 16)

    def decimals_call(url: str) -> int:
        return int(_tron_call_at(url, dep.contract, "decimals()"), 16)

    raw, endpoint, consensus = _pool_call(
        chain, supply_call, cross_check=True,
        label=f"{dep.chain}:{dep.contract[:10]}:totalSupply",
    )
    decimals, _, _ = _pool_call(
        chain, decimals_call, cross_check=False,
        label=f"{dep.chain}:{dep.contract[:10]}:decimals",
    )
    return _chain_supply(dep, raw, decimals, consensus=consensus, endpoint=endpoint)


_READERS = {"evm": _read_evm, "solana": _read_solana, "tron": _read_tron}


def _compute(symbol: str, allow_unverified: bool) -> SupplyResult:
    token = config.get_stablecoin(symbol)
    chains = config.chains()
    warnings: list[str] = []
    jobs: list[tuple] = []

    for dep in token.deployments:
        if not dep.verified:
            # Tech detail (slug + reason) goes to logs for ops; the user
            # gets plain English in the gap list.
            log_event(
                "supply.unverified_deployment", level="info",
                symbol=symbol, chain=dep.chain, contract=dep.contract,
                included=allow_unverified,
            )
            if not allow_unverified:
                warnings.append(
                    f"{symbol} on {dep.chain}: the contract address for "
                    "this deployment has not yet passed an on-chain identity "
                    "check, so this chain is excluded from the headline "
                    "total. A maintainer will confirm the address on the next "
                    "verification pass."
                )
                continue
            warnings.append(
                f"{symbol} on {dep.chain}: the contract address has not yet "
                "passed an on-chain identity check. The figure is included "
                "but should be treated as provisional pending verification."
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
    failed_chains: list[str] = []
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
                    failed_chains.append(dep.chain)

    per_chain.sort(key=lambda c: c.chain)

    # Sanity gate: compare each chain's reading against persisted history.
    # A jump beyond [0.5x, 2x] is implausible for a stablecoin and may
    # signal a misbehaving RPC, wrong contract, or hijack. We warn but
    # still return the value — the human reviewer decides.
    from sca import supply_history

    for cs in per_chain:
        is_jump, prior = supply_history.check_jump(symbol, cs.chain, cs.supply)
        if is_jump and prior:
            warnings.append(
                f"{symbol}/{cs.chain}: supply JUMP — prior {prior:,.0f}, "
                f"now {cs.supply:,.0f} ({cs.supply / prior:.2f}x). "
                "Verify before trusting."
            )
        # Record the new reading regardless — the next call needs a baseline.
        supply_history.record(symbol, cs.chain, cs.supply)

    native = sum(c.supply for c in per_chain if c.kind == "native")
    bridged = sum(c.supply for c in per_chain if c.kind == "bridged")

    # Integrity gate: if any expected chain failed, the headline figure is
    # PARTIAL — never let the UI render a partial total as authoritative.
    chains_expected = len(jobs)
    chains_read = len(per_chain)
    complete = chains_read == chains_expected and chains_expected > 0
    if not complete and failed_chains:
        warnings.append(
            f"{symbol}: PARTIAL TOTAL — {chains_read}/{chains_expected} chains "
            f"read. Failed: {', '.join(sorted(failed_chains))}. The headline "
            f"figure understates true circulation."
        )
        log_event("supply.partial", level="warn",
                  symbol=symbol, chains_read=chains_read,
                  chains_expected=chains_expected,
                  failed_chains=failed_chains)

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
        complete=complete,
        chains_expected=chains_expected,
        chains_read=chains_read,
        failed_chains=failed_chains,
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
