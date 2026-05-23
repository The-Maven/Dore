"""Deterministic tools layer — facts only, no LLM judgement.

(attestation_extract and attestation_locator use an LLM, but only for
structured extraction / link selection — never for interpretation.)
"""
from __future__ import annotations

from .address_verify import verify_address
from .attestation_extract import extract_attestation, extract_pdf_text
from .attestation_fetch import (
    AttestationUnavailable,
    fetch_latest_attestation,
    resolve_url,
)
from .attestation_locator import LocatorUnavailable, resolve_attestation_url
from .metrics import compute_metrics
from .onchain_supply import get_onchain_supply

__all__ = [
    "get_onchain_supply",
    "compute_metrics",
    "fetch_latest_attestation",
    "resolve_url",
    "AttestationUnavailable",
    "LocatorUnavailable",
    "resolve_attestation_url",
    "extract_attestation",
    "extract_pdf_text",
    "verify_address",
]
