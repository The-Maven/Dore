"""Deterministic tool: OFAC sanctions screening.

LAYER: facts. No LLM. Screens digital-currency addresses against the official
OFAC Specially Designated Nationals (SDN) list — the authoritative, public,
free US government source. This is exact-match screening against the
published list, not an attribution graph (that is a data-vendor's moat).

The ~28MB SDN XML is fetched once and cached, re-fetched when older than a
day. The parsed result carries the SDN publish date so guardrails can flag a
stale list — a stale list silently mis-screens.

Multi-URL resilience: Treasury hosts the SDN feed at two domains. We try
them in order — if the primary moves or 4xx's, we automatically fall back.
The list of URLs is small and stable; if a new mirror appears, add it here.

Tamper detection: every successful download is hashed (sha256) and the
hash logged. A future canary can compare across mirrors to catch a
compromised feed before it reaches our screen.
"""
from __future__ import annotations

import hashlib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests

from sca import config
from sca.models import SdnAddress
from sca.observability import log_event, timed

# Treasury publishes the SDN list at both URLs. They occasionally rotate
# which is canonical; rotating *us* through both gives us a free fallback.
OFAC_SDN_URLS = (
    "https://www.treasury.gov/ofac/downloads/sdn.xml",
    "https://ofac.treasury.gov/media/4076/download?inline",  # alternate path
)
# Legacy alias for callers that imported the old single-URL symbol.
OFAC_SDN_URL = OFAC_SDN_URLS[0]
_CACHE_DIR = config.DATA_DIR / "ofac"
_SDN_FILE = _CACHE_DIR / "sdn.xml"
_SDN_HASH_FILE = _CACHE_DIR / "sdn.sha256"
# Last-Modified header captured on the most recent successful fetch.
# Used by the within-window validator to ask Treasury "is the SDN
# you'd give me older or same as what I already have?" via
# If-Modified-Since. None when we've never captured one.
_SDN_LASTMOD_FILE = _CACHE_DIR / "sdn.last_modified"
# ISO timestamp of the most recent validation event (HEAD probe that
# confirmed the cached SDN is current OR a full re-fetch). Distinct
# from the file mtime, which only moves on a re-fetch.
_SDN_VALIDATED_FILE = _CACHE_DIR / "sdn.validated_at"
_MAX_AGE_SECONDS = 86_400  # refresh the SDN list daily
# Within the daily-refresh window, validate via If-Modified-Since at
# most once per cooldown so a sanctions screen doesn't hammer
# Treasury on every call.
_SDN_VALIDATION_COOLDOWN_S = 1800  # 30 minutes

_parsed: "SdnList | None" = None
_parsed_mtime: float = 0.0


@dataclass
class SdnList:
    publish_date: str  # MM/DD/YYYY, as published by OFAC
    addresses: dict[str, SdnAddress]  # keyed by address.lower()

    @property
    def staleness_days(self) -> int | None:
        try:
            published = datetime.strptime(self.publish_date, "%m/%d/%Y").date()
        except ValueError:
            return None
        return (date.today() - published).days


class SanctionsUnavailable(RuntimeError):
    """The OFAC SDN list could not be fetched or parsed."""


def _sdn_should_validate() -> bool:
    """True if the cached SDN hasn't been validated against Treasury
    within the cooldown. Falls back to file mtime when no validation
    timestamp exists (fresh fetch is effectively validated)."""
    try:
        if _SDN_VALIDATED_FILE.exists():
            last = float(_SDN_VALIDATED_FILE.read_text().strip())
            return (time.time() - last) >= _SDN_VALIDATION_COOLDOWN_S
    except (OSError, ValueError):
        pass
    if _SDN_FILE.exists():
        return (time.time() - _SDN_FILE.stat().st_mtime) >= _SDN_VALIDATION_COOLDOWN_S
    return True


def _mark_sdn_validated() -> None:
    """Record a successful validation event (cache confirmed current
    by Treasury OR a full re-fetch). Distinct from the file mtime."""
    try:
        from sca.persist import atomic_write_text
        atomic_write_text(_SDN_VALIDATED_FILE, str(time.time()))
    except Exception:  # noqa: BLE001
        pass  # validation tracking is best-effort, never blocks the sweep


def _validate_sdn_against_treasury() -> str | None:
    """HEAD-probe Treasury with If-Modified-Since to ask whether the
    cached SDN is current. Returns:
      "current"   → Treasury says nothing newer; bump validated_at
      "updated"   → Treasury reports a newer Last-Modified; caller
                    should re-fetch
      None        → probe failed (no network, 5xx, etc.); caller
                    serves cache silently

    Defensive: even if the server ignores If-Modified-Since and
    returns 200, we compare the returned Last-Modified header against
    what we have stored. Different = updated; same = current. That way
    a CDN that doesn't honour conditional requests doesn't cause us
    to re-download the full SDN every 30 minutes."""
    if not _SDN_LASTMOD_FILE.exists():
        # No stored Last-Modified — can't ask the conditional
        # question. Tell the caller to do a full fetch path.
        return "updated"
    try:
        prior = _SDN_LASTMOD_FILE.read_text().strip()
    except OSError:
        return "updated"
    if not prior:
        return "updated"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "If-Modified-Since": prior,
    }
    for url in OFAC_SDN_URLS:
        try:
            resp = requests.head(
                url, timeout=15, allow_redirects=True, headers=headers,
            )
        except requests.RequestException as exc:
            log_event(
                "sdn.validator.head_failed", level="info",
                endpoint=url, error_class=type(exc).__name__,
                error_message=str(exc)[:160],
            )
            continue
        if resp.status_code == 304:
            log_event("sdn.validator.unchanged_304", level="info",
                      endpoint=url, last_modified=prior)
            return "current"
        if resp.status_code == 200:
            # CDN may have ignored If-Modified-Since. Compare headers.
            new_lm = resp.headers.get("Last-Modified", "").strip()
            if new_lm and new_lm == prior:
                log_event("sdn.validator.unchanged_lastmod", level="info",
                          endpoint=url, last_modified=prior)
                return "current"
            log_event("sdn.validator.updated", level="info",
                      endpoint=url, prior=prior, new=new_lm)
            return "updated"
        # any other status — try next mirror
    return None


def _ensure_sdn_file(*, refresh: bool = False) -> Path:
    fresh = (
        _SDN_FILE.exists()
        and time.time() - _SDN_FILE.stat().st_mtime < _MAX_AGE_SECONDS
    )
    # Within the daily-refresh window, validate against Treasury if we
    # haven't probed recently. Catches OFAC publishing more than once
    # in 24h (they sometimes drop multiple lists per day on enforcement
    # actions). The hot path is gated by a 30min cooldown so a burst
    # of sanctions screens doesn't hammer Treasury.
    if fresh and not refresh:
        if _sdn_should_validate():
            verdict = _validate_sdn_against_treasury()
            if verdict == "current":
                _mark_sdn_validated()
                return _SDN_FILE
            if verdict == "updated":
                # Treasury reports a newer file — fall through to the
                # re-fetch block below (force a fresh download).
                refresh = True
            # verdict is None: probe failed, serve cache silently
            else:
                return _SDN_FILE
        else:
            return _SDN_FILE

    # Try each Treasury URL in order. First success wins; on any failure we
    # rotate. If they all fail and we have a cached copy, we use it but
    # mark it stale (loudly — the sanctions surface will escalate this).
    last_exc: Exception | None = None
    for url in OFAC_SDN_URLS:
        try:
            with timed("sdn.fetch", endpoint=url) as ev:
                resp = requests.get(
                    url, timeout=120,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                ev["status_code"] = resp.status_code
                ev["bytes"] = len(resp.content)
        except requests.RequestException as exc:
            last_exc = exc
            log_event(
                "sdn.fetch.failed", level="warn",
                endpoint=url, error_class=type(exc).__name__,
                error_message=str(exc),
            )
            continue

        # Atomic write — a crash mid-download must NOT leave a half-
        # written SDN file behind. The next sanctions screen would
        # silently parse an empty or truncated file and clear addresses
        # that should have flagged.
        from sca.persist import atomic_write_bytes, atomic_write_text
        atomic_write_bytes(_SDN_FILE, resp.content)
        sha = hashlib.sha256(resp.content).hexdigest()
        atomic_write_text(_SDN_HASH_FILE, sha)
        # Persist the Last-Modified header so the next within-window
        # validation can ask conditionally instead of re-fetching the
        # whole file. Mark this fetch as a validation event too.
        last_mod = resp.headers.get("Last-Modified", "").strip()
        if last_mod:
            atomic_write_text(_SDN_LASTMOD_FILE, last_mod)
        _mark_sdn_validated()
        log_event(
            "sdn.fetch.ok", level="info",
            endpoint=url, sha256=sha, bytes=len(resp.content),
            last_modified=last_mod,
        )
        return _SDN_FILE

    if _SDN_FILE.exists():
        log_event(
            "sdn.fetch.exhausted_using_cache", level="error",
            cache_age_days=int(
                (time.time() - _SDN_FILE.stat().st_mtime) / 86_400
            ),
        )
        return _SDN_FILE  # last-resort fallback to cached copy
    raise SanctionsUnavailable(
        f"could not fetch the OFAC SDN list from any mirror: {last_exc}"
    ) from last_exc


def _entry_name(entry: ET.Element) -> str:
    first = entry.findtext("firstName") or ""
    last = entry.findtext("lastName") or ""
    return f"{first} {last}".strip() or "(unnamed SDN entry)"


def _parse(xml_bytes: bytes) -> SdnList:
    root = ET.fromstring(xml_bytes)
    for el in root.iter():
        el.tag = el.tag.rpartition("}")[2]  # strip the XML namespace
    publish_date = (root.findtext(".//Publish_Date") or "").strip()
    addresses: dict[str, SdnAddress] = {}
    for entry in root.iter("sdnEntry"):
        uid = (entry.findtext("uid") or "").strip()
        name = _entry_name(entry)
        for id_el in entry.iter("id"):
            id_type = id_el.findtext("idType") or ""
            if "Digital Currency Address" not in id_type:
                continue
            number = (id_el.findtext("idNumber") or "").strip()
            if not number:
                continue
            addresses[number.lower()] = SdnAddress(
                address=number,
                currency=id_type.split("-")[-1].strip(),
                sdn_name=name,
                sdn_uid=uid,
            )
    if not addresses:
        raise SanctionsUnavailable(
            "OFAC SDN list parsed but contained no digital-currency "
            "addresses — the source format may have changed"
        )
    return SdnList(publish_date=publish_date, addresses=addresses)


def load_sdn(*, refresh: bool = False) -> SdnList:
    """Load the OFAC SDN digital-currency address list (fetched + cached)."""
    global _parsed, _parsed_mtime
    path = _ensure_sdn_file(refresh=refresh)
    mtime = path.stat().st_mtime
    if _parsed is None or mtime != _parsed_mtime or refresh:
        _parsed = _parse(path.read_bytes())
        _parsed_mtime = mtime
    return _parsed


def screen(
    addresses: list[str], *, refresh: bool = False
) -> list[SdnAddress]:
    """Return OFAC SDN matches for `addresses` (exact, case-insensitive)."""
    sdn = load_sdn(refresh=refresh)
    hits: list[SdnAddress] = []
    for addr in addresses:
        hit = sdn.addresses.get(addr.strip().lower())
        if hit is not None:
            hits.append(hit)
    return hits
