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
2. **Reasoning frame** (`src/sca/corpus/`) — a curated set of
   authoritative regulatory sources, included and citable by default. A
   human may opt a source out, and may mark one explicitly verified (a
   badge on its citations). Every judgement cites this, to the section.
   An uncited judgement is not allowed.
3. **Synthesis** (`src/sca/agent/`) — composes facts + frame into a cited
   analysis, with deterministic guardrails and citation verification.

Full design + the 5-layer audit: open `docs/architecture-booklet.html`.

## Inventory — what we read, who owns it

This list is the source of truth for "where does data come from at any point"
— useful for compliance reviews, incident triage, and onboarding.

### Chains (9)

Every chain has a primary RPC and (where stable mirrors exist) one or two
fallbacks. High-stakes supply reads are corroborated across endpoints;
disagreements escalate to a tertiary and a critical log event.

| Chain | Kind | Endpoints | Primary custodian |
|---|---|---|---|
| ethereum | EVM | 3 | publicnode + LlamaRPC + Ankr |
| base | EVM | 3 | publicnode + LlamaRPC + Coinbase |
| arbitrum | EVM | 3 | publicnode + LlamaRPC + Offchain Labs |
| optimism | EVM | 3 | publicnode + LlamaRPC + OP Labs |
| polygon | EVM | 3 | publicnode + LlamaRPC + Polygon Labs |
| bsc | EVM | 3 | publicnode + LlamaRPC + Binance |
| avalanche | EVM | 2 | publicnode + Ava Labs |
| solana | Solana | 2 | Solana Foundation + publicnode |
| tron | Tron | 1 | TronGrid (set `TRON_PRO_API_KEY` for higher quota) |

Override any pool at runtime: `<CHAIN>_RPCS=url1,url2,url3` (preferred) or
legacy `<CHAIN>_RPC_URL=url`.

### Stablecoins (25 tracked)

Tokens carry a `backing_model` so the UI frames a missing attestation
correctly — by-design (crypto-collateralized) vs. real gap (fiat-backed,
fetch failed).

- **Fiat reserves** (CPA attestations): USDC, USDT, PYUSD, USDP, USDG,
  TUSD, GUSD, FDUSD, EURC, RLUSD, AUSD, USDM, EURI, AEUR
- **Crypto-collateralized** (on-chain backing): DAI, USDS, GHO, crvUSD,
  LUSD, MIM, FRAX
- **Synthetic / delta-neutral**: USDe, USDf
- **Algorithmic / partial collateral**: USDD
- **New / no mature attestation system**: USD1

Run `sca tokens` for the live view, `sca verify` to auto-verify addresses
against on-chain `symbol()` + `decimals()`. Current cleared count: **59
auto-verified deployments** across the 9 chains.

### External feeds (non-RPC)

| Feed | Purpose | URLs | Custodian |
|---|---|---|---|
| OFAC SDN list | sanctions screening | 2 (treasury.gov primary + ofac.treasury.gov fallback) | US Department of the Treasury |
| Issuer transparency pages | attestation discovery | one per token (see registry) | Each token's issuer |
| Corpus source URLs | regulatory citations | 13 (BIS, FSB, EUR-Lex, NYDFS, FCA, OFAC FAQs, …) | Mixed (gov + standards bodies) |
| DeepSeek API | LLM synthesis | `api.deepseek.com` | DeepSeek |

Every fetched URL is **snapshotted** to `data/source_snapshots/` so when a
live link breaks the UI silently falls back to the archived body and flags
the broken source loud enough that a maintainer can re-anchor it. Run
`sca canary` to sweep every external dependency and report drift.

### Auto-discovery — keeping the corpus alive

`sca discover` sweeps three discovery surfaces and registers anything new
to the corpus (included by default, opt-out via `sca curate`). Source
bodies are staged to `corpus/staging/` and picked up on the next
retrieval. Every fetch is snapshotted; dedup is by sha256 of the body.

| Surface | Module | Sources | Tier |
|---|---|---|---|
| HTML index scrapers | `sca.discovery` | 6 (OFAC, BIS, FSB, EUR-Lex MiCA, NYDFS, IAASB) | tier1_official |
| RSS / Atom feed pollers | `sca.discovery_rss` | 5 (BIS news, FSB news, NYDFS press, Fed speeches, ECB digital-euro) | tier1_official |
| Issuer + analytics blogs | `sca.discovery_blogs` | 6 (Circle, Paxos, Tether, Chainalysis, TRM Labs, Elliptic) | tier2_industry |

**Total discoverable sources: 17 pollers** across three surfaces, all
flowing through one `sync_discovered()` chokepoint.

### Keeping the corpus fresh — cron

The intended cadence: **once daily**, low-priority off-peak. Example crontab:

    # Doré daily corpus sweep — 04:17 UTC (low-traffic, post-attestation cycle)
    17 4 * * *  cd /srv/dore && .venv/bin/python -m sca.cli discover >> /var/log/dore-discover.log 2>&1

The discovery flow is idempotent: bodies are sha256-deduped against
`data/discovered_sources.json`, so a daily run only stages what's actually
new. Auto-ingest then registers passages on the next `retrieve()`. Live
source health is checked by the in-process `health_thread` (default 6h
cadence) — it emits `health.source.flipped` events that an external alert
sink can subscribe to.

In production the web server runs a **background source-health canary**
(`sca.health_thread`) on a configurable cadence — default every **6
hours**, override via `SCA_HEALTH_INTERVAL_HOURS`, disable with
`SCA_HEALTH_DISABLED=1`. Each cycle runs the full `sca canary` sweep,
persists per-source state to `data/health_state.json`, and emits a
`health.source.flipped` structured event the moment a source flips
live↔broken — so an alert sink (Sentry, paging) catches outages without
a human noticing the badge change.

### Data flow — who touches a number on the way to a user

    user (browser) ──▶ FastAPI (web/server.py)
                          │
                          ├─▶ agent layer (sca/agent/*)
                          │      │
                          │      ├─▶ tools layer (sca/tools/*)
                          │      │      │
                          │      │      ├─▶ multi-RPC pool ──▶ chain RPCs
                          │      │      │     (corroborated, fallback-on-fail,
                          │      │      │      sanity-bounded vs supply_history)
                          │      │      │
                          │      │      ├─▶ OFAC SDN list  ──▶ treasury.gov
                          │      │      │     (multi-URL, hashed, 24h cache)
                          │      │      │
                          │      │      └─▶ attestation HTTP ──▶ issuer pages
                          │      │            (via snapshot store ↔ fallback)
                          │      │
                          │      ├─▶ corpus retrieval (sca/corpus/*)
                          │      │      └─▶ local staged text (sha256-tracked)
                          │      │
                          │      └─▶ synthesis (sca/llm/*) ──▶ DeepSeek API
                          │
                          └─▶ Store (file or Supabase) for persistence

Every external call is wrapped in `sca.observability.timed()` and emits a
structured event (`rpc.call`, `sdn.fetch`, `snapshot.saved`, …). Pipe to
Sentry by swapping `observability.set_sink()`.

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
    sca tokens / sources        registries + inclusion status
    sca curate                  human curation — exclude/include/verify sources, verify addresses
    sca verify [SYMBOL]         auto-verify contracts via on-chain self-report
    sca canary                  re-fetch every external source; report drift
    sca discover                sweep official-source pollers; auto-register new publications
    sca refresh                 re-resolve attestation URLs + run auto-verification
    sca evals                   the eval harness
    pytest                      offline, hermetic Python test suite
    npm test                    JS render-smoke tests (jsdom) — guards against TDZ / silent-blank-panel regressions

### Nightly cron

`sca canary` (drift) and `sca discover` (new publications) are designed for
a daily cron. No internal scheduler — let the OS do scheduling. Example
crontab:

    # 02:30 nightly — re-fetch every external source we depend on
    30 2 * * *  cd /path/to/stablecoin-agent && .venv/bin/sca canary >> data/cron.log 2>&1
    # 03:00 nightly — sweep official sources; auto-register new publications
    0  3 * * *  cd /path/to/stablecoin-agent && .venv/bin/sca discover >> data/cron.log 2>&1

`sca discover` is best-effort: a single failing poller (OFAC / BIS / FSB /
EUR-Lex MiCA / NYDFS / IAASB) logs and continues; new sources are de-duped
by body sha256 against `data/discovered_sources.json`, and a changed body
is registered as a `<id>_v2` revision so old citations stay pinned.

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
the Supabase `curation_votes` table), applied as overrides. The corpus is
**opt-out**: every registered source is citable by default. `sca curate`
(or the F3 Corpus view) lets a human exclude a source, re-include one, or
mark one explicitly verified.

Address verification is **automated where possible**: `sca verify` (also
run by `sca refresh`) asks each contract its own `symbol()` and
`decimals()` and clears the unverified flag on a clean match — and only
on a clean match. Mismatches, unsupported chains (e.g. Solana SPL
metadata), and RPC failures stay flagged for a human to handle through
`sca curate`. A human vote always wins over an auto-verification, so the
curation lever is preserved.

## Reliability

Doré is built on the assumption that everything external can move, throttle,
or lie. Concretely:

- **Multi-RPC pool with cross-validation.** Every chain has fallback
  endpoints. Supply reads (highest-stakes) are corroborated across 2+
  endpoints in parallel; disagreement triggers a third opinion and a
  critical log event. Per-chain consensus is surfaced in the UI.
- **Partial-result gate.** If any chain in a multi-chain token fails to
  read entirely, the total is marked `complete=False` and rendered as
  PARTIAL — never as authoritative.
- **Supply jump detector.** Each read is compared against the last
  persisted value; an implausible swing (≥2× or ≤0.5×) raises a warning
  before the number reaches a "fully backed" judgement.
- **OFAC redundancy.** SDN feed has multiple Treasury URLs; staleness > 7
  days escalates from warn to critical (screening is no longer reliable).
  Every download is hashed for tamper detection.
- **Snapshot store.** Every fetched external URL is archived to disk.
  When a live URL breaks, the UI silently serves the archived copy and
  flags the source loudly so a maintainer can re-anchor.
- **Canary.** `sca canary` sweeps every external dependency nightly
  (RPCs, OFAC, transparency URLs, corpus URLs) and reports drift.
- **Structured logging.** Every external call emits a JSON event via
  `sca.observability` — swappable sink, ready for Sentry.
- **LLM augmentation for context gaps.** When a deterministic source can't
  be resolved, the embedded LLM fills *qualitative* context (backing model,
  attestation cadence, where live data lives) with citations to real URLs.
  Strictly bounded: the LLM **never invents numeric figures** — those come
  only from the deterministic pipeline. Tagged in the UI as "AI CONTEXT".
- **Backing-model classification.** Every token is tagged
  `fiat_reserves` / `crypto_collateral` / `synthetic_delta_neutral` /
  `algorithmic` / `new_or_unverified` so a missing attestation is framed
  correctly — "by design" for crypto-collateralized DAI, not as a gap.
- **User-friendly errors.** Pipeline failures (HTTP 404, JS-gated pages,
  timeouts) produce clean human-readable gap messages; raw exceptions go
  to structured logs for ops, never to the user.

## Status

Three working compliance surfaces · multi-chain supply with cross-RPC
corroboration · resilient attestation locator · deterministic guardrails +
citation verification · Supabase persistence + optional auth · the terminal
web app · the Hermes analyst integration · the corpus flipped to opt-out
(included by default, human excludes / verifies) · snapshot fallback for
every external source · structured logging foundation. **222 hermetic
tests (201 Python + 21 JS).** Pending: deployment, more attestation sources for JS-gated
issuers, staged text for the remaining registry-only sources, a compliance
operator expanding `evals/cases.yaml`, daily auto-discovery of new sources,
LLM fallback ladder beyond DeepSeek.

---

Contributing or working on the code with an AI agent? Read **`AGENTS.md`**.
