"""Resilient attestation locator.

Attestation PDF URLs rot — issuers publish a new file every month. This
locator resolves the *current* latest attestation PDF from the issuer's
durable transparency page, so a changed URL does not blindside us.

Candidate links are gathered by harvesters, tried in order of reliability:

  gatsby  — many transparency sites are Gatsby; their page-data.json is
            static JSON listing every report URL (no JavaScript, no token).
  static  — parse <a> links straight from the page HTML.

Whatever a harvester yields, the LLM then picks the most recent,
token-matched attestation PDF — robust to layout changes. If nothing is
found (e.g. a JS-only site with no page-data), LocatorUnavailable is raised
so the caller can fall back to a cached URL and surface the staleness.

Adding a site type = adding a harvester to _HARVESTERS.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests

from sca.llm import LLMClient, get_llm

_UA = {"User-Agent": "Mozilla/5.0"}
_LINK_RE = re.compile(
    r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_PDF_RE = re.compile(r"(?:https?:)?//[^\"'\\\s]+\.pdf", re.IGNORECASE)
_NEXTDATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_KEYWORDS = ("attestation", "reserve", "examination", "assurance", "report")

_SYSTEM = (
    "You identify the single most recent RESERVE attestation report FOR THE "
    "SPECIFIED stablecoin, from links scraped from an issuer's transparency "
    "page. It must be a reserve / financial attestation or examination "
    "report — an independent accountant's report on the assets backing the "
    "token. It must NOT be a smart-contract security audit, code review, "
    "whitepaper, terms of service, or policy document; reject those. The "
    "report must clearly correspond to the specified token — an issuer may "
    "publish separate reports for several stablecoins. Prefer the newest "
    "dated report, and prefer a direct PDF. If no link is a reserve "
    "attestation for the specified token, return null for url — never "
    "substitute a different document or a different token's report."
)


class LocatorUnavailable(RuntimeError):
    """No resolvable attestation link was found on the transparency page."""


def _clean_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


# ── harvesters — one per transparency-site type ───────────────────────
def _extract_candidates(html: str, base_url: str) -> list[dict]:
    """Static <a>-link harvest from page HTML."""
    out: list[dict] = []
    seen: set[str] = set()
    for href, inner in _LINK_RE.findall(html):
        url = urljoin(base_url, href.strip())
        text = _TAG_RE.sub("", inner).strip()[:160]
        haystack = f"{url} {text}".lower()
        if ".pdf" not in url.lower() and not any(
            k in haystack for k in _KEYWORDS
        ):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "text": text})
    return out


def _harvest_static(transparency_url: str, timeout: float) -> list[dict]:
    resp = requests.get(transparency_url, headers=_UA, timeout=timeout)
    resp.raise_for_status()
    return _extract_candidates(resp.text, transparency_url)


def _gatsby_page_data_url(transparency_url: str) -> str:
    parts = urlparse(transparency_url)
    path = parts.path.strip("/")
    return f"{parts.scheme}://{parts.netloc}/page-data/{path}/page-data.json"


def _harvest_gatsby(transparency_url: str, timeout: float) -> list[dict]:
    """Gatsby sites expose a static page-data.json with every report URL."""
    resp = requests.get(
        _gatsby_page_data_url(transparency_url), headers=_UA, timeout=timeout
    )
    if resp.status_code != 200:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in _PDF_RE.findall(resp.text):
        url = _clean_url(raw)
        if url in seen:
            continue
        seen.add(url)
        # the filename carries the date — a strong hint for the LLM
        out.append({"url": url, "text": url.rsplit("/", 1)[-1]})
    return out


def _harvest_nextjs(transparency_url: str, timeout: float) -> list[dict]:
    """Next.js sites embed page data as JSON in a __NEXT_DATA__ script tag."""
    resp = requests.get(transparency_url, headers=_UA, timeout=timeout)
    if resp.status_code != 200:
        return []
    match = _NEXTDATA_RE.search(resp.text)
    if not match:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in _PDF_RE.findall(match.group(1)):
        url = _clean_url(raw)
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "text": url.rsplit("/", 1)[-1]})
    return out


# Tried in order of reliability; first harvester to yield links wins.
_HARVESTERS = (_harvest_gatsby, _harvest_nextjs, _harvest_static)


def resolve_attestation_url(
    transparency_url: str,
    *,
    symbol: str = "",
    llm: LLMClient | None = None,
    timeout: float = 30.0,
) -> dict:
    """Resolve the current latest attestation PDF URL from a transparency page.

    Returns {"url", "confidence", "as_of_hint"}.
    Raises LocatorUnavailable when nothing usable is found.
    """
    candidates: list[dict] = []
    for harvest in _HARVESTERS:
        try:
            found = harvest(transparency_url, timeout)
        except requests.RequestException:
            found = []
        if found:
            candidates = found
            break

    if not candidates:
        raise LocatorUnavailable(
            f"{transparency_url}: no attestation links found "
            "(no Gatsby page-data, no static PDF links)"
        )

    llm = llm or get_llm()
    listing = "\n".join(
        f"- {c['url']}  |  {c['text']}" for c in candidates[:80]
    )
    data = llm.extract_json(
        system=_SYSTEM,
        prompt=f"Stablecoin: {symbol or 'unknown'}\nLinks:\n{listing}",
        schema={
            "url": "the chosen attestation PDF URL, or null",
            "as_of_hint": "YYYY-MM of that report, or null",
            "confidence": "float 0.0-1.0",
        },
    )
    url = data.get("url")
    if not url:
        raise LocatorUnavailable(
            f"{transparency_url}: no attestation report matched "
            f"{symbol or 'the token'} among the links"
        )
    return {
        "url": url,
        "confidence": float(data.get("confidence") or 0.0),
        "as_of_hint": data.get("as_of_hint") or "",
    }
