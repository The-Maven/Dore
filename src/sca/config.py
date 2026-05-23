"""Configuration: repo paths and the stablecoin registry."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# Repo root = two levels up from src/sca/config.py
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
CORPUS_DIR = ROOT / "corpus"
SKILL_DIR = ROOT / "skill"
EVALS_DIR = ROOT / "evals"
DATA_DIR = ROOT / "data"  # runtime artifacts (gitignored)


def _load_dotenv() -> None:
    """Minimal, zero-dependency .env loader. Existing env vars win."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


# ── Supabase (durable persistence) ────────────────────────────────────
# The service_role key is full-access and secret — keep it in .env only.
# When SUPABASE_URL is unset the app uses the file-backed store (offline /
# tests). See sca/store/.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def supabase_configured() -> bool:
    """True when Supabase credentials are present — selects the DB store."""
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


@dataclass(frozen=True)
class Chain:
    name: str
    kind: str  # evm | solana | tron
    # Ordered: primary first. We rotate on failure and corroborate
    # high-stakes reads (supply) across multiple endpoints. Single-string
    # `rpc:` in YAML still works — the loader wraps it.
    rpcs: tuple[str, ...]

    @property
    def rpc(self) -> str:
        """Primary endpoint — backwards-compat shim for older callers."""
        return self.rpcs[0] if self.rpcs else ""


@dataclass(frozen=True)
class Deployment:
    chain: str
    contract: str
    verified: bool
    kind: str = "native"  # native | bridged
    # How the `verified` flag was reached. Empty when the address is not
    # verified at all. Surfaced as a badge in the UI ("human" vs "auto"
    # vs the old loud unverified warning) — calmer than a binary flag.
    verification_method: str = ""
    # Accepted alternate symbols for on-chain self-report match. Real-world
    # need: Tether migrated USDT on Polygon to the omnichain USDT0 standard
    # in place at the same address — the contract self-reports "USDT0"
    # though it's still the canonical Tether USDT deployment. Listing
    # ("USDT0",) here lets auto-verify accept it without a human override.
    symbol_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stablecoin:
    symbol: str
    name: str
    issuer: str
    deployments: tuple[Deployment, ...]
    transparency_url: str = ""
    latest_attestation_url: str = ""
    peg: str = "USD"
    # Backing model determines what "attestation" *means* for this token:
    #   fiat_reserves          — backed by cash/treasuries; HAS issuer attestation
    #   crypto_collateral      — backed on-chain (e.g. DAI, GHO, LUSD); NO fiat attestation
    #   synthetic_delta_neutral— hedged positions (e.g. USDe); on-chain reserves dashboard
    #   algorithmic            — partial collateral + algorithmic stabilization
    #   new_or_unverified      — too new, no published attestation system yet
    # This drives how the UI frames a missing attestation: an honest "by design"
    # for crypto-collateralized, vs. a real gap for fiat-backed tokens.
    backing_model: str = "fiat_reserves"
    # Where the live backing data actually lives — used to replace dead-end
    # "n/a" with a working link. For crypto-collateralized: protocol dashboard.
    # For fiat-backed: transparency_url is usually right.
    protocol_url: str = ""


_YAML_PATH = CONFIG_DIR / "stablecoins.yaml"
_yaml_mtime: float = 0.0


def _maybe_invalidate_registry() -> None:
    """Drop the lru_caches when the YAML changes on disk.

    Lets a running server pick up registry edits without a restart — a real
    UX problem when the operator adds tokens via PR but the SPA still shows
    the old list. mtime check is dirt cheap; only the YAML drives caches.
    """
    global _yaml_mtime
    try:
        mtime = _YAML_PATH.stat().st_mtime
    except OSError:
        return
    if mtime != _yaml_mtime:
        _yaml_mtime = mtime
        # Wipe every registry-derived cache. Order doesn't matter; all
        # ultimately depend on _raw().
        for fn in (_raw, chains, _stablecoins_cached):
            cache_clear = getattr(fn, "cache_clear", None)
            if cache_clear is not None:
                cache_clear()


@lru_cache(maxsize=1)
def _raw() -> dict:
    return yaml.safe_load(_YAML_PATH.read_text())


@lru_cache(maxsize=1)
def chains() -> dict[str, Chain]:
    """Chains keyed by name. Endpoints overridable via env:

    - `ETHEREUM_RPCS` (comma-separated) overrides the whole pool
    - `ETHEREUM_RPC_URL` (single) overrides primary only — legacy

    YAML supports either `rpcs: [...]` (preferred) or `rpc: "..."` (legacy).
    Single-string YAML is wrapped into a one-element pool — no cross-check
    benefit, but identical behaviour to before.
    """
    out: dict[str, Chain] = {}
    for name, spec in _raw()["chains"].items():
        # YAML — either list or string
        yaml_rpcs = spec.get("rpcs") or ([spec["rpc"]] if spec.get("rpc") else [])
        # Env override (takes priority): full pool, then legacy single
        env_pool = os.environ.get(f"{name.upper()}_RPCS", "").strip()
        env_single = os.environ.get(f"{name.upper()}_RPC_URL", "").strip()
        if env_pool:
            rpcs = tuple(u.strip() for u in env_pool.split(",") if u.strip())
        elif env_single:
            # Legacy single override replaces just the primary; the rest of
            # the YAML pool is preserved as fallbacks.
            rpcs = (env_single, *(u for u in yaml_rpcs if u != env_single))
        else:
            rpcs = tuple(yaml_rpcs)
        if not rpcs:
            raise ValueError(f"chain '{name}' has no RPC endpoint configured")
        out[name] = Chain(name=name, kind=spec["kind"], rpcs=rpcs)
    return out


def stablecoins() -> dict[str, Stablecoin]:
    _maybe_invalidate_registry()
    return _stablecoins_cached()


# Back-compat: callers still do `config.stablecoins.cache_clear()` —
# wire it through to the actual cached implementation.
stablecoins.cache_clear = lambda: _stablecoins_cached.cache_clear()  # type: ignore[attr-defined]


@lru_cache(maxsize=1)
def _stablecoins_cached() -> dict[str, Stablecoin]:
    # Two ledgers can flip the `verified` flag:
    #   - human votes (`votes.py`) — append-only curation decisions
    #   - auto-verifications (`auto_verify.py`) — clean on-chain self-report
    # A human vote always wins; otherwise an auto entry verifies the address.
    # The deployment carries the resulting `verification_method` so the UI
    # can show "human" vs "auto" vs the loud unverified state.
    from sca.auto_verify import auto_verified_overrides
    from sca.votes import address_verified_overrides

    human = address_verified_overrides()
    auto = auto_verified_overrides()
    out: dict[str, Stablecoin] = {}
    for token in _raw()["stablecoins"]:
        deployments: list[Deployment] = []
        for d in token["deployments"]:
            key = (token["symbol"], d["chain"])
            verified = bool(d.get("verified", False))
            method = "human" if verified else ""  # YAML-side `verified: true` reads as human
            human_decision = human.get(key)
            if human_decision is not None:
                verified = human_decision
                method = "human" if verified else ""
            elif auto.get(key):
                verified = True
                # Read the recorded method off the auto-verify record so
                # Solana's "auto: SPL mint + decimals" path shows distinctly
                # from EVM/Tron's "auto: on-chain symbol match" path.
                from sca.auto_verify import auto_verification_records
                rec = auto_verification_records().get(key, {})
                method = rec.get("method", "auto: on-chain symbol match")
            deployments.append(
                Deployment(
                    chain=d["chain"],
                    contract=d["contract"],
                    verified=verified,
                    kind=d.get("kind", "native"),
                    verification_method=method,
                    symbol_aliases=tuple(d.get("symbol_aliases", ())),
                )
            )
        out[token["symbol"]] = Stablecoin(
            symbol=token["symbol"],
            name=token["name"],
            issuer=token["issuer"],
            deployments=tuple(deployments),
            transparency_url=token.get("transparency_url", ""),
            latest_attestation_url=token.get("latest_attestation_url", ""),
            peg=token.get("peg", "USD"),
            backing_model=token.get("backing_model", "fiat_reserves"),
            protocol_url=token.get("protocol_url", ""),
        )
    return out


def get_stablecoin(symbol: str) -> Stablecoin:
    """Look up a stablecoin by symbol — CASE-INSENSITIVE.

    The registry has mixed-case keys (USDe, USDf, crvUSD) because some
    issuers use distinct casing as part of the brand. Callers (web routes,
    URL params, user input) shouldn't have to know that — a case-insensitive
    lookup returns the canonical entry without forcing every endpoint to
    normalize first.
    """
    coins = stablecoins()
    if symbol in coins:
        return coins[symbol]
    lookup = {s.lower(): s for s in coins}
    canonical = lookup.get(symbol.lower())
    if canonical is not None:
        return coins[canonical]
    raise ValueError(f"unknown stablecoin: {symbol!r}")
