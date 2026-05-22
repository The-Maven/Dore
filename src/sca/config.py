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
    rpc: str


@dataclass(frozen=True)
class Deployment:
    chain: str
    contract: str
    verified: bool
    kind: str = "native"  # native | bridged


@dataclass(frozen=True)
class Stablecoin:
    symbol: str
    name: str
    issuer: str
    deployments: tuple[Deployment, ...]
    transparency_url: str = ""
    latest_attestation_url: str = ""
    peg: str = "USD"


@lru_cache(maxsize=1)
def _raw() -> dict:
    return yaml.safe_load((CONFIG_DIR / "stablecoins.yaml").read_text())


@lru_cache(maxsize=1)
def chains() -> dict[str, Chain]:
    """Chains keyed by name. RPC overridable via env (e.g. ETHEREUM_RPC_URL)."""
    out: dict[str, Chain] = {}
    for name, spec in _raw()["chains"].items():
        rpc = os.environ.get(f"{name.upper()}_RPC_URL", spec["rpc"])
        out[name] = Chain(name=name, kind=spec["kind"], rpc=rpc)
    return out


@lru_cache(maxsize=1)
def stablecoins() -> dict[str, Stablecoin]:
    # Human verification votes override the registry's `verified` flag.
    from sca.votes import address_verified_overrides

    overrides = address_verified_overrides()
    out: dict[str, Stablecoin] = {}
    for token in _raw()["stablecoins"]:
        deployments: list[Deployment] = []
        for d in token["deployments"]:
            verified = bool(d.get("verified", False))
            voted = overrides.get((token["symbol"], d["chain"]))
            if voted is not None:
                verified = voted
            deployments.append(
                Deployment(
                    chain=d["chain"],
                    contract=d["contract"],
                    verified=verified,
                    kind=d.get("kind", "native"),
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
        )
    return out


def get_stablecoin(symbol: str) -> Stablecoin:
    coins = stablecoins()
    if symbol not in coins:
        raise ValueError(f"unknown stablecoin: {symbol!r}")
    return coins[symbol]
