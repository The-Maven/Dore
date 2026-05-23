# Doré — audit map

A single document any reviewer (human or agent) can read in ~10 minutes to confirm every external dependency, failure mode, and guard. Built to the principle that *we should be readable by a strong adversarial reviewer*.

## Architecture in one breath

User → FastAPI → agent layer (analyze / screen / assess) → tools layer (deterministic facts) ↔ corpus layer (cited regulatory text) ↔ synthesis (LLM, cited) → typed result → Store (file or Supabase) for persistence.

Five layers, three principles:
1. **Deterministic facts** never come from an LLM. Numbers come from tools.
2. **Every judgement cites** an included corpus passage. Uncited = not allowed.
3. **The honest answer can be "we can't tell"** — that's a finding, not a failure.

## External dependencies + failure modes + guards

| Dependency | Failure mode | Guard | Test |
|---|---|---|---|
| EVM RPCs (publicnode, llamarpc-defunct, native) | 5xx, DNS, throttle | Multi-RPC pool, cross-validate supply, partial-result gate | `tests/unit/test_rpc_pool.py` |
| Solana RPC (mainnet-beta + publicnode) | 429, getnowblock fails | Same pool helper | same |
| Tron RPC (TronGrid) | 429 (frequent on public tier) | Retry+backoff, optional API key, decoded as raw hex (fix for in-place USDT→USDT0 rename) | `tests/unit/test_address_verify.py` |
| OFAC SDN feed | URL rotation, schema drift, staleness | Multi-URL fallback (treasury.gov + ofac.treasury.gov), sha256 hashed, staleness >7d escalates from warn to critical | `tests/unit/test_sanctions.py` |
| Issuer transparency pages | JS-rendered (Paxos), 404, redesign | Snapshot store fallback, augmentation prompt acknowledges *why* the fetch failed, UI surfaces "Coherence note" | `tests/unit/test_snapshots.py`, `tests/unit/test_augment_attestation.py` |
| Corpus source URLs | Site rot | Snapshot store (last-good copy served via `/api/snapshot/<id>`), canary detects drift | `tests/unit/test_snapshots.py`, `tests/unit/test_discovery*.py` |
| DeepSeek API | Outage, slow model, timeout | Fallback ladder (`fallback_llm`): primary → Anthropic if configured → raise visibly. NEVER silent FakeLLM in prod | `tests/unit/test_llm_fallback.py` |
| Discovery sources (17 pollers) | One site changes shape | Per-poller try/except, per-link try/except, sweep continues. Failing poller logs and returns empty | `tests/unit/test_discovery*.py` |

## Coherence guarantees

The audit-grade checks an external reviewer would specifically look for:

1. **No "AI says X, system says ¬X" contradictions.** When attestation can't be fetched, the augmentation prompt is REQUIRED to open by acknowledging WHY (e.g. JS-rendered page). UI renders a "Coherence note" linking the n/a cells to the AI Context card so they read as coherent statements, not contradictory ones. — `web/static/app.js::renderAnalysis` na-bridge block.

2. **Multi-chain provenance is visible, not buried.** Every analyze view shows a `DATA LINEAGE` banner above the metric grid — colour-coded green/gold/amber/rose by corroboration quality. Headlines like *"DATA LINEAGE — 5 chains read · 4 cross-RPC corroborated · 1 single-source"* are above the figures, not in a small column. — `dataLineageBanner()`, tested in `tests/js/helpers.test.js`.

3. **LLM never invents numeric figures.** Augmentation prompts have a STRICT RULE #1 forbidding numeric reserves/supply/coverage. The system prompt is the same across attestation/sanctions/redemption surfaces. Citations are required where applicable.

4. **Production never silently falls back to a fake LLM.** `get_llm()` raises `LLMNotConfigured` when no key is set. `FakeLLM` is reserved for tests, constructed directly. The fallback ladder uses Anthropic as the secondary; on dual failure it surfaces the PRIMARY's error so ops see the right problem.

5. **Unverified addresses don't poison totals.** Default behavior skips unverified deployments from the headline; `allow_unverified=True` (used by the supply endpoint) includes them but flags loudly. Auto-verify (`sca verify`) clears 60 of 67 deployments via on-chain `symbol()`+`decimals()` match; the remaining 7 are Solana SPL (Metaplex not auto-readable) plus 1 known-good alias case (USDT/Polygon → USDT0).

6. **User-facing errors never leak operator commands.** Test `test_unverified_included_but_flagged` asserts that warnings do not contain `sca verify`, `sca refresh`, or `sca curate`.

7. **Case-insensitive symbol lookup.** USDe / USDf / crvUSD are mixed-case registry keys; the API endpoint canonicalises via `config.get_stablecoin(raw).symbol` so any URL casing resolves. Regression guard: `tests/unit/test_symbol_lookup.py`.

## What you should challenge

A strong reviewer should push on these specific weaknesses:

- **No JS test for the model-tier toggle behaviour** beyond unit tests for the helpers. End-to-end would catch a bug where pressing PRO doesn't actually send `tier=deep`.
- **The augmentation web-search path is wired but never exercised** (`SCA_AUGMENT_WEB=1` flag). When turned on, no current test validates that the prompt augmentation actually uses web tools — it's a feature flag with no in-the-loop verification.
- **The health-thread runs in-process** — for a multi-replica deploy, this becomes redundant work or worse, drift. Should move to a single cron OR add a leader-election guard.
- **Snapshot store is local disk only.** S3 (or equivalent) is the real durability path for "the source moved and we still have it" — local disk dies with the box.
- **Eval harness has only a handful of cases** (`evals/cases.yaml`). A compliance product wants 100+ cases covering the matrix of (token × surface × edge case).
- **There is no rate-limit budget per LLM key**. A noisy user can burn the DeepSeek quota; should bucket by user/ip.

## Persistence layer — durability per artefact

Every durable state file goes through `sca.persist.atomic_write_*`, which writes to a same-directory temp file, fsyncs, then `os.replace`s — POSIX-atomic. A crash mid-write leaves the original file intact (proved by `tests/unit/test_persist.py`).

| Artefact | Path | Write helper | Surface restored on startup? |
|---|---|---|---|
| Auto-verify ledger | `data/auto_verifications.json` | atomic_write_json | yes — drives `verified` overlay |
| Supply history | `data/supply_history.json` | atomic_write_json | yes — jump detector baseline |
| Discovery ledger | `data/discovered_sources.json` | atomic_write_json | yes — dedup across runs |
| Snapshot body + meta | `data/source_snapshots/<id>/{body.ext, meta.json}` | atomic_write_bytes/json | yes — fallback for broken sources |
| Health-thread state | `data/health_state.json` | atomic_write_json | yes — flip detection survives restart |
| Health-thread leader lockfile | `data/health_thread.leader` | direct write_text | only advisory; multi-replica leader election |
| Attestation URL cache | `data/attestation_cache.json` | atomic_write_json | yes — saves resolve work |
| Paxos resolver cache | `data/paxos_resolved.json` | atomic_write_json | yes — saves probe work |
| Ingest sha256 state | `data/corpus_ingest_state.json` | atomic_write_json | yes — auto-ingest skips unchanged |
| OFAC SDN XML + hash | `data/ofac/sdn.xml`, `sdn.sha256` | atomic_write_bytes/text | yes — daily refresh cycle |
| Corpus passages | `data/corpus/<source>.json` | atomic_write_json | yes — corpus retrieval |
| Curation votes | (FileStore) `data/votes.yaml` | atomic_write_text | yes — opt-out / verify overrides |
| Curation votes | (SupabaseStore) `curation_votes` table | Postgres txn | yes |
| Analysis history | (FileStore) in-memory map | none — transient | NO (FileStore loses on restart) |
| Analysis history | (SupabaseStore) `analyses` table | Postgres txn | yes |
| Source registry | `corpus/sources.yaml` | direct append (idempotent) | yes |

**Known gap:** FileStore's in-memory analyses are lost on restart. Acceptable for dev/single-user; production must use SupabaseStore (already the default when `SUPABASE_URL` is set).

**Schema-drift fallback:** SupabaseStore.list_sources reads the YAML registry directly (the durable source of truth) so YAML-added sources are visible without DB migrations. `_ensure_source_row` upserts on write to keep the FK constraint on `corpus_passages` satisfied, coercing to legacy statuses (`approved`/`included`-equivalents) until the `0002_corpus_opt_out.sql` migration runs against the live DB.

**Backup story:** the YAML files in `corpus/` and `config/` are git-tracked — version-controlled durability. The `data/` directory is gitignored runtime state; back up with the same backup as the deploy's persistent volume (or copy the snapshot bodies + ledger JSONs to S3 in a nightly job).

## Test inventory (as of this commit)

- **Python: 240 tests** in `tests/` — all hermetic, no network, no DB. Covers: tools, agent, corpus, store, discovery, snapshots, canary, supply pool, augmentation, validation, llm fallback, symbol lookup.
- **JS: 25 tests** in `tests/js/` — jsdom render smoke tests + pure-function helpers. Catches the class of bug that nearly shipped earlier (TDZ in renderAnalysis silently blanked the panel).

Run both: `python -m pytest && npm test`.
