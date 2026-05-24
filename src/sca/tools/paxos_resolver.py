"""Best-effort Paxos attestation URL resolver.

Paxos's transparency pages (paxos.com/{pyusd,usdp,usdg}-transparency) are
JavaScript-rendered, so the generic HTML/Gatsby/Next.js harvesters in
`attestation_locator` see nothing useful and the pipeline degrades to the
"visit the transparency page and download manually" gap.

We've observed in production logs that Paxos publishes attestation PDFs at
a predictable URL shape on their WordPress CDN:

    https://www.paxos.com/wp-content/uploads/{YYYY}/{MM}/
        {TOKEN}-Attestation-Report-{MonthName}-{YYYY}.pdf

where:
  - {YYYY}/{MM}   = publication year/month (the WP upload bucket)
  - {MonthName}   = ATTESTATION period month name (typically MM minus 1)
  - {YYYY}        = ATTESTATION period year (rolls back at January)
  - {TOKEN}       = the token symbol exactly: PYUSD | USDP | USDG

So the April-2026 attestation is published in May 2026 and lives at:

    https://www.paxos.com/wp-content/uploads/2026/05/
        PYUSD-Attestation-Report-April-2026.pdf

This module probes the last 6 plausible publication months via cheap HEAD
requests (falling back to a GET-Range probe if Paxos's WP doesn't honour
HEAD). First 200 wins. Failure returns None — it's a best-effort hint, the
caller still has the LLM-driven locator and the seed URL to fall through to.

Resolved URLs are cached at data/paxos_resolved.json keyed by symbol so a
warm pipeline doesn't re-probe on every refresh.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import requests

from sca import config
from sca.observability import log_event

# Easy to extend: add a Paxos-issued token here and the resolver picks it up.
PAXOS_TOKENS: set[str] = {"PYUSD", "USDP", "USDG"}

# How many plausible publication months to probe (current month + 5 prior).
# Paxos publishes monthly within ~30 days of period end, so a 6-month window
# covers cold-start, a brief publication delay, and the "they skipped a
# month and we're catching up" case without ever being noisy.
_LOOKBACK_MONTHS = 6

# Cache: {symbol: {"url": str, "attestation_month": "YYYY-MM",
#                  "resolved_at": "YYYY-MM-DD"}}
_CACHE_PATH = config.DATA_DIR / "paxos_resolved.json"

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

_BASE = "https://www.paxos.com/wp-content/uploads"
_UA = {"User-Agent": "Mozilla/5.0"}
_HTTP_TIMEOUT = 10.0


# ── cache ─────────────────────────────────────────────────────────────
def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        from sca.persist import atomic_write_json
        atomic_write_json(_CACHE_PATH, cache)
    except OSError:
        # Caching is an optimisation — never break the caller for a disk hiccup.
        pass


def _cache_entry_current(entry: dict, today: date) -> bool:
    """A cached resolution is good for the current calendar month.

    Once the calendar rolls into a new month, the cached URL may still work,
    but a newer attestation has probably been published — re-probe so the
    pipeline picks it up instead of pinning to last month's report forever.
    """
    resolved_at = entry.get("resolved_at", "")
    try:
        when = date.fromisoformat(resolved_at)
    except ValueError:
        return False
    return when.year == today.year and when.month == today.month


# ── Within-month validation ────────────────────────────────────────
# The calendar-month cache rule guarantees re-probing at month roll-over.
# It does NOT catch the case where Paxos publishes their next monthly
# report early in the month (say, mid-month, before the calendar rolls).
# The validator runs once per cached entry per cooldown window: it
# probes next month's URL pattern; if it returns 200, we upgrade.

_PAXOS_VALIDATION_COOLDOWN_HOURS = 12


def _paxos_should_validate(entry: dict) -> bool:
    """True if this cached Paxos entry hasn't been validated within the
    cooldown. Falls back to resolved_at when validated_at is missing —
    a freshly-discovered URL is effectively validated."""
    from datetime import datetime as _dt, timezone as _tz
    last = entry.get("validated_at")
    if last:
        try:
            when = _dt.fromisoformat(last.replace("Z", "+00:00"))
            age_h = (_dt.now(_tz.utc) - when).total_seconds() / 3600
            return age_h >= _PAXOS_VALIDATION_COOLDOWN_HOURS
        except (ValueError, AttributeError):
            pass
    resolved = entry.get("resolved_at")
    if resolved:
        try:
            when_d = date.fromisoformat(resolved)
            age_h = (date.today() - when_d).days * 24
            return age_h >= _PAXOS_VALIDATION_COOLDOWN_HOURS
        except ValueError:
            pass
    return True


def _validate_paxos_currency(symbol: str, today: date) -> str | None:
    """Probe the next-publication-month URL for `symbol`. Returns the
    new URL if a newer report has dropped early, else None.

    Cheap: one HEAD probe. Designed for the case where the calendar
    cache is still 'current' but Paxos has published the next month
    ahead of the month roll. Without this, we'd serve last month's
    report for up to ~30 days longer than needed."""
    # Next publication month = today's month + 1 (publication month MM
    # contains the report dated MM-1 per Paxos's URL convention)
    if today.month == 12:
        next_pub = date(today.year + 1, 1, 1)
    else:
        next_pub = date(today.year, today.month + 1, 1)
    url, _ = _candidate_url(symbol, next_pub)
    status = _probe(url)
    return url if status == 200 else None


# ── URL construction ──────────────────────────────────────────────────
def _candidate_url(symbol: str, publication: date) -> tuple[str, str]:
    """Build the (url, attestation_month) for a given publication month.

    Publication month MM is what appears in the /YYYY/MM/ WP path. The
    attestation PERIOD is MM minus one calendar month (rolls back at Jan).
    """
    pub_year = publication.year
    pub_month = publication.month
    if pub_month == 1:
        att_year = pub_year - 1
        att_month = 12
    else:
        att_year = pub_year
        att_month = pub_month - 1
    month_name = _MONTH_NAMES[att_month - 1]
    url = (
        f"{_BASE}/{pub_year:04d}/{pub_month:02d}/"
        f"{symbol}-Attestation-Report-{month_name}-{att_year:04d}.pdf"
    )
    attestation_month = f"{att_year:04d}-{att_month:02d}"
    return url, attestation_month


def _publication_window(today: date, months: int) -> Iterable[date]:
    """Yield the last `months` publication months, most recent first."""
    year = today.year
    month = today.month
    for _ in range(months):
        yield date(year, month, 1)
        month -= 1
        if month == 0:
            month = 12
            year -= 1


# ── HTTP probe ────────────────────────────────────────────────────────
def _probe(url: str) -> int:
    """HEAD-probe `url`; fall back to a tiny GET if HEAD is rejected.

    Some WordPress installs disallow HEAD on /wp-content/uploads or return
    405/403 even when the resource exists. A small Range GET is the safe
    fallback — same bandwidth profile, universally honoured. Returns the
    HTTP status code, or 0 on a transport error (treated as a miss).
    """
    try:
        resp = requests.head(
            url,
            timeout=_HTTP_TIMEOUT,
            allow_redirects=True,
            headers=_UA,
        )
    except requests.RequestException as exc:
        log_event(
            "paxos.resolver.head",
            level="warn",
            url=url,
            error_class=type(exc).__name__,
        )
        return 0
    log_event(
        "paxos.resolver.head",
        url=url,
        status_code=resp.status_code,
        method="HEAD",
    )
    if resp.status_code == 200:
        return 200
    # HEAD not honoured — fall back to a 1-byte ranged GET. Cheaper than a
    # full download, but reliably exercises the same auth/path logic as
    # the eventual full fetch.
    if resp.status_code in (403, 405, 501):
        try:
            resp2 = requests.get(
                url,
                timeout=_HTTP_TIMEOUT,
                allow_redirects=True,
                headers={**_UA, "Range": "bytes=0-0"},
                stream=True,
            )
        except requests.RequestException as exc:
            log_event(
                "paxos.resolver.head",
                level="warn",
                url=url,
                error_class=type(exc).__name__,
                method="GET",
            )
            return 0
        # Close streamed body promptly — we only needed the status line.
        try:
            resp2.close()
        except Exception:  # noqa: BLE001
            pass
        log_event(
            "paxos.resolver.head",
            url=url,
            status_code=resp2.status_code,
            method="GET",
        )
        # 206 = partial content (Range honoured), 200 = full body served.
        if resp2.status_code in (200, 206):
            return 200
        return resp2.status_code
    return resp.status_code


# ── public API ────────────────────────────────────────────────────────
def resolve_paxos_attestation_url(symbol: str) -> str | None:
    """Resolve the latest Paxos attestation PDF URL for `symbol`.

    Best-effort: returns the URL on success, None on miss or for a non-Paxos
    token. Never raises — the calling pipeline already has fallbacks.
    """
    symbol_u = symbol.upper()
    if symbol_u not in PAXOS_TOKENS:
        return None

    today = date.today()
    cache = _load_cache()
    entry = cache.get(symbol_u)
    if entry and entry.get("url") and _cache_entry_current(entry, today):
        # Validate against the issuer if we haven't checked recently.
        # Catches the case where Paxos publishes the next monthly
        # report ahead of the calendar roll (the cache would otherwise
        # hold last month's report for the rest of the calendar month).
        if _paxos_should_validate(entry):
            from datetime import datetime as _dt, timezone as _tz
            newer = _validate_paxos_currency(symbol_u, today)
            now_iso = _dt.now(_tz.utc).isoformat(timespec="seconds")
            entry["validated_at"] = now_iso
            if newer and newer != entry["url"]:
                log_event(
                    "paxos.resolver.upgraded",
                    symbol=symbol_u,
                    cached_url=entry["url"],
                    newer_url=newer,
                )
                from sca.tools.attestation_fetch import _record_override
                # Compute the attestation_month of the new URL (the
                # publication-month logic in _candidate_url: pub MM
                # contains the report dated MM-1).
                pub_month = (today.month % 12) + 1
                pub_year = today.year + (1 if today.month == 12 else 0)
                att_month_n = today.month
                att_year_n = today.year
                cache[symbol_u] = {
                    "url": newer,
                    "attestation_month": f"{att_year_n:04d}-{att_month_n:02d}",
                    "resolved_at": today.isoformat(),
                    "validated_at": now_iso,
                }
                _save_cache(cache)
                # Write through the store as an auto_validate override
                # so the central pipeline picks up the new URL too.
                _record_override(symbol_u, newer, "auto_validate")
                return newer
            cache[symbol_u] = entry
            _save_cache(cache)
        log_event(
            "paxos.resolver.hit",
            symbol=symbol_u,
            url=entry["url"],
            via="cache",
            attestation_month=entry.get("attestation_month", ""),
        )
        return entry["url"]

    for publication in _publication_window(today, _LOOKBACK_MONTHS):
        url, attestation_month = _candidate_url(symbol_u, publication)
        status = _probe(url)
        if status == 200:
            cache[symbol_u] = {
                "url": url,
                "attestation_month": attestation_month,
                "resolved_at": today.isoformat(),
            }
            _save_cache(cache)
            log_event(
                "paxos.resolver.hit",
                symbol=symbol_u,
                url=url,
                via="probe",
                attestation_month=attestation_month,
            )
            return url

    log_event(
        "paxos.resolver.miss",
        level="warn",
        symbol=symbol_u,
        lookback_months=_LOOKBACK_MONTHS,
    )
    return None


# Exposed for tests and for callers who want the raw probe sequence
# (e.g. an admin tool that wants to display every candidate it tried).
def _candidate_urls(symbol: str, today: date | None = None) -> list[tuple[str, str]]:
    """Return the (url, attestation_month) probe list for `symbol`."""
    today = today or date.today()
    return [
        _candidate_url(symbol.upper(), pub)
        for pub in _publication_window(today, _LOOKBACK_MONTHS)
    ]


__all__ = [
    "PAXOS_TOKENS",
    "resolve_paxos_attestation_url",
]
