# Doré — Architecture

This document is the canonical map of how Doré works today, and the target
architecture we are building toward (a fused platform with **one verification
core and two lenses**). Read it before changing the spine of the system.

Sister documents: `AGENTS.md` (working rules), `agent/GUARDRAILS.md` (Hermes
boundary), `AUDIT.md` (external-reviewer audit map), `SECURITY.md` (security
posture), `NOTES.md` (autonomous-run change log).

---

## 1. Standing principles — non-negotiable

Violating any of these is a defect, not a style nit. They are the spine of
the product's value (verifiable trust).

1. **The model proposes; deterministic code disposes.** Every number a user
   sees comes from auditable code in `src/sca/tools/`, `validation.py`, or
   `metrics.py`. The LLM is never a source of figures — only of narrative
   framing and citations.
2. **Every fact is traceable to a source.** A verified value carries: the
   claim, the source(s) checked against, a timestamp, a block number (for
   on-chain reads), and a verification status (`verified` / `unverified` /
   `assumed`).
3. **Sanity bounds gate every value** before reconciliation or display.
   Implausible inputs (impossible coverage ratios, decimals mismatches, zero
   or negative supply) are rejected, not surfaced.
4. **`n/a` is a valid, honest answer.** When data cannot be verified, the UI
   shows that plainly. A clearly-labelled gap is a finding, not a failure.
5. **Hermes answers only from the verified fact store.** Cites every figure,
   hedges or refuses when it cannot ground an answer. Cannot exclude or
   verify sources or addresses — that lever belongs to humans.
6. **Conservative posture on claims.** The system surfaces and reconciles
   facts. It never asserts a verdict about an issuer ("under-reserved",
   "fraudulent"). Surface the data; let the user judge.

---

## 2. Today: the as-built architecture (Lens 1 — reserves)

Five layers, deliberate boundaries. Everything below answers one question:
*is this stablecoin's money real?*

```
   ┌───────────────────────────────────────────────────────────────┐
   │  SURFACE              web (SPA) · CLI · MCP (for Hermes)      │
   ├───────────────────────────────────────────────────────────────┤
   │  SYNTHESIS            sca.agent.{analyze,sanctions,           │
   │  (judgement, cited)   redemptions}, sca.augment, sca.brief    │
   ├───────────────────────────────────────────────────────────────┤
   │  GUARDRAILS           sca.validation — Check / Gap;           │
   │  (sanity bands)       sca.metrics computed only here          │
   ├───────────────────────────────────────────────────────────────┤
   │  FACTS                sca.tools — onchain_supply,             │
   │  (deterministic,      attestation_{fetch,extract},            │
   │   no LLM judgement)   sanctions, address_verify, metrics,     │
   │                       paxos_resolver, redemption              │
   ├───────────────────────────────────────────────────────────────┤
   │  PERSISTENCE          sca.store (FileStore | SupabaseStore);  │
   │  + CORPUS             sca.corpus, snapshots, atomic writes    │
   └───────────────────────────────────────────────────────────────┘
```

### 2.1 Facts layer — `src/sca/tools/`

Deterministic. No LLM judgement. Each tool reads an external source, applies
sanity bounds, and returns a typed result with provenance.

| Module | Role | Inputs → Outputs | Guardrails / sanity bounds |
|---|---|---|---|
| `onchain_supply` | Multi-RPC supply reader | symbol → `SupplyResult` (per-chain + total + warnings) | Primary + fallback RPC for every read; 2/3 majority on disagreement; consensus label (`2/2 agree`, `1/2 single source`, `DISAGREEMENT`) surfaces in UI; per-chain supply against cached history triggers warnings if outside [0.5×, 2.0×] prior; 60s cache. |
| `attestation_fetch` | Resolve + download attestation PDF | symbol → `{url, via, local_path}` | Cached URL (25-day max age + HEAD revalidation); seed URL probe; Paxos deterministic WP-CDN probe; LLM HTML locator (last resort). PDF magic-bytes check before accepting. |
| `attestation_extract` | LLM-powered span-grounded extraction | PDF text, symbol, source_url → `Attestation` | LLM constrained to schema; null fields lower confidence; source URL + page numbers retained for citation; LLM treats document as untrusted data. |
| `sanctions` | OFAC SDN screen | `[addresses]` → `[SdnAddress]`, `SdnList` (publish_date, staleness_days) | Two Treasury mirrors with round-robin fallback; SHA256 hashed for tamper detection; staleness escalates `warn` (>2d) → `critical` (>7d). |
| `address_verify` | On-chain self-report identity | (chain, contract, expected_symbol, expected_decimals) → verified bool + signal | EVM: `symbol()` + `decimals()` + non-zero `totalSupply()`. Solana: SPL-Token-Program ownership + decimals + non-zero supply (three structural proofs). Tron: TRC20 self-report match. Zero supply blocks even on symbol match. |
| `metrics` | Pure arithmetic | (reserves, tokens, supply, date) → `Metrics` | `attested_coverage = reserves/tokens`, `live_coverage = reserves/current_supply`, `supply_drift`, `staleness_days`. No LLM, no network. Multi-chain lineage embedded in `provenance`. |
| `paxos_resolver` | Deterministic Paxos URL probe | symbol → URL or None | Pure HEAD probes across a 6-month window of known URL shapes. Cached per (symbol, calendar-month). |
| `redemption` | Reserve-tier classifier | `Attestation` → `[ReserveTier]` (liquid/moderate/illiquid) | Keyword match only. No LLM. |

### 2.2 Guardrail layer — `src/sca/validation.py`

Every value entering reconciliation or display passes a `Check` (returns
`(name, passed, severity, detail)`). Failures become a `Gap` in the user's
view rather than being silently dropped.

| Surface | Checks |
|---|---|
| Attestation | `reserves_positive` (critical), `tokens_positive` (critical), `breakdown_sums_to_total ≤ 2%` (warn), `attestation_date_valid` (critical), `extraction_confidence ≥ 0.6` (warn) |
| Metrics | `coverage_plausible: 0.5 ≤ attested_coverage ≤ 2.0` (critical — outside means data error, not finding) |
| Supply | `supply_resolved: total_supply > 0` (critical) |
| Sanctions | `sdn_list_loaded` (critical), `sdn_list_fresh ≤ 2d pass / 3–7d warn / >7d critical`, `no_sanctioned_addresses` (critical) |
| Redemption | `liquid_coverage_plausible: 0 ≤ x ≤ 2.0` (critical), `reserve_classification_complete ±1%` (warn) |
| Citations | `citations_tool_valid` ⊆ {onchain_supply, attestation_extract, metrics, sanctions, redemption} (warn); `citations_source_valid` ⊆ supplied passages (critical); `figures_traceable` — every numeric `$X,XXX+` in narrative matches an allowed tool figure within 0.5% (critical). |

### 2.3 Synthesis layer — `src/sca/agent/`

| Module | Role | LLM-allowed | LLM-forbidden |
|---|---|---|---|
| `analyze.py` | Orchestrate attestation analysis | Narrative framing, corpus citations | Inventing figures (post-synthesis `verify_citations` enforces this) |
| `sanctions.py` | OFAC screen + narrative | Narrative framing, corpus citations | Verdicts ("safe"/"sanctioned"), figures |
| `redemptions.py` | Liquid-tier assessment + narrative | Narrative framing, citations | Figures, verdicts |
| `synthesis.py` | Skill-prompt + facts + corpus + LLM | Narrative; cited corpus | Numeric figures (rule #1 in prompt); on LLM failure, deterministic narrative composed verbatim from facts |
| `augment.py` | Qualitative gap-filler (when deterministic fetch fails) | Backing-model context, auditor, cadence, cited URLs | Numeric reserves / supply / coverage (system prompt rule #1; schema has no numeric fields) |
| `brief.py` | Editorial top-of-view (`AiBrief`) | Headline + key points + relevant news indices | Inventing figures; rule #1 says "use facts block verbatim" |

**Verified by audit:** no code path allows an LLM to produce a user-facing
displayed number. Synthesis prompts include a facts block built only from
tool outputs; `verify_citations` post-check rejects untraceable figures.

### 2.4 Persistence layer — `src/sca/store/`

A `Store` interface (`src/sca/store/base.py`) with two implementations:

| Store | Backing | Used when |
|---|---|---|
| `FileStore` | YAML + JSON on disk, in-memory for analyses + snapshots | dev, tests (forced), offline runs |
| `SupabaseStore` | Postgres (service role) | production (`SUPABASE_URL` set) |

**Durable artefacts and their writers** (every one goes through
`sca.persist.atomic_write_*` — POSIX-atomic temp+fsync+replace):

| Artefact | Path / Table | Mode | Restored on startup? |
|---|---|---|---|
| Auto-verify ledger | `data/auto_verifications.json` | last-write-wins | yes |
| Supply history | `data/supply_history.json` | last-write-wins | yes |
| Discovery ledger | `data/discovered_sources.json` | last-write-wins | yes |
| Snapshot meta + body | `data/source_snapshots/<id>/…` | last-write-wins | yes |
| Attestation URL cache | `data/attestation_cache.json` | last-write-wins | yes |
| Paxos resolver cache | `data/paxos_resolved.json` | last-write-wins | yes |
| Ingest sha256 state | `data/corpus_ingest_state.json` | last-write-wins | yes |
| Health-thread state | `data/health_state.json` | last-write-wins | yes |
| OFAC SDN file + hash | `data/ofac/{sdn.xml,sdn.sha256}` | last-write-wins | yes |
| Corpus passages | `data/corpus/<source>.json` / `corpus_passages` | last-write-wins per source | yes |
| Curation votes | `votes.yaml` / `curation_votes` | **append-only** | yes |
| Address decisions | (in `votes.yaml`) / `address_decisions` | **append-only** | yes |
| Analyses | `_analyses` dict / `analyses` table | append on create, status-update | FileStore: NO; SupabaseStore: yes |
| Monitor snapshots | `_snapshots` list / `monitor_snapshots` | append, latest-N read | FileStore: NO; SupabaseStore: yes |
| **Attestation URL overrides** | `attestation_url_overrides` (migration 0003) | last-write-wins per symbol, queried newest-first | yes |
| **Verified facts** (time-series) | `verified_facts` (migration 0004) | **append-only**, content-addressed | yes |
| Source registry | `corpus/sources.yaml` | idempotent appends | yes (single source of truth) |

**The immutable time-series fact store is live** (migration 0004, applied).
Every completed analysis writes one `verified_facts` row per (claim_type,
subject) — reserves once per analyze run, supply once per chain — with a
sha256 content hash that lets a repeat reading deduplicate cheaply. Schema
and discipline detailed in §5.

The attestation-URL override table (migration 0003) is what closes the
"curator-set URL survives a redeploy" gap. Both the human curator path
(F8 Compendium → set override) and the canary's auto-discovery path
(every fresh `web_search` / `locator` / `paxos_resolver` resolution)
write through this table. The local JSON cache is now a hot-path speed
layer in front of the durable store.

### 2.5 Web / UI surface — `web/`

| Endpoint | Method | Triggers paid work? | Rate-limit | Auth |
|---|---|---|---|---|
| `/api/config`, `/api/me`, `/api/health`, `/api/history`, `/api/tokens`, `/api/sources`, `/api/snapshot/{id}` | GET | no | no | optional |
| `/api/supply/{symbol}` | GET | RPC reads | **none today** (gap) | none |
| `/api/analyze`, `/api/sanctions`, `/api/redemption` | POST | RPC + LLM | yes (10-burst, 0.5 tok/s per IP) | optional |
| `/api/analyze/{id}`, `/api/sanctions/{id}`, `/api/redemption/{id}` | GET | no (poll) | no | optional |
| `/api/cached/{surface}/{symbol}` | GET | no (Store read) | no | optional |
| `/api/sources/{id}/vote`, `/api/addresses/decision` | POST | store write | no | **required** |
| `/api/attestations/{symbol}/url` | POST | store write | no | **required** |
| `/api/evals` | GET | no (Store read) | no | none |
| `/api/evals` | POST | LLM × N cases | yes | none |
| `/api/compendium` | GET | aggregate read | no | none |
| `/api/compendium/refresh` | POST/GET | canary sweep (background) | no | optional |
| `/api/agent` | POST | LLM + RPC | **none today** (gap) | none |

Rate limiting is per-IP token bucket in `web/rate_limit.py` (capacity 10,
refill 0.5/s). In-memory — single-replica deploys only. Per-LLM-key budget
is the remaining gap (challenge §6 in AUDIT.md).

The SPA in `web/static/` is vanilla JS, no build. It renders figures from
API responses; **it does not compute its own numbers**. Numbers flow from
tools → metrics → synthesis result → JSON → DOM, formatted only.

**View shell — five consistent surface patterns.** Analyze (F2), Sanctions
(F5), Redemption (F6), and Evals (F4) all share the same UX shape:

- **Cached-first render.** On mount each view calls `GET /api/cached/...`
  (or `GET /api/evals` for the suite). Any-age cached result renders
  instantly without spinning a job. The fresh-six-hours window decides
  whether the freshness strip flags it stale.
- **Freshness strip.** "computed Nm ago · RE-RUN" beneath every rendered
  result. Quiet ghost button when fresh; pulsing prominent REFRESH when
  past the 6-hour window. Tooltip carries the per-token expected runtime
  (averaged over the user's last N real runs, persisted in `localStorage`).
- **Staged-run narration.** When a real recompute is needed, the run
  panel shows the named stages with a rotating sub-narration line per
  stage. A live clock reads "8s / ~22s" inside the expected window;
  amber + "still on track" past 1.5×; amber-bold + "Taking longer than
  usual" past 2.5×. A `--scan-pct` CSS variable drives a proportional
  progress fill underneath the animated sweep.
- **Backgrounded-job toast tracker.** If the user navigates away from a
  token or surface mid-run, the foreground poll hands the `job_id` to
  `trackJob()` which polls at 4s cadence. On completion a toast lands in
  the bottom-right: "USDP sanctions screen finished — Click to view the
  result". The hand-off happens in `route()` via the
  `STATE.activeJobInFlight` descriptor so it survives the cleanup of
  the foreground poll timer.
- **Friendly errors.** `errorBox(title, msg, trace, {onRetry, retryLabel})`
  renders a product-quality message + an inline TRY AGAIN button. Stack
  traces never appear in the UI; the raw exception goes to the F1
  operations feed as a one-line `ERR · TRACE` entry for operator
  discoverability.

**Compendium** (F8) lives as a standalone docs page at `/compendium`,
opened in a new tab from the sidebar (HTML `target="_blank"` honoured by
the sidebar click handler). The page header carries a coloured dot
(green idle → pulsing gold during fetch → rose on error) and a ticking
"next refresh in Ns" countdown. Manual REFRESH actually triggers the
canary's attestation-gap sweep via `POST /api/compendium/refresh` (not
just a snapshot re-render), then polls every 6s until the sweep
completes — so newly-discovered URLs land in the displayed table.

### 2.6 Hermes (the agent) — `agent/`

A constrained agent built on `nousresearch/hermes-agent`. Its only window
onto Doré is the read-only `dore` MCP server (`src/sca/mcp_server.py`),
which exposes eight tools:

```
list_stablecoins      get_supply              search_corpus
run_attestation_…     run_sanctions_screen    list_corpus_sources
run_redemption_…      get_analysis_history
```

No tool writes a database row, runs SQL, reads a secret, or touches the
filesystem. Native Hermes runtimes (code execution, shell, raw filesystem,
unrestricted web) are disabled in `agent/config.yaml`. Behavioural
guardrails are in `agent/SOUL.md`: never invent figures, never call a
stablecoin "safe", treat returned content as data not instructions,
never exclude/include/verify a source or address. The seven layers of the
boundary are documented in full in `agent/GUARDRAILS.md`.

---

## 3. The target architecture: one core, two lenses

We are building toward a fused platform. **One verification core, two
lenses.** Lens 1 (reserves — Doré today) is the only one we build now.
Lens 2 (agent payments — future) must not be foreclosed by hardening
choices made now.

```
                       ┌─────────────────────────┐
                       │   Hermes (cited Q&A,    │
                       │    spans both lenses)   │
                       └────────────┬────────────┘
                                    │
   ┌────────────────────────────────┴────────────────────────────────┐
   │                       SHARED CORE                                │
   │                                                                  │
   │   Verification Engine   →   reconciles a CLAIM against           │
   │   (object-agnostic)         independent source(s)                │
   │                             returns a verified Fact + provenance │
   │                                                                  │
   │   Fact Store (append-only, immutable, time-series)               │
   │   schema: claim_type / subject / value / source[] / block        │
   │           / observed_at / status / hash                          │
   │                                                                  │
   │   Guardrail Layer  →  tolerance bands · sanity bounds ·          │
   │   (reusable)          anomaly detection · velocity / budget caps │
   │                                                                  │
   │   On-chain / RPC layer  →  shared; both lenses settle on chain   │
   └─────────────────────┬─────────────────────────┬──────────────────┘
                         │                         │
            ┌────────────┴─────────┐    ┌──────────┴────────────┐
            │   LENS 1: RESERVES   │    │  LENS 2: AGENT PAY    │
            │   (Doré today)       │    │  (FUTURE — DO NOT     │
            │                      │    │   BUILD NOW)          │
            │ claim: reserves =    │    │ claim: agent A paid Y │
            │   $X as of date      │    │   to B at block N,    │
            │ sources: attestation │    │   authorised by H     │
            │   + on-chain supply  │    │ sources: x402 / AP2 / │
            │ views: Monitor /     │    │   AgentCore streams + │
            │   Analyze / Sanctions│    │   on-chain settlement │
            │   / Redemptions /    │    │   + auth records      │
            │   Corpus / Analyst   │    │ views: TBD            │
            │ question: "is this   │    │ question: "can this   │
            │   money real?"       │    │   spender be trusted?"│
            └──────────────────────┘    └───────────────────────┘
```

### 3.1 The one discipline (Phase 0.5)

As we harden Lens 1, keep the Verification Engine, Fact Store, and
Guardrail Layer **object-agnostic** — they must not hardcode "reserves" in a
way that would force a rewrite to add Lens 2. The test: adding Lens 2 later
should be "a new ingestion connector + a new claim-type + new views",
never "rewrite the engine".

### 3.2 Object-agnosticism gap analysis (today)

| Layer | Object-agnostic today? | Notes |
|---|---|---|
| Tools (RPC, fetch, screen) | ✅ Mostly. `onchain_supply`, `address_verify`, RPC pool are generic primitives reusable for any on-chain claim. `attestation_fetch/extract` are reserves-specific (which is fine — they're a connector). | Lens 2 will add new connectors alongside, not rewrite. |
| Validation (`Check`, `Gap`) | ✅ Generic. `Check(name, passed, severity, detail)` and `Gap(area, detail)` are not reserves-specific shapes. | Reusable for budget-cap / velocity / allowlist checks in Lens 2. |
| Metrics (`Metrics` dataclass) | ⚠️ Field names are concrete (`attested_coverage`, `live_coverage`, `supply_drift`). | Fine for Lens 1. Lens 2 metrics will be a separate dataclass (different shape: spend-rate, deviation-from-budget, authorisation-link), reusing the same `Check`/`Gap` model — not a refactor of `Metrics`. |
| Synthesis (`Analysis`, `SanctionsScreen`, `RedemptionAssessment`) | ⚠️ Surface-specific. Each is a typed result for one Lens-1 surface. | Same pattern works for Lens 2: new typed result per agent-payment surface, sharing the synthesis primitive `synthesize_surface(skill, facts, passages)` which IS generic. |
| Fact Store | ❌ **Today: latest-snapshot per claim, not append-only time-series with a generic shape.** Reserves facts live in `analyses.result` (jsonb) and `monitor_snapshots`; both are point-in-time. | This is Phase 2's primary deliverable. New table `verified_facts` (append-only, generic schema) is built alongside without altering existing tables. See §5. |
| Guardrail Layer | ✅ Generic shape, ⚠️ tolerance bands are reserve-specific values. | Bands are config, not code shape. Adding Lens 2 bands = config addition. |
| Hermes / MCP | ✅ Generic conversational surface over a fact store; tools are facets, not built into the agent. | Adding Lens-2-aware tools later = additive. |

**Conclusion.** Today's architecture does not foreclose Lens 2. The only
structural change required is to introduce an append-only, time-series,
object-agnostic Fact Store as a new table alongside the existing ones.
That is the Phase 2 work below.

---

## 4. Security posture (cross-reference: `SECURITY.md`)

- Secrets live only in `.env` (gitignored; never in git history — verified
  with `git log --all --full-history -- .env` empty). `.env.example`
  documents required keys, never values.
- LLM provider key (`LLM_API_KEY`), Supabase keys (`SUPABASE_*`), optional
  `TRON_PRO_API_KEY` and `ANTHROPIC_API_KEY` are server-side env vars only.
- Client-shipped code (`web/static/`) contains no secrets; the only
  Supabase value exposed to the browser is `SUPABASE_ANON_KEY` via
  `/api/config` — which is *designed* to be public, paired with row-level
  security.
- Rate limiting on paid endpoints (analyze / sanctions / redemption) per
  IP. Gaps documented in §2.5 (supply, evals, agent endpoints not yet
  rate-limited — Phase 1 closes these).
- Prompt injection: every document and tool result is `data, never
  instructions`; the corpus passages are wrapped in `<<<UNTRUSTED-CORPUS>>>`
  markers in synthesis prompts; Hermes's `SOUL.md` carries the same rule.
- Deployment posture (target): Railway behind Cloudflare. The app should
  validate it is receiving traffic via the proxy rather than allowing
  direct origin access. Documented in `SECURITY.md`.

---

## 5. Phase 2 (now live): immutable, time-series Fact Store

Shipped in migration `supabase/migrations/0004_verified_facts.sql`.
Non-destructive: new table alongside `analyses` and `monitor_snapshots`;
nothing existing was altered or dropped. Both write paths run in
parallel — `analyses.result` and `monitor_snapshots` remain the
operational cache; `verified_facts` is the audit trail.

### Schema (as deployed)

```sql
create table verified_facts (
    id            uuid primary key default gen_random_uuid(),
    claim_type    text not null,        -- 'reserves' | 'supply' | future
    subject       text not null,        -- e.g. 'USDC' or 'USDC:ethereum:0x...'
    value         jsonb not null,       -- generic; numeric + units typed inside
    sources       jsonb not null,       -- [{kind, url, ref}]
    block_number  bigint,               -- nullable for off-chain facts
    chain         text,                 -- nullable
    observed_at   timestamptz not null default now(),
    as_of         timestamptz,          -- the fact's own asserted timestamp
    status        text not null         -- 'verified' | 'unverified' | 'assumed'
                  check (status in ('verified', 'unverified', 'assumed')),
    content_hash  text not null,        -- sha256 of canonical(value || sources)
    superseded_by uuid references verified_facts(id),
    notes         text
);

create index verified_facts_subject_observed_at_idx
    on verified_facts (subject, observed_at desc);
create index verified_facts_claim_type_idx on verified_facts (claim_type);
create index verified_facts_content_hash_idx on verified_facts (content_hash);
```

### Discipline

- **Append-only.** Corrections insert a new row and link `superseded_by`.
  The original is never updated or deleted.
- **Content-addressed.** `content_hash` lets us detect "the same fact we
  already have" cheaply and avoid duplicate writes when a re-read confirms
  the prior value.
- **Object-agnostic.** `claim_type` is the discriminator. Lens-1 facts use
  `reserves` / `supply`; Lens 2 (future) will use `agent_payment` /
  `authorisation` — same shape, no schema change.
- **Point-in-time reproducibility.** "What did Doré report for USDC on
  2026-04-30?" is a SQL window query: latest fact per claim_type+subject
  with `observed_at <= '2026-04-30'`.

### Writer

`_persist_analysis` in `src/sca/agent/analyze.py` writes through on every
completed analyze run: one `reserves` claim from the extracted attestation
plus one `supply` claim per chain. Best-effort: a Store hiccup logs a
warning but never breaks the user-facing analysis result.

### Companion migration: 0003 attestation URL overrides

`supabase/migrations/0003_attestation_url_overrides.sql` adds a
last-write-wins table queried newest-first. Curator-set URLs (via
`POST /api/attestations/{symbol}/url`) and canary-discovered URLs (every
fresh `web_search` / `locator` / `paxos_resolver` resolution, via
`_record_override` in `attestation_fetch.py`) write through it. The
`set_by` column is a typed UUID — anonymous curator/canary writes pass
`None`, signed-in curator writes pass the Supabase user id.

---

## 6. Tests and the contract

The test suite is the contract: `.venv/bin/python -m pytest -q` must stay
green and hermetic (offline, no network, no DB; the suite forces FileStore
regardless of `.env`). New durable state means a new Store method and a new
migration, plus a test that exercises both FileStore and SupabaseStore
shapes — never an ad-hoc write.

Current baseline (this commit): 291 Python tests + 30 JS tests = 321
hermetic tests passing.

---

## 7. Quick reference

```
# Tests (must stay green, must stay hermetic)
.venv/bin/python -m pytest -q

# Web app
.venv/bin/python -m uvicorn web.server:app --port 8000

# CLI
.venv/bin/sca analyze USDC
.venv/bin/sca verify              # auto-verify supported deployments
.venv/bin/sca discover            # corpus discovery sweep

# Hermes (MCP server)
.venv/bin/python -m sca.mcp_server
```
