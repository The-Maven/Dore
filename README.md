# Doré — by Rayleigh Stark

**Doré** verifies stablecoin issuers' *claims* against on-chain *reality*.

A doré bar is the semi-pure gold a mine produces before it is assayed to
certified purity — raw claimed value that must be checked. A stablecoin
issuer's reserve attestation is the same: a claim. Doré is the assay. It
reconciles what issuers attest against what the blockchain actually shows,
and reports it cited, guardrailed, and honest — including when the honest
answer is "we cannot yet tell."

## Three compliance surfaces

| Surface | Verifies |
|---|---|
| **Attestation analysis** | the issuer's reserve attestation vs live on-chain supply — coverage, drift, staleness |
| **Sanctions screening** | the token's contract addresses against the official OFAC SDN list |
| **Redemption capacity** | reserve-liquidity tiers, liquid coverage, net redemption flow |

Each produces a deterministic, guardrailed, corpus-cited result.

## Architecture — three layers, nothing free-floating

1. **Facts** (`src/sca/tools/`) — deterministic. On-chain supply (EVM /
   Solana / Tron), attestation extraction, OFAC screening, metrics. The
   system never estimates a figure.
2. **Reasoning frame** (`src/sca/corpus/`) — a *human-gated* set of
   authoritative regulatory sources. Every judgement cites this, to the
   section. An uncited judgement is not allowed.
3. **Synthesis** (`src/sca/agent/`) — composes facts + frame into a cited
   analysis, with deterministic guardrails and citation verification.

Full design + the 5-layer audit: open `docs/architecture-booklet.html`.

## What's in the box

- **`src/sca/`** — the `sca` Python package: the three layers, the typed
  `models`, deterministic `validation` guardrails, the `Store` persistence
  abstraction, an MCP server, a themed CLI.
- **`web/`** — the Doré web app: a FastAPI server and a Bloomberg-terminal
  vanilla-JS SPA (F1 Monitor · F2 Analyze · F3 Corpus · F4 Evals ·
  F5 Sanctions · F6 Redemptions · F7 Analyst).
- **`agent/`** — the Hermes home: `SOUL.md`, `config.yaml`, skills,
  `GUARDRAILS.md` — the conversational analyst (see *The analyst* below).
- **`supabase/`** — the Postgres schema (`migrations/`).
- **`config/stablecoins.yaml`** — the tracked-token registry.
- **`corpus/`** — the source registry + staged regulatory text.
- **`skill/`**, **`evals/`**, **`brand/`**, **`docs/`** — pipeline skill
  prompts, the eval harness, the Doré wordmark, the architecture booklet.

## Setup

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"        # add .[pdf] .[supabase] .[agent] for full use

## Use — CLI

    sca analyze USDC            attestation analysis (cited, guardrailed)
    sca screen USDC             OFAC sanctions screen
    sca redemption USDC         redemption-capacity assessment
    sca supply USDT             on-chain supply only
    sca tokens / sources        registries + curation status
    sca curate                  human curation — approve sources, verify addresses
    sca refresh                 re-resolve attestation URLs
    sca evals                   the eval harness
    pytest                      125 offline, hermetic tests

## Use — web app

    .venv/bin/python -m uvicorn web.server:app --port 8000   # → localhost:8000

## Persistence & accounts

Durable state lives in **Supabase** (Postgres) behind a `Store` interface;
with no `SUPABASE_*` env vars the app runs fully on a file backend (and the
test suite always does — offline, hermetic). Authentication is **optional**:
anonymous users get full use; signing in unlocks saving (history, curation,
profile). See `supabase/migrations/`.

## The analyst

Doré's conversational analyst (web view **F7**) is built on the
`nousresearch/hermes-agent` runtime. It reaches the system only through a
**read-only MCP server** (`src/sca/mcp_server.py`) — a deliberate boundary:
no database writes, no secrets, no shell. Its identity is `agent/SOUL.md`;
the full security model is `agent/GUARDRAILS.md`. The `Dockerfile` is the
Railway deployment path.

## Guardrails

`validation.py` runs deterministic checks over the highest-risk data before
it reaches an analysis — breakdowns must sum, figures be positive, coverage
plausible, citations resolve, stated figures trace to tool outputs. Failures
become structured gaps. The system surfaces uncertainty; it never invents.

## Curation — your decisions

Human judgement is recorded as an append-only vote ledger (`votes.yaml` /
the Supabase `curation_votes` table), applied as overrides. `sca curate` (or
the F3 Corpus view) approves sources and verifies addresses. The agent may
propose; only a human approves.

## Status

Three working compliance surfaces · multi-chain supply · resilient
attestation locator · deterministic guardrails + citation verification ·
Supabase persistence + optional auth · the terminal web app · the Hermes
analyst integration. **125 hermetic tests.** Pending: deployment, more
attestation sources for JS-gated issuers, real corpus text + human approval,
a compliance operator expanding `evals/cases.yaml`.

---

Contributing or working on the code with an AI agent? Read **`AGENTS.md`**.
