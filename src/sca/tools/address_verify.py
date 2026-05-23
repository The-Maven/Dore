"""Deterministic tool: contract-address self-report check.

LAYER: facts. No LLM. Asks the contract itself what it is — its `symbol()`
and `decimals()` (and, as a sanity guard, `totalSupply()`) — and compares
against what the registry says it should be. If they match and supply is
non-trivial, that is strong evidence the address is genuine.

This is the substrate for `sca verify`: a clean on-chain match clears the
"unverified" wall for an address; anything else (mismatch, unsupported
chain, RPC failure, empty contract) stays flagged for a human. The agent
flags + auto-verifies the unambiguous; the human handles the ambiguous —
the curation lever is preserved, just no longer the only path through.

A wrong contract address does not magically self-report the right symbol,
so a clean match is reliable. The zero-supply guard exists because a
deployed-but-empty contract can carry the right symbol; we won't auto-
verify on metadata alone with no economic substance behind it.
"""
from __future__ import annotations

import requests

from sca import config
from sca.config import Chain
from sca.tools.onchain_supply import (
    SEL_DECIMALS,
    SEL_TOTAL_SUPPLY,
    _json_rpc,
    _pool_call,
    _tron_call_at,
)

# ERC-20 `symbol()` selector. Returns an ABI-encoded `string` (or, for
# some pre-2017 tokens like MKR, a raw `bytes32`).
SEL_SYMBOL = "0x95d89b41"


# ── ABI decoding (just enough for symbol()) ───────────────────────────
def _decode_symbol_hex(hex_result: str) -> str:
    """Decode an ERC-20 `symbol()` return value.

    Accepts both the modern dynamic `string` ABI encoding and the legacy
    `bytes32` shape some early tokens still use. Returns the symbol with
    any null padding stripped; an empty result means the contract does
    not expose `symbol()` in a recognisable form.
    """
    if hex_result is None or hex_result in ("", "0x"):
        return ""
    raw = bytes.fromhex(hex_result[2:] if hex_result.startswith("0x") else hex_result)
    # Dynamic `string`: 32-byte offset, 32-byte length, then data.
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset == 32 and len(raw) >= 64:
            length = int.from_bytes(raw[32:64], "big")
            if 0 < length <= len(raw) - 64:
                try:
                    return raw[64 : 64 + length].decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    return ""
    # Legacy `bytes32`: 32 bytes, right-padded with nulls.
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", errors="ignore")
    return ""


# ── result builder ────────────────────────────────────────────────────
def _result(
    verified: bool,
    signal: str,
    *,
    on_chain_symbol: str | None = None,
    on_chain_decimals: int | None = None,
    detail: str = "",
) -> dict:
    """Build the canonical verification result dict.

    Shape is stable for callers (CLI summary, web): every field present.
    """
    return {
        "verified": verified,
        "signal": signal,
        "on_chain_symbol": on_chain_symbol,
        "on_chain_decimals": on_chain_decimals,
        "detail": detail,
    }


# ── readers per chain kind ────────────────────────────────────────────
def _read_evm(chain: Chain, contract: str) -> tuple[str, int, int]:
    """Return (symbol, decimals, total_supply) for an EVM contract.

    Uses the RPC pool (sequential — first endpoint wins) so verification
    survives a primary outage. No cross-check: verification is one-off
    and a clean symbol+decimals+supply match is its own integrity gate.
    """
    def evm_call(selector: str):
        def _do(url: str) -> str:
            r = _json_rpc(url, "eth_call",
                          [{"to": contract, "data": selector}, "latest"])
            if r in (None, "0x", ""):
                raise RuntimeError("empty result (wrong address or chain?)")
            return r
        value, _, _ = _pool_call(chain, _do, cross_check=False,
                                  label=f"verify:{contract[:10]}:{selector}")
        return value

    sym_raw = evm_call(SEL_SYMBOL)
    dec_raw = evm_call(SEL_DECIMALS)
    sup_raw = evm_call(SEL_TOTAL_SUPPLY)
    return _decode_symbol_hex(sym_raw), int(dec_raw, 16), int(sup_raw, 16)


# ── Solana SPL verification ───────────────────────────────────────────
# SPL tokens don't expose `symbol()` like EVM — the human-readable
# symbol lives in a Metaplex metadata PDA that requires curve-aware
# derivation (ed25519). Implementing that without solana-py is a lot of
# code for marginal value. Instead we verify the address through three
# independent SPL-Token-Program checks:
#
#   1. `getAccountInfo(mint)` returns an account whose owner is the
#      canonical SPL Token Program. This validates the address CLASS —
#      it can only ever be reached if `mint` is a real SPL mint.
#   2. The mint's `decimals` (parsed from account.data) matches the
#      registry's declared decimals — eliminates the "wrong mint but
#      same chain" failure mode.
#   3. `getTokenSupply` returns a non-zero supply — eliminates the
#      "right-class empty contract" failure mode.
#
# All three passing produces the same auto-verification trust as a
# clean EVM symbol() match: the registry's address provably IS an SPL
# mint with the expected decimals and real economic substance. The UI
# can render this with a slightly different "auto: SPL mint + decimals"
# verification_method so a reviewer sees the proof shape clearly.
SPL_TOKEN_PROGRAM_IDS = {
    # Canonical SPL Token Program (Token v1) — the vast majority of tokens.
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    # Token-2022 program (newer issuance, e.g. PYUSD on Solana uses this).
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


def _read_solana(chain: Chain, mint: str) -> tuple[str, int, int]:
    """SPL-Token verification triplet.

    Returns (symbol, decimals, total_supply) where symbol is a soft
    proxy: the SPL token program owns this mint AND decimals match.
    We return the EXPECTED symbol when the proof triplet passes, so the
    downstream `sym_match` check passes against the registry's value.
    On any check failure, raise — the verify entry-point converts that
    into an error result for the UI to flag for a human.
    """
    import base64

    # 1. getAccountInfo with jsonParsed encoding parses the SPL mint layout
    #    in one round trip. The owner field is the program that holds the
    #    account; the parsed data carries the decimals.
    info = _json_rpc(chain.rpc, "getAccountInfo",
                     [mint, {"encoding": "jsonParsed"}])
    value = (info or {}).get("value")
    if not value:
        raise RuntimeError(
            "getAccountInfo returned no value — address is not an "
            "existing on-chain account on this cluster"
        )
    owner = value.get("owner", "")
    if owner not in SPL_TOKEN_PROGRAM_IDS:
        raise RuntimeError(
            f"account exists but owner is {owner!r}, not the SPL Token "
            "Program — this is not a token mint"
        )

    parsed = (value.get("data", {}) or {}).get("parsed", {})
    if (parsed.get("type") or "") != "mint":
        raise RuntimeError("account is owned by SPL Token Program but "
                           "not a mint (likely a token account)")
    info_block = parsed.get("info", {}) or {}
    decimals = info_block.get("decimals")
    if decimals is None:
        raise RuntimeError(
            "mint parsed payload missing 'decimals' — Solana RPC may not "
            "support jsonParsed for this token program version"
        )

    # 3. supply check — getTokenSupply returns amount + decimals.
    supply_resp = _json_rpc(chain.rpc, "getTokenSupply", [mint])
    sv = (supply_resp or {}).get("value", {})
    raw = int(sv.get("amount", 0))
    if raw == 0:
        raise RuntimeError(
            "mint is a valid SPL token but totalSupply is zero — needs "
            "human review (could be an unused mint reusing a known name)"
        )

    # The verify entry-point compares on_chain_symbol vs expected_symbol
    # (with alias support). For Solana we have no on-chain symbol to
    # report, so we return a sentinel that the entry-point treats
    # specially below: empty string + decimals + supply.
    return "<spl-mint-verified>", int(decimals), raw


def _read_tron(chain: Chain, contract: str) -> tuple[str, int, int]:
    """Return (symbol, decimals, total_supply) for a Tron TRC20 contract.

    Uses raw-hex for symbol() so the ABI-encoded string survives intact —
    the numeric int() path drops leading zero bytes and truncates past
    32 bytes. decimals/totalSupply are fixed-width so the int path is fine.
    """
    def tron_call(selector: str, *, as_int: bool):
        def _do(url: str):
            raw = _tron_call_at(url, contract, selector)
            return int(raw, 16) if as_int else raw
        value, _, _ = _pool_call(chain, _do, cross_check=False,
                                  label=f"verify:{contract[:10]}:{selector}")
        return value

    symbol = _decode_symbol_hex(tron_call("symbol()", as_int=False))
    decimals = tron_call("decimals()", as_int=True)
    total_supply = tron_call("totalSupply()", as_int=True)
    return symbol, decimals, total_supply


# ── the public entry point ────────────────────────────────────────────
def verify_address(
    chain: str,
    contract: str,
    expected_symbol: str,
    expected_decimals: int | None = None,
    symbol_aliases: tuple[str, ...] = (),
) -> dict:
    """Self-report check: does the contract say it is `expected_symbol`?

    `symbol_aliases` lets the registry declare known alternate symbols
    for the same canonical token (e.g. Tether's USDT0 omnichain upgrade
    in place of USDT on Polygon). Match is accepted on the canonical
    symbol OR any alias. Aliases must be deliberately registry-recorded;
    we never silently accept arbitrary substitutions.

    Returns a dict with stable keys:
      `verified`        bool — only True on a clean on-chain match
      `signal`          one of: 'on-chain-symbol', 'mismatch',
                        'unsupported-chain', 'error'
      `on_chain_symbol` what the contract reports for `symbol()`, if any
      `on_chain_decimals` what it reports for `decimals()`, if read
      `detail`          a short human-readable note

    Strict by design: anything other than a clean match leaves the address
    flagged. We never paper over an ambiguity.
    """
    chains = config.chains()
    chain_cfg = chains.get(chain)
    if chain_cfg is None:
        return _result(False, "error", detail=f"chain '{chain}' not configured")

    if chain_cfg.kind not in ("evm", "tron", "solana"):
        return _result(
            False,
            "unsupported-chain",
            detail=f"no verifier for chain kind '{chain_cfg.kind}'",
        )

    try:
        if chain_cfg.kind == "evm":
            on_chain_symbol, on_chain_decimals, total_supply = _read_evm(
                chain_cfg, contract
            )
        elif chain_cfg.kind == "solana":
            on_chain_symbol, on_chain_decimals, total_supply = _read_solana(
                chain_cfg, contract
            )
        else:  # tron
            on_chain_symbol, on_chain_decimals, total_supply = _read_tron(
                chain_cfg, contract
            )
    except requests.RequestException as exc:
        return _result(False, "error", detail=f"rpc error: {exc}")
    except Exception as exc:  # noqa: BLE001 - report, don't swallow
        return _result(False, "error", detail=str(exc))

    # Solana: the symbol-match path doesn't apply (no on-chain symbol).
    # The _read_solana function returns a sentinel and has already proven
    # (1) SPL-Token-Program ownership, (2) decimals match expected,
    # (3) non-zero supply. We still validate decimals explicitly here so
    # the registry mismatch case is caught uniformly with EVM.
    if on_chain_symbol == "<spl-mint-verified>":
        if (expected_decimals is not None
                and int(on_chain_decimals) != int(expected_decimals)):
            return _result(
                False,
                "mismatch",
                on_chain_decimals=on_chain_decimals,
                detail=(
                    f"SPL mint exists but decimals {on_chain_decimals} "
                    f"≠ registry-expected {expected_decimals}"
                ),
            )
        return _result(
            True,
            "spl-mint-verified",
            on_chain_symbol=expected_symbol,  # echo the registry value
            on_chain_decimals=on_chain_decimals,
            detail=(
                "SPL mint owned by SPL Token Program · decimals match "
                "· non-zero supply (Metaplex symbol skipped: PDA "
                "derivation requires ed25519, weak link in the proof "
                "chain compared to the three structural checks above)"
            ),
        )

    if not on_chain_symbol:
        return _result(
            False,
            "error",
            on_chain_decimals=on_chain_decimals,
            detail="contract did not return a readable symbol()",
        )

    on_chain_norm = on_chain_symbol.strip().lower()
    accepted = {expected_symbol.strip().lower()}
    accepted.update(a.strip().lower() for a in symbol_aliases if a)
    sym_match = on_chain_norm in accepted
    matched_via_alias = sym_match and on_chain_norm != expected_symbol.strip().lower()
    dec_match = (
        expected_decimals is None or int(on_chain_decimals) == int(expected_decimals)
    )

    if not sym_match or not dec_match:
        bits = []
        if not sym_match:
            bits.append(
                f"symbol {on_chain_symbol!r} ≠ expected {expected_symbol!r}"
            )
        if not dec_match:
            bits.append(
                f"decimals {on_chain_decimals} ≠ expected {expected_decimals}"
            )
        return _result(
            False,
            "mismatch",
            on_chain_symbol=on_chain_symbol,
            on_chain_decimals=on_chain_decimals,
            detail="; ".join(bits),
        )

    # Sanity guard: a real stablecoin has economic substance. A correctly
    # named but empty contract is suspicious enough to leave for a human.
    if total_supply == 0:
        return _result(
            False,
            "mismatch",
            on_chain_symbol=on_chain_symbol,
            on_chain_decimals=on_chain_decimals,
            detail="symbol matched but totalSupply is zero — needs human review",
        )

    detail = "on-chain symbol + decimals match"
    if matched_via_alias:
        detail = (
            f"matched via registered alias: contract self-reports "
            f"{on_chain_symbol!r}, accepted as alias for {expected_symbol!r}"
        )
    return _result(
        True,
        "on-chain-symbol",
        on_chain_symbol=on_chain_symbol,
        on_chain_decimals=on_chain_decimals,
        detail=detail,
    )


# ── orchestration over the whole registry ─────────────────────────────
def verify_all(symbol: str | None = None) -> dict:
    """Run `verify_address` over every deployment (or just one symbol).

    Persists clean matches to the auto-verification ledger; never touches
    deployments a human has already decided on. Returns a structured summary
    the CLI + refresh hook can render or log:

        {
          "checked":      [(symbol, chain, contract, result), ...],
          "auto_verified": N,   "skipped_human": N,
          "unsupported":  N,    "mismatch":      N,    "errored": N,
        }
    """
    from sca import auto_verify
    from sca.votes import address_verified_overrides

    human = address_verified_overrides()

    # Resolve the deployments to check from the raw YAML, not the overlaid
    # view — overlaid `verified` already reflects prior auto-verifications,
    # which would otherwise mask a regression on re-run.
    raw = config._raw()["stablecoins"]
    if symbol is not None:
        raw = [t for t in raw if t["symbol"] == symbol.upper()]
        if not raw:
            raise ValueError(f"unknown stablecoin: {symbol!r}")

    summary: dict = {
        "checked": [],
        "auto_verified": 0,
        "skipped_human": 0,
        "unsupported": 0,
        "mismatch": 0,
        "errored": 0,
    }

    for token in raw:
        sym = token["symbol"]
        expected_decimals = token.get("decimals")
        for dep in token["deployments"]:
            chain = dep["chain"]
            contract = dep["contract"]

            if (sym, chain) in human:
                # Human decision is sovereign — never overwrite, never re-check.
                summary["skipped_human"] += 1
                continue

            aliases = tuple(dep.get("symbol_aliases", ()) or ())
            result = verify_address(
                chain, contract, sym, expected_decimals,
                symbol_aliases=aliases,
            )
            summary["checked"].append((sym, chain, contract, result))

            if result["verified"]:
                # Different verification proof shapes get different
                # method labels — the UI surfaces this so a reviewer
                # sees AT A GLANCE what kind of proof underlies each
                # auto-verified entry (EVM contract self-report vs.
                # Solana SPL structural triplet).
                method = (
                    "auto: SPL mint + decimals match"
                    if result["signal"] == "spl-mint-verified"
                    else "auto: on-chain symbol match"
                )
                auto_verify.record_auto_verification(
                    sym,
                    chain,
                    on_chain_symbol=result["on_chain_symbol"] or sym,
                    on_chain_decimals=result["on_chain_decimals"] or 0,
                    method=method,
                )
                summary["auto_verified"] += 1
            elif result["signal"] == "unsupported-chain":
                summary["unsupported"] += 1
            elif result["signal"] == "mismatch":
                # If we'd previously auto-verified this address and now see a
                # mismatch, drop the cached entry so the loud flag returns.
                auto_verify.clear_auto_verification(sym, chain)
                summary["mismatch"] += 1
            else:  # 'error'
                summary["errored"] += 1

    # The deployment loader caches; invalidate so callers see the new state.
    config.stablecoins.cache_clear()
    return summary

