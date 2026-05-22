"""The resilient attestation locator — transparency page -> latest PDF URL."""
import pytest

import sca.tools.attestation_locator as loc
from sca.llm import FakeLLM
from sca.tools.attestation_locator import (
    LocatorUnavailable,
    _extract_candidates,
    _gatsby_page_data_url,
    resolve_attestation_url,
)

_HTML = """
<html><body>
  <a href="/reports/jan-2026.pdf">January 2026 Attestation Report</a>
  <a href="/reports/feb-2026.pdf">February 2026 Reserve Examination</a>
  <a href="/about">About us</a>
  <a href="/careers">Careers</a>
</body></html>
"""

_GATSBY_JSON = """
{"data": {"reports": [
  "//assets.ctfassets.net/x/aa/bb/ISAE_3000R_Opinion_31-03-2026.pdf",
  "//assets.ctfassets.net/x/cc/dd/ISAE_3000R_Opinion_31-12-2025.pdf"
]}}
"""


class _Page:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        pass


def test_gatsby_page_data_url_derivation():
    assert _gatsby_page_data_url("https://tether.to/en/transparency") == (
        "https://tether.to/page-data/en/transparency/page-data.json"
    )


def test_extract_candidates_keeps_attestation_links_only():
    candidates = _extract_candidates(_HTML, "https://issuer.com")
    urls = [c["url"] for c in candidates]
    assert "https://issuer.com/reports/feb-2026.pdf" in urls
    assert "https://issuer.com/about" not in urls  # no pdf, no keyword


def test_gatsby_harvester_reads_page_data(monkeypatch):
    def fake_get(url, **kwargs):
        if "page-data" in url:
            return _Page(_GATSBY_JSON)
        return _Page("<html><body>js app</body></html>")

    monkeypatch.setattr(loc.requests, "get", fake_get)
    fake = FakeLLM(
        json_responses=[
            {
                "url": "https://assets.ctfassets.net/x/aa/bb/"
                "ISAE_3000R_Opinion_31-03-2026.pdf",
                "as_of_hint": "2026-03",
                "confidence": 0.97,
            }
        ]
    )
    out = resolve_attestation_url(
        "https://tether.to/en/transparency", symbol="USDT", llm=fake
    )
    assert out["url"].endswith("31-03-2026.pdf")


def test_static_harvester_via_llm(monkeypatch):
    # No page-data (gatsby harvester yields nothing) -> static harvest used.
    def fake_get(url, **kwargs):
        if "page-data" in url:
            return _Page("{}", status=404)
        return _Page(_HTML)

    monkeypatch.setattr(loc.requests, "get", fake_get)
    fake = FakeLLM(
        json_responses=[
            {
                "url": "https://issuer.com/reports/feb-2026.pdf",
                "as_of_hint": "2026-02",
                "confidence": 0.95,
            }
        ]
    )
    out = resolve_attestation_url("https://issuer.com/transparency", llm=fake)
    assert out["url"].endswith("feb-2026.pdf")


def test_js_gated_page_raises(monkeypatch):
    monkeypatch.setattr(
        loc.requests, "get",
        lambda *a, **k: _Page("<html><body>loading…</body></html>", 404),
    )
    with pytest.raises(LocatorUnavailable):
        resolve_attestation_url("https://issuer.com", llm=FakeLLM())


def test_llm_finds_no_attestation_raises(monkeypatch):
    def fake_get(url, **kwargs):
        if "page-data" in url:
            return _Page("{}", status=404)
        return _Page(_HTML)

    monkeypatch.setattr(loc.requests, "get", fake_get)
    fake = FakeLLM(json_responses=[{"url": None, "confidence": 0.0}])
    with pytest.raises(LocatorUnavailable):
        resolve_attestation_url("https://issuer.com", llm=fake)


def test_nextjs_harvester_reads_next_data(monkeypatch):
    next_html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"report":'
        '"//cdn.issuer.com/reports/jan-2026-attestation.pdf"}}}'
        "</script></body></html>"
    )

    def fake_get(url, **kwargs):
        if "page-data" in url:  # gatsby harvester misses -> next.js harvester
            return _Page("{}", status=404)
        return _Page(next_html)

    monkeypatch.setattr(loc.requests, "get", fake_get)
    fake = FakeLLM(json_responses=[{
        "url": "https://cdn.issuer.com/reports/jan-2026-attestation.pdf",
        "as_of_hint": "2026-01",
        "confidence": 0.9,
    }])
    out = resolve_attestation_url("https://issuer.com/transparency", llm=fake)
    assert out["url"].endswith("attestation.pdf")
