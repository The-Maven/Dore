"""Tool: resolve, health-check, and download the latest attestation PDF.

Resilient to URL rot. The URL is chosen in this order:

  1. store override     (set by curators / web_discovery / locator)
                        — HEAD-checked; this is the production source of
                        truth that survives redeploys.
  2. cached resolution  (data/attestation_cache.json) — HEAD-checked
  3. config seed        (latest_attestation_url, YAML bootstrap)
                        — HEAD-checked
  4. paxos resolver     (PYUSD / USDP / USDG only)
  5. web discovery      (search-the-web for a fresh PDF)
  6. the locator        — re-resolves from the transparency page

A URL that can neither be reused nor re-resolved raises AttestationUnavailable.
The pipeline then reports a clear gap — it never serves a stale or wrong
attestation silently. Whichever path succeeds writes through to the store
so the next call short-circuits at step (1).
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


def _record_override(symbol: str, url: str, via: str) -> None:
    """Persist a discovered URL through the store so it survives a redeploy.

    Best-effort: a store failure (e.g. Supabase blip) must never break the
    resolution call site. The local cache still records the URL via
    `_save_cache` for the next call within this process.
    """
    try:
        from sca.store import get_store
        get_store().set_attestation_url_override(
            symbol, url, via=via, set_by=None,
        )
    except Exception as exc:  # noqa: BLE001
        from sca.observability import log_event
        log_event(
            "attestation.override_persist_failed", level="warn",
            symbol=symbol, via=via, error_class=type(exc).__name__,
            error_message=str(exc),
        )


def resolve_url(symbol: str, *, refresh: bool = False) -> dict:
    """Resolve a healthy attestation PDF URL for `symbol`.

    Returns {"url", "via"} — via is one of:
        store_override | cache | seed | paxos_resolver | web_search | locator.
    Raises AttestationUnavailable if nothing resolves.
    """
    token = config.get_stablecoin(symbol)
    cache = _load_cache()

    # 1. store override — the durable production source of truth. A curator
    # (or the discovery thread) sets this in Supabase / FileStore; it
    # survives redeploys, where the YAML seed alone would not.
    if not refresh:
        try:
            from sca.store import get_store
            override = get_store().get_attestation_url_override(symbol)
        except Exception:  # noqa: BLE001 - store hiccup falls through
            override = None
        if override:
            ovr_url = override.get("url", "")
            if ovr_url and _head_ok(ovr_url):
                return {"url": ovr_url, "via": "store_override"}

    # 2. cached resolution — only if recent (within the monthly cadence)
    if not refresh:
        entry = cache.get(symbol, {})
        cached = entry.get("url")
        if cached and _cache_fresh(entry) and _head_ok(cached):
            return {"url": cached, "via": "cache"}

    # 3. config seed (YAML bootstrap)
    seed = token.latest_attestation_url
    if seed and _head_ok(seed):
        cache[symbol] = {"url": seed, "via": "seed",
                         "resolved_at": date.today().isoformat()}
        _save_cache(cache)
        _record_override(symbol, seed, "seed")
        return {"url": seed, "via": "seed"}

    # 4. Paxos URL pattern probe — paxos.com transparency pages are JS-
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
            _record_override(symbol, paxos_url, "paxos_resolver")
            return {"url": paxos_url, "via": "paxos_resolver"}

    # 5. web discovery — typed web search for "[symbol] reserves attestation
    # [year] pdf". No-op unless SCA_WEB_SEARCH_PROVIDER is configured;
    # logs a one-shot "disabled" event when it isn't, so the Compendium
    # surfaces the gap rather than silently swallowing it.
    try:
        from sca.web_discovery import discover_attestation_url
        web_url = discover_attestation_url(symbol, token.issuer)
    except Exception as exc:  # noqa: BLE001 - discovery is best-effort
        from sca.observability import log_event
        log_event(
            "web_discovery.unexpected_error", level="warn",
            symbol=symbol, error_class=type(exc).__name__,
            error_message=str(exc),
        )
        web_url = None
    if web_url and _head_ok(web_url):
        cache[symbol] = {
            "url": web_url,
            "via": "web_search",
            "resolved_at": date.today().isoformat(),
        }
        _save_cache(cache)
        _record_override(symbol, web_url, "web_search")
        return {"url": web_url, "via": "web_search"}

    # 6. locator — re-resolve from the durable transparency page
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
    _record_override(symbol, found["url"], "locator")
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
