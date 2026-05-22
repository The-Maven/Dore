"""Attestation URL resolution — cache freshness, seed, locator, PDF guard."""
from datetime import date

import pytest

import sca.tools.attestation_fetch as af
from sca.tools.attestation_fetch import AttestationUnavailable, resolve_url


def test_fresh_cache_url_is_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    af._save_cache({"USDC": {"url": "https://x.com/cached.pdf",
                             "resolved_at": date.today().isoformat()}})
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: True)
    assert resolve_url("USDC") == {
        "url": "https://x.com/cached.pdf", "via": "cache"
    }


def test_stale_cache_is_re_resolved(monkeypatch, tmp_path):
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    af._save_cache({"USDC": {"url": "https://x.com/old.pdf",
                             "resolved_at": "2020-01-01"}})
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: True)
    # Stale entry -> cache skipped -> seed used (USDC has a seed URL).
    assert resolve_url("USDC")["via"] == "seed"


def test_seed_used_when_no_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: True)
    assert resolve_url("USDC")["via"] == "seed"


def test_falls_back_to_locator(monkeypatch, tmp_path):
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: False)
    monkeypatch.setattr(
        af, "resolve_attestation_url",
        lambda url, **k: {"url": "https://x.com/found.pdf",
                          "confidence": 0.9, "as_of_hint": ""},
    )
    assert resolve_url("USDC", refresh=True) == {
        "url": "https://x.com/found.pdf", "via": "locator"
    }


def test_unavailable_when_locator_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(af, "_CACHE", tmp_path / "c.json")
    monkeypatch.setattr(af, "_head_ok", lambda url, **k: False)

    def boom(url, **k):
        raise af.LocatorUnavailable("page is JavaScript-rendered")

    monkeypatch.setattr(af, "resolve_attestation_url", boom)
    with pytest.raises(AttestationUnavailable):
        resolve_url("USDT", refresh=True)


def test_fetch_rejects_non_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr(
        af, "resolve_url", lambda s, **k: {"url": "https://x/page", "via": "seed"}
    )

    class _Resp:
        content = b"<html>404 Not Found</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(af.requests, "get", lambda *a, **k: _Resp())
    with pytest.raises(AttestationUnavailable):
        af.fetch_latest_attestation("USDC", dest_dir=tmp_path)


def test_fetch_accepts_pdf(monkeypatch, tmp_path):
    monkeypatch.setattr(
        af, "resolve_url", lambda s, **k: {"url": "https://x/r.pdf", "via": "seed"}
    )

    class _Resp:
        content = b"%PDF-1.6\n<binary pdf body>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(af.requests, "get", lambda *a, **k: _Resp())
    out = af.fetch_latest_attestation("USDC", dest_dir=tmp_path)
    assert out["source_url"] == "https://x/r.pdf"
    assert (tmp_path / "USDC-latest.pdf").exists()
