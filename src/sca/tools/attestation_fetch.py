"""Tool: resolve, health-check, and download the latest attestation PDF.

Resilient to URL rot. The URL is chosen in this order:

  1. cached resolution  (data/attestation_cache.json) — HEAD-checked
  2. config seed        (latest_attestation_url)      — HEAD-checked
  3. the locator        — re-resolves from the transparency page

A URL that can neither be reused nor re-resolved raises AttestationUnavailable.
The pipeline then reports a clear gap — it never serves a stale or wrong
attestation silently.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import requests

from sca import config
from sca.tools.attestation_locator import (
    LocatorUnavailable,
    resolve_attestation_url,
)
from sca.tools.paxos_resolver import (
    PAXOS_TOKENS,
    resolve_paxos_attestation_url,
)

_CACHE = config.DATA_DIR / "attestation_cache.json"
# Attestations publish monthly — a cached resolution older than this is
# re-resolved so a freshly-published report is never missed.
_CACHE_MAX_AGE_DAYS = 25


class AttestationUnavailable(RuntimeError):
    """No attestation source could be resolved for the token."""


def _load_cache() -> dict:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    from sca.persist import atomic_write_json
    atomic_write_json(_CACHE, cache, sort_keys=False)


def _head_ok(url: str, timeout: float = 20.0) -> bool:
    try:
        resp = requests.head(
            url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _cache_fresh(entry: dict) -> bool:
    """A cached resolution is trusted only within the attestation cadence."""
    resolved_at = entry.get("resolved_at")
    if not resolved_at:
        return False
    try:
        when = date.fromisoformat(resolved_at)
    except ValueError:
        return False
    return (date.today() - when).days <= _CACHE_MAX_AGE_DAYS


def resolve_url(symbol: str, *, refresh: bool = False) -> dict:
    """Resolve a healthy attestation PDF URL for `symbol`.

    Returns {"url", "via"} — via is cache | seed | locator.
    Raises AttestationUnavailable if nothing resolves.
    """
    token = config.get_stablecoin(symbol)
    cache = _load_cache()

    # 1. cached resolution — only if recent (within the monthly cadence)
    if not refresh:
        entry = cache.get(symbol, {})
        cached = entry.get("url")
        if cached and _cache_fresh(entry) and _head_ok(cached):
            return {"url": cached, "via": "cache"}

    # 2. config seed
    seed = token.latest_attestation_url
    if seed and _head_ok(seed):
        cache[symbol] = {"url": seed, "via": "seed",
                         "resolved_at": date.today().isoformat()}
        _save_cache(cache)
        return {"url": seed, "via": "seed"}

    # 3. Paxos URL pattern probe — paxos.com transparency pages are JS-
    # rendered so the HTML locator can't see the link, but the WP CDN
    # publishes at a predictable shape we can probe directly. Try this
    # BEFORE the LLM-driven locator: it's cheap, deterministic, and avoids
    # spending an LLM call on a known-broken page.
    if symbol.upper() in PAXOS_TOKENS:
        paxos_url = resolve_paxos_attestation_url(symbol)
        if paxos_url and _head_ok(paxos_url):
            cache[symbol] = {
                "url": paxos_url,
                "via": "paxos_resolver",
                "resolved_at": date.today().isoformat(),
            }
            _save_cache(cache)
            return {"url": paxos_url, "via": "paxos_resolver"}

    # 4. locator — re-resolve from the durable transparency page
    if not token.transparency_url:
        raise AttestationUnavailable(
            f"{symbol}: no working attestation URL and no transparency_url "
            "to locate one from"
        )
    try:
        found = resolve_attestation_url(token.transparency_url, symbol=symbol)
    except (LocatorUnavailable, requests.RequestException) as exc:
        raise AttestationUnavailable(
            f"{symbol}: could not resolve an attestation URL — {exc}"
        ) from exc
    cache[symbol] = {
        "url": found["url"],
        "via": "locator",
        "resolved_at": date.today().isoformat(),
        "confidence": found["confidence"],
    }
    _save_cache(cache)
    return {"url": found["url"], "via": "locator"}


def fetch_latest_attestation(
    symbol: str, dest_dir: Path | None = None, *, refresh: bool = False
) -> dict:
    """Resolve and download the latest attestation PDF for `symbol`.

    Returns {"symbol", "source_url", "local_path", "via"}.
    """
    resolved = resolve_url(symbol, refresh=refresh)
    dest_dir = dest_dir or (config.DATA_DIR / "attestations")
    dest_dir.mkdir(parents=True, exist_ok=True)
    local = dest_dir / f"{symbol}-latest.pdf"
    resp = requests.get(
        resolved["url"], timeout=60, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    if resp.content[:5] != b"%PDF-":
        raise AttestationUnavailable(
            f"{symbol}: content at {resolved['url']} is not a PDF "
            f"(starts {resp.content[:12]!r}) — likely an error or login page"
        )
    local.write_bytes(resp.content)
    return {
        "symbol": symbol,
        "source_url": resolved["url"],
        "local_path": str(local),
        "via": resolved["via"],
    }
