# Doré — autonomous work notes

> **Chronological work log.** Each round below is a snapshot of one
> autonomous session at the time it was written, preserved for
> archaeological context. The canonical current state lives in
> `ARCHITECTURE.md`, `README.md`, and `AGENTS.md` — read those for
> what the system actually does today. The "Where we stand right
> now" block at the end of this file always reflects today.

User stepped away ~05:50 UTC with a broad mandate: fix everything, audit-grade, AI as wedge, corpus enrichment, un-stale data. This file documents what I'm changing and why, so you can scan my reasoning when you're back instead of reading the diff cold.

## The honest gap analysis I'm working from

What an external auditor (e.g. GPT-5) would call out today:

1. **AI-context-vs-n/a contradiction.** Analyze view for Paxos shows "AI: Paxos publishes monthly attestations via Withum" right next to "ATTESTED COVERAGE: n/a" with no bridge between them. Fix: augmentation prompt now demands the LLM open by acknowledging WHY the automated fetch failed; UI adds a "Coherence note" linking the n/a to the AI context.

2. **Multi-chain corroboration is happening but invisible.** Per-chain consensus is in a small table column. Users don't see at a glance that the headline figure came from 6 chains with 5 cross-RPC corroborations. Fix: prominent DATA LINEAGE banner under every computed headline.

3. **AI is one card per surface — should be woven in.** Right now AI Context is a single block. Better: per-figure tooltips (`?` icon next to each metric → AI explainer on hover), proactive insights when something is unusual.

4. **Corpus is registered but not ingested.** 17 discovery pollers exist but haven't been run; corpus is mostly empty of staged text. Fix: run `sca discover` to populate; document the cron cadence for keeping it fresh.

5. **No LLM fallback.** DeepSeek is single point of failure. The user said "we'll sort that out later" but it's still task #76 open. Building it now since the user is afk and wants completeness.

6. **No visible audit trail for "when was this last checked".** Sources have snapshot_age_days but it's only in the API. Should surface in the UI per cell.

## Plan, in priority order

1. Coherence bridge between AI Context and n/a cells (IN PROGRESS)
2. Multi-chain DATA LINEAGE banner (prominent, not buried)
3. LLM fallback ladder — DeepSeek → Anthropic → "synthesis unavailable, facts only"
4. Run discovery sweep to actually populate corpus
5. Per-figure AI explainer tooltips
6. Self-audit document (`AUDIT.md`) — explicit map of every external dependency, failure mode, and the test that guards it
7. Browser-verify the analyze view end-to-end
8. Final test pass — Python + JS

---

## What landed (autonomous run summary)

**Coherence + audit grade**
- Augmentation prompt rewrites: REQUIRES the LLM to open by acknowledging WHY the automated fetch failed, so "AI says attestation exists" and "n/a" read as coherent statements.
- UI `na-bridge` block: when the cell is n/a AND an AI Context card is present, an explicit "Coherence note" links the two so a user (or auditor) never reads them as contradictory.
- `dataLineageBanner` — prominent banner above metric grid: "DATA LINEAGE — 6 chains read · 4 cross-RPC corroborated · 2 single-source". Colour-coded green/gold/amber/rose by corroboration quality.
- Browser-verified: USDC analyze view renders 6 consensus chips, lineage banner, backing strip, all per spec.

**Fixed: discovery + persistence divergence**
- SupabaseStore.list_sources was reading from its own table — never saw YAML-added sources. Aligned to read YAML (single source of truth).
- Discovery `propose_source` chain was failing FK constraint on corpus_passages. Added `_ensure_source_row` upsert that handles the legacy schema (CHECK constraint predates opt-out migration — coerced to legacy statuses until the live DB runs 0002).
- Discovery sweep now lands 41 new corpus sources cleanly.

**Production LLM hardening (#76 done)**
- `sca.llm.fallback.fallback_llm()` — DeepSeek → Anthropic → re-raise primary error. Used by synthesis + all augmentation surfaces.
- Production NEVER silently falls back to FakeLLM — raises `LLMNotConfigured` visibly when no key.
- 7 new tests in `test_llm_fallback.py`.

**Corpus enrichment + de-staling**
- Discovery sweep ran successfully: 41 new sources (FSB publications, OFAC recent actions, ECB digital-euro, FSB RSS, Chainalysis blog) auto-ingested.
- Total registered sources: 13 → 54.
- README documents the daily cron entry; in-process `health_thread` runs every 6h to detect live↔broken flips.

**AUDIT.md**
- Single-document map an auditor (or strong agent like GPT-5) can read in 10 min to verify every external dependency, failure mode, and the test guarding each. Includes a "What you should challenge" section naming our known weaknesses honestly.

**Tests**
- Python: **240 passing** (+6 symbol-lookup, +7 llm-fallback, +33 discovery/health from parallel agent)
- JS: **25 passing** (+4 lineage banner tests, +existing render + helpers)
- Both run hermetic, offline.

**Honest open items**
- Per-figure AI tooltips (#80) — wired toggle exists but per-cell hover explainers not done. Worth the next round.
- DB schema migration (`0002_corpus_opt_out.sql`) not run on live Supabase — code coerces to legacy statuses; should be applied during next maintenance.
- Snapshot store still local-disk — S3 / Supabase Storage is the real durability path.
- Eval harness still thin (1-2 cases per surface).

---

## Round 3 (autonomous, coffee-break)

**Multi-user bulletproofing**
- Bounded `ThreadPoolExecutor(max_workers=4)` for jobs — no more thread-per-request spawning under load.
- Auto-eviction: jobs older than 1h pruned each enqueue so `_JOBS` never leaks.
- Per-IP token-bucket rate limit on /api/analyze, /api/sanctions, /api/redemption. 10-burst, 0.5 tok/s refill, 429 with Retry-After. Tested.
- Health-thread leader election via PID lockfile — multi-replica deploys (uvicorn --workers N) no longer all run the sweep redundantly. Stale-after-10min reclaim.
- `LLMNotConfigured` already raises visibly in prod; FakeLLM is test-only.

**AI as the wedge**
- `sca.brief.generate_brief()` — editorial top-of-view synthesis. Headline + key points + relevant news (drawn from real corpus discovery, cited URLs). Bounded prompt: NEVER invents numbers; news indices reference a candidate list so the LLM can't fabricate sources.
- Wired into analyze + sanctions + redemption surfaces. `Analysis.brief`, `SanctionsScreen.brief`, `RedemptionAssessment.brief` carry a serialised dict.
- `aiBriefHero` JS helper renders the panel: DORÉ BRIEF badge, big editorial headline (Bodoni Moda), key-point list with gold ◇ markers, "RELEVANT IN THE CORPUS" cited news rows, AI-composed footer. Distinct gold-bordered hero panel above everything else.
- Verified live for PYUSD: headline "PYUSD lacks a current attestation; supply figures rely on on-chain data, some provisional." + 3 key points + 3 news items (GENIUS Act, FSB, MiCA).
- Verified live for DAI (crypto-collateral): headline "No recent attestation available for DAI; total supply stands at 4.35B across five chains."

**Backing-model-aware n/a copy**
- DAI/USDe/GHO (crypto-collateral) now read "BACKING — ON-CHAIN COLLATERAL" with `∞` glyph, not the dead-end "No attestation could be resolved".
- USDe/USDf (synthetic) reads "BACKING — DELTA-NEUTRAL POSITIONS" with `◇`.
- USDD (algorithmic) reads "BACKING — HYBRID ALGORITHMIC" with `⌬`.
- Each carries a working "view live dashboard ↗" link to the protocol_url so users have a real escape hatch.

**Durable Paxos resolver**
- `sca.tools.paxos_resolver` — probes the 6 most recent plausible URL patterns for PYUSD/USDP/USDG. HEAD with auto-fallback to ranged GET (handles WordPress quirk).
- Cached per (symbol, calendar-month) to avoid re-probing.
- Wired into `attestation_fetch.resolve_url` as step 3, before the LLM-driven HTML locator falls back. Eliminates manual fetch for the 3 Paxos tokens.
- +14 tests, hermetic.

**Mobile pass (delegated agent)**
- Horizontal-scroll snap carousels for `.mgrid`, `.dtable`, `.strip`, `.aug-cites`.
- F-key sidebar collapses to horizontal scroll-snap strip on mobile (no hamburger needed — terminal aesthetic preserved).
- Touch targets ≥ 32-44px on all interactive elements.
- Topbar compresses at ≤500px (only SESSION + TOKENS visible).
- Coverage row stacks vertically, `.lineage-banner` wraps cleanly.

**Light/dark theme toggle**
- Sun/moon icon in topbar; flips `[data-theme="light"]` on `<html>` and persists to localStorage.
- Light palette: cream backgrounds (#F5F1E8), darker brass gold (#8B6720) for AA contrast, scanline overlays suppressed.
- Theme applied BEFORE DOMContentLoaded to avoid white-flash on dark-mode reloads.
- 3 JS tests guard the toggle behaviour.

**Synthesis bulletproofing**
- "No narrative synthesised" eliminated. Both `synthesize()` and `synthesize_surface()` catch LLM exceptions AND empty returns, fall back to a deterministic narrative composed from structured facts. Honest "Auto-composed because LLM was unavailable" footer.
- 9 new tests for the fallback.

**Tests**
- Python: 269 passing (+27 from round 2)
- JS: 30 passing (+5: theme + AI Brief + others)
- Total: **299 hermetic tests**.
- Run both: `python -m pytest && npm test`.

---

## Round 4 (Solana + Tron explorers + persistence hardening)

**Solana now auto-verifies (5 mints cleared).** The "unsupported-chain" framing is gone. New verifier reads through the SPL Token Program: `getAccountInfo` proves the address is owned by the canonical SPL Token Program (or Token-2022), the parsed mint payload's `decimals` must match the registry, and `getTokenSupply` must be non-zero. Three structural checks make the verification roughly as strong as an EVM `symbol()` match without requiring ed25519 PDA derivation. The `auto: SPL mint + decimals match` label is distinct from `auto: on-chain symbol match` so a reviewer sees the proof shape at a glance. Live verify summary now reads **65 auto-verified, 0 unsupported, 0 errors**.

**Tron explorer links added.** Per-chain table rows for Tron deployments now link to `tronscan.org/#/contract/{address}` — same credibility level as the etherscan token pages we link for EVM chains. Also added bsc → bscscan, avalanche → snowtrace.

**Persistence hardening (#94).** Built `sca.persist.atomic_write_*` and migrated every durable-state write site: auto-verify ledger, supply history, discovery ledger, snapshot meta + body, attestation cache, Paxos resolver cache, ingest sha256 state, health-thread state, OFAC SDN file + hash, FileStore votes + corpus passages. Pattern: temp file in same directory → fsync → `os.replace` (POSIX atomic). A crash mid-write now leaves the original file intact (7 new tests prove it). AUDIT.md gained a per-artefact durability table.

**Honest open items (revisited)**
- DB schema migration `0002_corpus_opt_out.sql` still not run on live Supabase — code coerces to legacy statuses during the gap.
- Snapshot store still local-disk — S3 / Supabase Storage is the next durability tier.
- Eval harness still thin (1-2 cases per surface).
- Per-figure AI tooltips closed as superseded by the AI Brief + AI Context patterns (incremental, not transformative).

**Final tally**
- Python: 279 passing
- JS: 30 passing
- **309 hermetic tests** total.

---

## Round 5 (UX polish push + persistence-layer completion)

A multi-day collaborative session focused on closing operator-pendings
and bringing the user-facing surface to SF-grade polish. Reflects the
state at session end.

**Persistence layer — fully closed.**
- Migration `0003_attestation_url_overrides.sql` applied to live
  Supabase. Curator-set URLs and canary-discovered URLs now persist
  across redeploys via `_record_override` writing through the store.
  `set_by` column is a typed UUID — anonymous writes pass `None`.
- Migration `0004_verified_facts.sql` applied. The immutable
  time-series fact store is live in production; `_persist_analysis`
  writes one `reserves` claim + one `supply` claim per chain per
  completed analyze run, content-addressed via sha256.
- Migration `0002_corpus_opt_out.sql` finally applied. The status
  CHECK now enforces `{included, excluded}` exclusively. The legacy
  status-coercion shim in `_ensure_source_row` (which had been
  mapping every status to `approved` to satisfy the pre-0002 CHECK)
  is stripped. Status flows through verbatim from the YAML. Tier
  coercion stays in place — migration 0002 didn't touch the tier
  CHECK and `tier1_official` / `tier2_industry` from auto-discovery
  still need to coerce to `research`.

**Discovery / data quality.**
- Three-hop attestation discovery chain (Brave → DDG → domain-scoped
  follow-up) with origin filter rejecting unknown S3 buckets, retry
  on transient errors. Canary auto-resolves 11/14 fiat tokens.
- Web search proposes → DB caches/disposes → YAML bootstrap-only.
- Empty attestation-date crash fixed in `tools/metrics._as_date`
  (returns `None` instead of raising on blank); guardrails surface
  the missing field as a proper gap instead of a 500.
- Test-fixture leak in `corpus/staging/` traced + plugged
  (`isolated_registry` now hermetic — was writing fixture markdown
  into the live corpus).

**LLM voice + prompt hygiene.**
- Em-dashes purged from `brief.py` + `augment.py` prompts (LLM was
  picking them up); explicit voice rule (no em-dashes, banned
  marketing words) added.
- `_backing_model_brief` cleaned in both modules (duplicate missed
  first round).
- "secondary references" framing replacing the harsher "untrusted".
- Verified live: USDC / AEUR / PYUSD all emit em-dash-free briefs.

**Empty-state copy rewrite.**
- "ATTESTATION — FETCH UNAVAILABLE / n/a / could not be resolved —
  see GAPS" replaced everywhere. The AI Context serves the answer; a
  quiet `<details>` disclosure tucks deterministic placeholders; a
  dashed-rule footnote names the raw-parsing limitation without
  leading the page. Same pattern across analyze + redemption +
  sanctions.
- Mcell values switched from `n/a` to `—`; hover notes neutralised
  ("see AI Context above", "populates when an attestation resolves").
- Stack traces no longer visible in the UI; `friendlyErrorCopy`
  stops leaking raw exception text; one-line `ERR · TRACE` entry to
  F1 feed preserves operator discoverability.

**Snappier UX — cached-first render across all four result surfaces.**
- New `GET /api/cached/{surface}/{symbol}` returns any-age cached
  result. `renderCachedOrRun` mounts analyze / sanctions / redemption
  from cache instantly (~1s, no spinner).
- Evals view aligned: `GET /api/evals` cached, `POST /api/evals`
  runs. `freshnessStrip` extended to evals; latency UX matched.
- Sidebar token-clicks respect the current surface
  (`currentSurfaceHash`) instead of always bouncing to analyze.

**Latency UX.**
- Per-(surface, symbol) `localStorage` memory of last 8 real
  durations. Clock shows "8s / ~22s"; past 1.5× → amber + "A little
  past typical, still on track"; past 2.5× → "Taking longer than
  usual. Still working".
- Scanbar gains a real progress fill driven by elapsed/expected.
- RE-RUN tooltip ends with "A recompute typically takes about N
  seconds (averaged over your last X runs)".

**Backgrounded-job toasts.**
- When the user switches token or surface mid-run, `route()` hands
  the job off to a 4s background tracker via the
  `STATE.activeJobInFlight` descriptor (this is the fix for a bug
  where `route()`'s `clearInterval(surfacePollTimer)` was killing
  the poll before its abort path could fire `trackJob`).
- Toast lands in bottom-right on completion: "USDP sanctions screen
  finished — Click to view the result". Click jumps to the surface;
  auto-dismisses after 12s. Failed jobs surface as a rose-bordered
  "Click to retry" toast.

**Compendium standalone page.**
- Lives at `/compendium`, opens in a new tab from the sidebar
  (sidebar click handler respects `target="_blank"` + modifier keys).
- Live-feeling header: green dot idle → pulsing gold during fetch →
  rose on error; "next refresh in Ns" countdown.
- Manual REFRESH actually triggers the canary's gap sweep via
  `POST /api/compendium/refresh` (not just a snapshot re-render),
  polls every 6s until done.

**Type-scale + mobile.**
- 9+ sub-floor CSS labels lifted from 8.5/9/9.5px → 10/10.5/11
  across the whole UI.
- WCAG AA contrast pass on 4 measured defects.
- Mobile button rhythm standardised (40px touch height for all
  primary buttons); freshness strip wraps cleanly instead of yawning
  empty band.

**Eval surface.**
- `humanizeEvalPoint()` maps every check kind to a sentence
  ("Live supply resolved from on-chain reads", 'Expected gap
  mentioning "X" was reported', etc.).
- Running state gets stage narration + scanbar + latency UX like the
  other result surfaces.

**Decisions closed in this round.**
- **Headless browser for JS-rendered issuer pages (USDP, USDG, EURC):
  do not build.** Documented as closed in `AGENTS.md`. The AI Context
  card already handles these tokens well; a ~200MB Chromium dep + a
  separate worker process + retry/timeout surface + selector-drift
  failure mode is not worth three tokens. Reopens only if the
  JS-rendered set grows materially.
- **Stack traces never appear in user-facing UI.** Operator-mode
  discoverability via the F1 feed only.

**Tests at session end**
- Python: 291 passing
- JS: 30 passing
- **321 hermetic tests** total.

---

## Round 6 (F9 SIMULATOR — predict, attribute, score, narrate)

User stepped out for cinema then bed across two sessions; mandate was
"build a coin movement simulator with predict-every-10-min cadence,
class-defining product, use AI as the wedge, treat data with
sophistication." Returned later with "I'm not happy with the results
— go research SF-class design, make it dynamic, hues of ticker
symbol colors, more data on screen, fast-paced, do it autonomously
until presentable at YC." Returned again with: mobile + readability
+ AI commentary per token + fix tokens with no data + expand the
universe + freshness. Documented here as one continuous arc because
the architecture stayed coherent across the rounds.

### Phase 1 — foundation
- **Migration 0007 (`movement_simulator.sql`)**: `peg_ticks` /
  `predictions` (append-only archive) / `resolutions` (1:1 via
  unique constraint, enforced in DB). RLS-aligned with existing
  pattern.
- **Migration 0008 (`peg_ticks_consensus.sql`)**: additive columns
  on `peg_ticks` — `consensus_kind`, `sources` jsonb,
  `max_disagreement_bps` — for multi-source ground truth.
- Wired `save_snapshot` on `/api/supply/{symbol}` so the existing
  F1 monitor poll now accretes a time-series the forecaster reads
  from. `save_snapshot` was already defined in `Store` but had no
  callers — the plumbing was dormant.
- New module `sca.peg_price` — Coinbase v2 spot adapter with 20s
  cache (was 60s; tightened in v3.1 for live feel).

### Phase 2 — the four-job pipeline
- `sca.movement.predict` — deterministic EWMA + asymmetric
  volatility cone. Two prediction kinds for v1: `peg_deviation`
  (bp) and `net_flow_direction` (probability + magnitude). IPCC
  ladder words mapped to band-half-width. **Structurally
  incapable** of emitting `virtually_certain` because the
  prob_positive clamp caps at 0.94 — honest hedging baked in.
- `sca.movement.resolve` — strictly-proper scoring. Brier for
  binary, normalised miss distance (NOT named CRPS until the
  audit caught it) for continuous. Outcome banding inside_p50 /
  p80 / p95 / outside. Persistence + climatology baselines on
  every row so model "skill" is measurable, not just "accuracy".
- `sca.movement.attribute` — cited drivers from observability
  events + corpus passages, trust-tier per source.
- `sca.movement.judge` — LLM voice over the math. Synthesis +
  insight + pitch. Strict guardrails (no em-dashes, no weasel
  words, no future-tense pitching) post-stripped with audit
  logging. Forged `[n]` / `(Wn)` citation indices stripped if
  they don't match the verified list.
- `sca.movement.ticker` — single background thread. Re-reads
  config every cycle. SCA_MOVEMENT_TICKER_DISABLED env var gates
  tests.
- `sca.movement.brave_context` — Brave web context for the judge
  with **five layers of cost discipline**: 12h TTL with ±20%
  jitter, coalesce per-symbol (one query both kinds), dual
  interest gate (skip when calm AND near-zero), daily quota cap
  (200/day default), stale-on-error. Verified live: BURST ×6 = 0
  new Brave calls.

### Phase 3 — API + first UI
- Endpoints: `/api/simulator/state`, `/predictions`, `/calibration`,
  `/timeline`, `/feed`, `/config` (GET/POST, auth-gated), `/tick`
  (auth-optional per saved operator preference).
- v1 UI: per-token panel stack with fan chart + judge carousel +
  vertical pulley. Worked but felt static; user pushed back.

### Phase 4 — Bloomberg-class redesign (v2 → v3)
Driven by two 30-min research agents pulling from Bloomberg
Terminal, Pyth Insights, Artemis, Linear, Datadog, TradingView,
Stripe Dashboard, FT/Economist data viz.

- Five-color contract: amber default, `#4AF6C3` up,
  `#FF433D` down, blue identifier, orange warning. Brand colour
  per ticker is a LABEL ONLY — never a chart line. Prevents the
  "20 tokens overlaid = rainbow vomit" trap.
- **Bloomberg ribbon** at the top: two-row scrolling tape (cells
  with brand pill + symbol + value + colored chevron delta +
  inline sparkline; peg-deviation ±25bp tracks below — the Doré-
  unique view; no competitor renders peg deviation as a live
  ribbon track).
- Six-cell status strip (LIVE / LAST TICK / CADENCE / PEG SOURCES
  / BRAVE QUOTA — sources cell added in v3.1).
- Three-column workspace: LEFT rail (token list with sparklines,
  sorted by |1m delta| descending, empty rows fade to 45%) /
  CENTER hero (brand-tinted Bodoni headline + big sign-tinted
  number + 5-cell delta grid 1M/5M/1H/24H/7D + line chart with
  forecast cone + AI Judge card) / RIGHT rail **THE WIRE**
  streaming event feed with 4-letter glyphs (TICK / KICK / CACH /
  BRAV / SKIP / RESV / DISP / SLNT / JUDG / SCHM / etc) + vertical
  pipeline pulley (PREDICT → ATTRIBUTE → SCORE → NARRATE with a
  gold marker that slides on each tick).
- **SSE primary channel**: `/api/simulator/stream` pushes a
  snapshot on connect, a diff `tick` event every 2s, a
  `heartbeat` every ~10s idle. Page polls `/feed` every 20s as
  reconciliation.
- Cell flash 700ms on tick diffs (green up / red down then decay
  to amber). Wire row fade-in 280ms cubic-bezier. Heartbeat dot
  1.4s sine (never faster per research).

### Phase 4 audit (external research-grade)
Brutal self-audit found 18 issues; closed all reversible ones:
- P1 — `crps_from_band` renamed to `normalised_miss_distance`
  (was misleadingly named; the math is normalised L1, not real
  CRPS). Column stays `crps_score` for schema stability.
- P2 — persistence baseline was secretly identical to climatology;
  rewritten to look back one horizon-window for the actual prior
  direction. Climatology is now empirical (reads recent positive-
  rate) with a 5-row min before falling back to 0.5.
- P3 — five judge bypasses closed: NFKC unicode normalisation,
  expanded weasel list, voice-rule strip logged not silent,
  per-field length caps enforced, forged-citation index validation.
- #5 — variance snap-to-1.0 now annotated in the row's `notes`.
- #6 — clamp-cutoff comment fixed to name the actual IPCC cutoff.
- #7 — **multi-source peg** (migration 0008): Coinbase + Kraken
  in parallel, 5bp agreement gate. Disputed ticks land in the
  archive with a "ground truth contested" tail in the resolution
  narrative. Closes the "single point of failure for the
  calibration archive" finding.
- #8 / #12 — `threading.RLock` around FileStore simulator lists;
  `fcntl.flock` around Brave quota read-modify-write so two
  uvicorn workers can't both read N and burn two calls counted
  as one.
- #9 — `max_in_flight_per_symbol` was dead config; now enforced
  before the cycle's LLM + Brave layers fire.
- #10 — `_CALM_ORDER` ambiguous list replaced with explicit
  `_CALM_RANK: dict[str, int]`.
- #11 — Brave interest gate is dual: skip ONLY when both calm
  word AND |point| ≤ 5bp. Closes the "slow-attack" failure mode
  where a tight cone hides a drifting point.
- #13 — web-context dicts carry `fetched_age_s` + `served_via` so
  the judge sees how stale a citation is.
- #14 — `_summarise_event` strips `[n]` / `(Wn)` from external
  titles before they enter the prompt (prompt-injection defense).
- #15 — honest n/a everywhere: peg_deviation narrative handles
  `point=None` gracefully; calibration `{unavailable: True}` is
  distinct from `{count: 0}`.
- #17 — BoE "two-piece asymmetric" claim removed from docstrings;
  v1 emits symmetric bands; schema supports asymmetric for a
  future model version.
- #18 — `schema_missing` detection pushed into the SupabaseStore
  layer so direct callers (agent bridge, future tools) see the
  same honest signal the API wrapper provides.
- Bug caught by the planted-miscalibrated-model reliability-bin
  test: empirical-rate computation was conflating band-
  containment with direction. Fixed in both backends.

### v3.1 — CoinGecko, AI Commentary, mobile, freshness
- **CoinGecko adapter** (tier-3 after Coinbase + Kraken). Free,
  no auth. Live result: 6/12 → 15/18 tokens with data, three
  active sources per consensus row. Closed the DeFi-native gap
  (FRAX, GHO, crvUSD, LUSD, USDe, USDD, etc).
- Case-insensitive symbol lookup in the CG ID map (config sends
  `crvUSD`, map had `CRVUSD` — was returning None silently).
- **Token universe expansion**: USDS (Sky), RLUSD (Ripple), USDM
  (Mountain) live; brand-color palette extended to sUSDe, BUIDL,
  USDtb, syrupUSDC, USD0, OUSG, deUSD, USR, USDB, USYC. Researcher
  noted: USDY / sUSDe / syrupUSDC drift above $1 by design (yield-
  bearing); their per-token cone thresholds were set wider.
- **`sca.movement.token_context`** — structural cheat sheet per
  token (20 entries): issuer, backing model, attestation cadence,
  auditor, transparency URL, watchlist signal, cone normal/alert
  thresholds, one-liner. Case-insensitive registry lookup across
  all keys (mixed-case `crvUSD` / `sUSDe` always resolve).
- **`sca.movement.commentary`** — LLM Commentary generator.
  Reads cheat sheet + live cone + 1h delta, emits JSON
  `{headline, body}` with inline `[n]` citations. 1h cache per
  regime bucket so chip clicks don't burn LLM budget.
  Deterministic fallback when LLM unavailable: cheat sheet body
  alone, honest "n/a" framing.
- New `/api/simulator/commentary/{symbol}` endpoint. Light-weight
  (reads only the focused token's latest tick + prediction, not
  the full feed). Response: `headline`, `body` (markdown with
  `[n]` citations), `citations[]`, `structural_one_liner`,
  `cone_normal_bps`, `cone_alert_bps`, `model`.
- UI: new gold-bordered AI Commentary card under AI Judge in the
  hero pane. Verb-named disclosure ("AI COMMENTARY · grounded in
  cited sources"). Inline `[n]` rendered as gold superscript
  links. SOURCES footer + "AI-generated summary. Verify before
  acting." disclaimer.
- **Mobile redesign**: `@media (max-width: 900px)` stacks the
  workspace, hides the ticker tape (redundant with rail), 2-col
  status strip. `(max-width: 480px)` tightens further.
  `(pointer: coarse)` enforces 44pt tap targets (Apple HIG /
  Hoober thumb-zone). Researcher-cited.
- **Freshness**: peg cache TTL 60s → 20s; still under upstream
  rate limits at our cadence.
- **Largest-mover-only judge** (cost discipline): each cycle
  picks the symbol with biggest |peg deviation| and ONLY that
  one gets the LLM call. 12x fewer LLM calls + cleaner editorial
  framing ("the judge focuses on what's moving"). Closed the
  DeepSeek rate-limit issue that was producing empty bodies.
- **Burst variant** of TICK NOW (`?count=6`) to bootstrap past
  the 6-reading insufficient-history floor in one click.

### Tests at end of Round 6
- Python: **368 passing** (~50 new this round)
- JS: **36 passing** (3 simulator render tests)
- **404 hermetic tests** total.

### Commit arc (chronological)
- `89d1226` F9 simulator build (predict/attribute/score/narrate)
- `d56ce4d` audit fix: CRPS rename + real persistence/climatology
- `04c0175` audit fix: judge guardrails + interest gate + scrub
- `481a054` audit fix: reliability bin correctness + #9 cap
- `3613735` audit fix: honest n/a + schema_missing in store
- `32b9a43` audit fix: concurrency hardening (#8 / #12 / #16.3)
- `ff07262` test flake fix (LLM_API_KEY env leak)
- `10550ea` audit fix #7: multi-source peg + agreement gate
- `bcd66a1` migration 0007 IMMUTABLE fix
- `5c6c627` TICK NOW no-auth
- `548163f` TICK NOW burst variant
- `47518b4` simulator v2 (per-token canvas + pistons + carousel)
- `7d30be1` simulator v3 (Bloomberg ribbon + workspace + WIRE)
- `33a400a` v3 polish: wire filter + sign-tinted hero + rail sort
- `1ec0a90` v3 flex-shrink fix (ribbon was 1px)
- `87eafd7` judge largest-mover-only + salvage parser
- `c9c252b` SSE flash chevron format
- `bd2d68c` 724-line v2 dead-code purge
- `c51e2ad` 6 integration tests for /api/simulator/*
- (v3.1 big commit) CoinGecko + AI Commentary + mobile + universe
- `dab0e2d` v3.1 tests + freshness tune

---

## Round 7 — explanatory hover layer

User asked for "useful information on hover for the interfaces to
explain the information." Built a CSS-only tooltip primitive plus
a centralised `SIM_TIP` dictionary so every dense bit of jargon in
F9 carries a plain-English explanation on hover.

### What shipped
- New CSS primitive in `web/static/app.css`: `[data-tip="…"]`
  renders a small dark card on `:hover` / `:focus-visible`
  (IBM Plex Mono on `#14161A` with gold border, 140ms fade-in
  100ms delayed so tooltips feel intentional, not skittish).
  Variants: `data-tip-pos` ∈ {below, right} for placement;
  `data-tip-size="lg"` widens to 400px for long copy. Touch
  devices (`@media (pointer: coarse)`) suppress the card so
  mid-tap renders stay clean.
- New `SIM_TIP` constant at the top of the F9 v3 section in
  `app.js` — one place to edit every tooltip. ~50 entries covering
  status strip / ribbon / rail / hero pane / delta grid /
  AI Judge / AI Commentary / WIRE glyphs / pipeline stages /
  calibration metrics / config fields.
- Wired into every surface that builds DOM: `simStatusBar`,
  `simRibbonCell`, `simPegTrack`, `simTokenRail`, `simRailRow`,
  `simHeroPane`, `simDeltaGrid`, `simHeroChart`, `simHeroJudge`,
  `simHeroCommentary`, `simWire`, `renderWireRow`,
  `simCalibrationPanel`, `simCalMetric`, `simReliabilityBins`,
  `simOutcomeHistogram`, `simConfigPanel`.
- WIRE rows now show the 4-letter glyph expansion + the original
  summary on hover — first-time readers can decode CACH / DISP /
  SLNT / RESV without guessing.
- Pipeline pulley stations (PREDICT / ATTRIBUTE / SCORE / NARRATE)
  explain what each job actually does.
- Calibration metrics (BRIER model / climatology / persistence /
  miss-distance) explain the strictly-proper-scoring discipline
  without forcing readers to know the math. Histogram buckets
  (`inside_p50` / `inside_p80` / `outside`) explain what those
  outcome bins mean.
- Native `title=` attribute on the live dot and pegtrack dot
  replaced with `data-tip` so the styling is consistent.

### Verification
- Live page now reports 127 `[data-tip]` attributes across every
  simulator surface: 18 rail rows, 36 ribbon cells (rendered twice
  for the seamless loop), 18 pegtracks, 19 wire rows, 5 status
  cells, 5 delta cells, 4 pulley stations, 4 calibration metrics,
  4 config cells, plus headlines/tags.
- Forced-visibility screenshot test confirmed the card renders
  correctly: dark `#14161A` background, gold border, IBM Plex
  Mono, paper text, positioned per `data-tip-pos`.
- All existing render tests (12) still green — they assert
  structural class presence, which `data-tip` doesn't disturb.

### Tests at end of Round 7
- Python: **368 passing** (unchanged from Round 6 — pure UI work)
- JS: **36 passing** (unchanged)
- **404 hermetic tests** total.

---

## Round 8 — polished product pass (Q3 readiness)

Operator goal verbatim: "audit what you have done, make improvements,
UX needs more explanation on hover, fix broken tokens, distinguish
CEX/DEX, make AI data quicker to load, P&L commentary, audit tests,
speed up startup, polish light theme, update corpus, make WIRE
easier to read, recommend paid services for next quarter."

This round was a single autonomous arc closing all of those at once.

### What shipped
- **Yield-bearing token discipline** (closes the v3.2 NOTES.md todo):
  `TokenContext` now carries `yield_bearing`, `venue_type` (CEX / DEX
  / MIXED), and `pl_lens`. Implemented as a `_OVERLAY` dict so the
  existing 20 constructor calls stay un-touched. The /feed payload
  now ships a `meta` field per token; the UI renders a YLD chip on
  ribbon, rail, and hero pane when `yield_bearing` is true, and
  switches the sub-label from "peg deviation · last tick" to "NAV
  drift · last tick" — so USDY at +1300bp reads as design drift,
  not a depeg. CEX / DEX / MIXED venue chip on the hero pane.
- **AI Commentary speed fix** — the "loading…" UX is gone. New
  `wait_for_llm=False` flag on `get_commentary`: serves any-regime
  cached entry instantly, OR a deterministic fallback built from
  the cheat sheet alone, AND schedules a background thread to
  refresh the right-regime entry. The /commentary endpoint defaults
  to this fast path. `?refresh=true` retains the slow LLM round
  trip for operator-driven regen.
- **AI Judge messaging** — the misleading "Awaiting LLM synthesis
  on the next tick" empty state is replaced with "Reserved for the
  cycle's biggest mover. Will get a synthesis when this token tops
  the daily delta list." Plus a sub-line pointing readers to the
  AI Commentary card below for the persistent structural read.
- **P&L lens block in commentary** — distinct gold-bordered sub-
  section showing path-to-profitability / path-to-loss framing per
  token. Hedged language enforced ("appears to", "loss tail is",
  never "guaranteed" or "always"). 18/18 registered tokens now
  carry a `pl_lens` field.
- **THE WIRE readability** — consecutive same-glyph events
  collapse to one row with an `×N` count badge and a symbol list
  (e.g. "USDC, FRAX, GHO +4 more · brave cache hit (7h old)"
  instead of 12 lookalike rows). Activity legend strip above the
  rows shows the top 8 glyphs by frequency. Summary text humanised
  (`age 26464s` → `7h old`, title-cased lead word).
- **Hover tooltip expansions** — `SIM_TIP.cadence` now lists what
  the tick actually pulls in (3 peg sources, 5bp agreement check,
  EWMA cone, resolution scoring, judge gating). Added
  `SIM_TIP.prediction` and `SIM_TIP.reality` explaining the
  predict→reality→archive loop. Source tip distinguishes Coinbase /
  Kraken (CEX) from CoinGecko (aggregator) and names what each
  brings. Per-token tooltips include venue type.
- **Canary corpus bug fix** — `health_thread._loop` was sleep-then-
  work, so a dev process restarted every <6h NEVER ran its first
  sweep. The corpus auto-verifications file was stale by 2 days at
  audit time. Added `health_last_sweep.txt` persistence; if the
  last sweep is older than the interval, run immediately on boot
  (with a 5s yield for clean startup observability). Stampede-safe
  because only the leader-elected worker runs the loop.
- **Light theme polish** — added inverted tooltip card styling for
  light mode (dark card on cream paper). New `--sim-up` / `--sim-down`
  AA-grade variants in light mode (matches existing `--green` /
  `--rose` paper-mode tokens). YLD tag, venue chip, P&L lens block,
  WIRE legend, and group-count badge all have explicit light-mode
  rules. WIRE glyph colours (TICK / BRAV) darken in light mode for
  AA contrast.
- **Eight new unit tests** (`test_token_context.py` 7 → 15):
  - every token has a `pl_lens` + no banned words ("guaranteed",
    "will earn", "risk-free")
  - USDY / sUSDe / USDM are `yield_bearing=True`
  - venue_type classification correct (MIXED for USDC/USDT, DEX
    for DAI/crvUSD/USDe, CEX for GUSD/RLUSD)
  - deterministic fallback includes the P&L lens block
  - yield-bearing fallback explicitly names "drift", "yield-bearing",
    "design"
  - `wait_for_llm=False` serves cached entry instantly across regimes
  - cold-cache + `wait_for_llm=False` returns deterministic fallback
    immediately
- **Paid-services memo** — `REC_PAID_SERVICES.md` at the repo
  root. 13 services tiered by ROI with monthly costs and "buy
  when" guidance. Q3 baseline: ~$334/mo (Pyth free + Supabase
  Pro $25 + Sonnet $300 + Brave Pro $9). Q1 2027 institutional:
  ~$3,000/mo when revenue justifies Nansen / Chainalysis /
  Chainlink Data Streams.

### Tests at end of Round 8
- Python: **375 passing** (+7 new this round; the 8th was an
  already-counted commentary test that was extended in place)
- JS: **36 passing** (unchanged)
- **411 hermetic tests** total.

### Commits this round
(see git log — single arc; expected one polish commit + one
new-test commit)

### Open items NOT addressed (deferred consciously)
- Server-side commentary pre-warming on /feed (the simpler fast-
  path covers ~95% of the UX win; eager pre-fetch is a Tier-2
  optimisation for when paid Anthropic tier lands).
- SSE-driven wire grouping (the 20s feed-poll re-render groups
  correctly; SSE-driven pushes show ungrouped rows briefly until
  the next poll — acceptable for now).
- F7 ANALYST + F2 ANALYZE + F5 SANCTIONS surfaces have not been
  light-themed in this pass. Scope was F9-first per operator's
  emphasis on the simulator slow-load UX. Carry to a later round.
- Yield-bearing engine fix: the predictor still measures against
  $1.00 for ALL tokens. UI now renders the drift correctly, but a
  proper fix is to anchor the forecast at the most-recent NAV for
  yield-bearing tokens (v4.0 work).

---

## Round 9 — F9 product hardening + agentic trader + chaos engineering

Single autonomous arc covering 14 commits today. Driven by a long
sequence of user prompts asking for: better cone annotation, faster
AI loading, smoother updates, market notices, trader, audit, chaos.
All addressed end-to-end.

### Chronological summary

**Commit `91b67aa` — Round 8 (carried over)** — yield-bearing display
fix, instant commentary, WIRE collapse, P&L lens, tooltips, light
theme, paid-services memo. Already documented in Round 8 above.

**Commit `a88ae8c` — ChainSupply + /feed latency** — Two production
bugs surfaced in the post-restart log audit. Fixed both. (1) The
snapshot persist path failed JSON serialisation because per_chain
arrived as `list[ChainSupply]` and the guard only converted
non-list dataclasses. (2) The /feed endpoint was 12-15s warm because
of 72 sequential Supabase HTTP/2 calls — 6.5s of which was a dead
per-token `calibration_summary` the UI never read. Fixes: deep-
convert dataclass→dict on every list element; drop dead calibration
call; parallelise per-token reads via ThreadPoolExecutor; 3s
process-level feed cache. Result: 12s → 1.4s cold, 1.5ms warm.

**Commit `a7d1633` — SSE silent death** — caught during the same
log audit. The SSE diff loop initialised `last_event_ts = ""` but
events ship `ts` as a unix-seconds float. `float > ""` TypeError
every 2s cycle, swallowed by an except-and-continue. Stream
intended to push live updates was effectively dead. Fixes: init as
`0.0`, coerce via tolerant `_ev_ts()` helper, JS-side new Date(ts)
multiplies by 1000 when value looks like seconds.

**Commit `bcbbbcd` — forecast cone v4 + redemptions <br> fix** —
Research-driven cone redesign (NHC hurricane cones, BoE fan charts,
Metaculus, 538). Added: direct endpoint labels (p50/p80/p95 values
in bps at right edge), in-band labels at widest point ("50%"/"80%"/
"95%"), $1.00 anchor line, NOW separator, HORIZON marker with
absolute width, "MODEL SAYS" plain-English callout, outside-the-cone
caveat strip. Separately: `<br>` tags from LLM-emitted narratives
were rendering as `&lt;br&gt;` visible text in redemption pages.
Server-side `_strip_html_tags` cleaner now runs on every
synthesize_surface / synthesize return; client-side defensive
pre-clean in markdown() for legacy data.

**Commit `9750404` — commentary back-off** — found via autonomous-
loop tick: 20+ `commentary.parse_failed` per minute. The fast-path
served deterministic correctly but the scheduled refresh kept
retrying an LLM that returned empty body. Added per-symbol
exponential back-off (30s → 10min cap); success clears it.

**Commit `9f76c23` — copy & clarity pass (8 fixes)** — user paste
from the rendered page surfaced 8 distinct readability issues. All
fixed:
  - `USDYYLD` (no space) → `USDY YLD` (flex+gap on rail-sym)
  - `CRVUSD —` and `USDE —` (missing issuer) → case-insensitive
    brand-lookup; verified live (`CRVUSD → Curve`, `USDE → Ethena`)
  - Orphan `—` on rail rows → `1M no recent move` or `1M ▲ 0.05bp`
  - `17 / 18` → `17 live · 18 watched` with rich tooltip
  - PEG DEVIATION → `vs $1.00 peg · last tick` / `drift above $1.00
    issuance peg · last tick` for yield-bearing
  - Y-axis `0.9 / -4.7` → `+0.9bp / -4.7bp`
  - Confidence chip tooltip → explicit IPCC probability ranges
  - Cone callout reworked from prophecy-style to three explicit
    clauses (anchor + forecast + regime)

**Commit `b6fe1f9` — Over → By** — tiny preposition fix surfaced by
the user: "Over 11:56 UTC" parsed wrong; should be "By 11:56 UTC"
for wall-clock horizon, "Over the next 5 min" for relative.

**Commit `784a1bd` — TRACK RECORD strip** — user: "it isn't obvious
how well the previous predictions have fared". Added a per-token
strip directly under the cone showing last 10 resolved predictions
as coloured squares (green inside p50, amber inside p95, red
outside), a hit-rate summary line, and a ↑↓ trend chip comparing
the recent half to the prior half.

**Commit `ee6874c` — soft reconciliation** — user: "the experience
of feeling the page refresh and losing scrolling context is jarring".
The 20s reconciliation poll did `mount.innerHTML = ''`, nuking scroll
+ tooltips + hover + animations. Rewrote: `_simNeedsRebuild` decides
structural rebuild vs. soft diff; soft path has six targeted
reconcilers that update values in place; `_simSnapshotScroll`
preserves scroll position even on rare full rebuilds.

**Commit `50b3d5c` — market notice bar + smoothness round 2** —
User: "create a really interesting visual cue when something in the
market is worthy of noticing... flash animation... visible as long as
news is relevant or true... way to dismiss". Five derived conditions
(DEPEG / WIDE CONE / DISPUTED / MODEL MISS / SILENT), gold/orange/red
severity, shimmer-on-entry, alert-glyph pulse, manual-dismiss
persisted in localStorage with condition-keyed ids so a new instance
of the same condition surfaces a fresh pill. Also: rAF batching,
`requestIdleCallback` for calibration panel, signature-skip when feed
is identical. Branded pegtrack symbol labels (per the user request).

**Commit `3b34e88` — ticker sparklines update in place** — User
asked for the same smoothness on the main ticker. Sparklines were
static between full rebuilds. New `refreshSparklinesFor()` swaps
the SVG inner content using a tagged `data-sim-spark` attribute,
keyed by signature so unchanged sparklines skip the write. Plus
honest empty-state messages: distinguish "no ticks ever" from
"silent this cycle" from "only one tick on record."

**Commit `a2d48fa` — The Discipline Trader + collapsible panels** —
User: "let's add an Agentic trader who spots an opportunity... give
it a profitability-focused but sensible persona with deterministic
enhancements... allow us to beautifully collapse and expand certain
panels."

Created `sca/movement/trader.py`. Persona: "The Discipline Trader",
patient mean-reversion arbitrageur. Deterministic rules:
- Mean-reversion thesis: opens a position OPPOSITE current deviation
  whenever the model's 80% cone REACHES toward peg (p80_high ≥ 0 for
  below-peg, p80_low ≤ 0 for above-peg). The cone reaching toward
  peg is the read a real market-maker uses — does NOT wait for the
  sticky EWMA point to predict reversion.
- Never trades yield-bearing tokens.
- Refuses to size up when cone is past per-token alert threshold.
- Position size scales DOWN with cone width.
- Caps concurrent open notional at $50k.
- Marks-to-market + resolves at horizon; P&L = direction-signed
  bp move × notional × 0.0001.
- Wired into ticker cycle's job #4 (after resolver).

Trader UI panel below the cone showing OPEN POSITIONS (cards) +
RECENT SETTLEMENTS (chips) + collapsible header. Plus general-
purpose `_simMakeCollapsible()` applied to the calibration archive
and config panel so power users can hide them.

**Commit `d59dce3` — audit fixes + chaos engineering** — Standing
goal: "review and audit the code, assumptions and experience, apply
fixes, report findings, updated notes... minimal chaos engineering
background thread."

Audit produced 11 findings. 4 CRITICAL fixed:
- Unbounded growth in commentary backoff dicts (now evicts entries
  older than 2× max-backoff, hard-caps at 200 entries)
- Trader could open with non-numeric current_bps (added isinstance
  check before direction assignment)
- _FEED_CACHE read outside the lock (read now lock-guarded)
- Commentary cache_key crashed on malformed cone_thresholds_bps
  (defensive length check, fail closed)

3 IMPORTANT fixed:
- Trader's per-token store reads were serial (parallelised via
  ThreadPoolExecutor)
- Focused-symbol-removal not detected in soft reconcile (now
  triggers rebuild when SIM_VIEW.focused not in feed)
- Trader malformed-context defensive (same shape as commentary fix)

Chaos engineering daemon at `sca/movement/chaos.py`:
- Five scenarios test the audit-fix invariants on every cycle:
  `commentary_malformed_context`, `trader_none_current`,
  `event_ts_string_coercion`, `peg_consensus_silent_token`,
  `synthesis_strip_html`
- Each finding is a (scenario, ran_at, passed, expected, observed,
  severity) row persisted to data/chaos_findings.json
- 15-min default cadence; SCA_CHAOS_INTERVAL_MINUTES override;
  SCA_CHAOS_DISABLED=1 in tests
- LLM judge integration: `fragility_prompt_block()` renders recent
  5 findings as a markdown block appended to the judge prompt so
  the narrative is grounded in tested resilience state
- `test_all_scenarios_pass_against_current_codebase` is the
  on-going self-test — fails if any audit-fix invariant regresses

**Commit `b887b89` — Trader v2: richness + story over time** —
User: "Make the trading mechanism and UX richer... see a story of
how the trader is performing over time, with clear WINS and LOSES
stated. Trader has a budget of $10,000 dollars each day. Track
everything."

Server (`trader.py` discipline_v2):
- DAILY_BUDGET_USD = $10,000 fresh allocation per UTC day; trades
  consume it on open; resolution doesn't replenish today's pool
  (only frees the per-position slot). Shrinks position when budget
  remainder is between 25%-100% of normal; refuses below 25%.
- `day_utc` field on every Trade (YYYY-MM-DD, set on open)
- `outcome` field on resolved trades: 'WIN' / 'LOSS' / 'FLAT'
- `track_record()` returns the story-over-time payload:
  daily_budget tracking, equity curve (last 50 cumulative-P&L
  points), current_streak (consecutive same-outcome trades), daily
  per-UTC-day aggregate (last 14 days, zero-fill), best_day /
  worst_day across full history.

UI:
- TODAY badge with date + "$X used / $10k" + progress bar (amber
  → orange as exhaustion approaches)
- ALL-TIME summary line: N resolved · NW/NL · win-rate · ±$net
- Streak chip ("▶ 3 WINS IN A ROW", pulses on wins)
- Equity-curve sparkline (last 50 P&L points, endpoint coloured)
- Each trade card: prominent WIN / LOSS / FLAT badge
- DAILY LEDGER table (collapsible when >5 days)
- BEST DAY / WORST DAY medals

### Tests at end of Round 9

- Python: **414 passing** (+26 today across trader v1/v2, chaos,
  synthesis-strip, audit-fix regressions)
- JS: **47 passing** (+8 today across notice-bar derivation, soft-
  reconciliation DOM contract, track-record, cone annotations,
  markdown HTML-stripping)
- **461 hermetic tests** total.

### Open items NOT addressed (deferred consciously)

- F2 / F5 / F7 surfaces have not been light-themed (Round 8 deferred,
  still deferred — F9 was the user's priority all session).
- Yield-bearing engine fix (cone anchored at $1.00 for all tokens;
  USDY render shows correctly but engine still measures wrong anchor).
  v4.0 work — needs a NAV oracle pipeline.
- Trader is JSON-file persisted. Audit flagged "use the DB" — for
  a v3 we'd migrate to Supabase `trader_trades` table (migration
  0009). Cap-200 JSON is fine for the current scale.
- Confidence-chip rework (regime-aware vs IPCC-anchored) — user
  asked, I recommended option 1, decision still pending. Both work;
  current ladder is the IPCC-anchored version.
- The `redrawSimulator` fall-back-to-rebuild path still does an
  innerHTML wipe inside its rare-case branch. The scroll snapshot
  preserves position, but a 100% in-place diff would be smoother
  still. Defer until SSE-only operation is proven.

---

## Where we stand right now

Updated as of the end of Round 9. Always rewrite this block, never
append to it.

(Round 9 was the F9 product-hardening + agentic-trader + chaos arc.
F9 is now the headline surface with: Bloomberg ribbon, status strip,
3-column workspace, market notice bar, forecast cone v4, per-token
track record strip, AI Judge, AI Commentary, The Discipline Trader,
calibration archive, config — all collapsible-where-appropriate, all
soft-reconciled, all light-themed. Chaos engineering thread runs
five invariant tests every 15 minutes and feeds findings into the
judge prompt.)

**Live, healthy, no open work:**
- Seven Supabase migrations applied (0001–0006 from prior rounds,
  plus 0007 movement-simulator schema and 0008 peg_ticks consensus
  columns). All additive; back-compat retries built into
  SupabaseStore.insert_peg_tick when 0008 hasn't landed yet.
- F1–F9 navigation complete. F9 SIMULATOR is the new headline
  surface: Bloomberg ribbon → 6-cell status strip → 3-column
  workspace (rail / hero / WIRE) → calibration archive → config
  panel.
- F9 powered by `/api/simulator/feed` (single dense payload) +
  `/api/simulator/stream` (SSE: snapshot + tick diffs + heartbeat).
  Page polls `/feed` every 20s as a reconciliation heartbeat
  behind the SSE channel.
- Multi-source peg ground truth: Coinbase + Kraken + CoinGecko
  polled in parallel, 5bp agreement gate. Disputed ticks flagged
  in the resolution narrative. 15+ of 18 tracked tokens get data
  per cycle.
- LLM judge runs on the largest-mover symbol only (cost
  discipline), with synthesis + insight + pitch. Five guardrails
  (NFKC normalise, weasel-word strip, length cap, forged-citation
  validate, voice-rule audit log). Deterministic fallback when
  the LLM is unavailable.
- Per-token AI Commentary card with structural cheat sheet,
  hedged-voice body, inline `[n]` citations to issuer transparency
  pages. 1h regime-bucket cache. 20 tokens registered.
- Brave web search guarded by five-layer cost discipline (cache
  / coalesce / interest gate / quota cap / stale-on-error).
  Verified live: a 6-tick BURST burns ZERO new Brave calls.
- Calibration archive accreting on every cycle: Brier (for
  direction) + miss-distance (for continuous) scored against
  reality, with persistence + climatology baselines for skill
  comparison. Reliability diagram populates as direction
  predictions accumulate.
- Mobile + responsive: `@media` rules stack the workspace, hide
  the ticker tape, enlarge tap targets to 44pt floor.
- Explanatory hover layer: every dense F9 element (status cells,
  WIRE glyphs, pipeline stages, calibration metrics, confidence
  ladder words, config fields) carries a `data-tip` with plain-
  English context. Centralised in `SIM_TIP` so edits land in one
  place. Suppressed on touch.
- Yield-bearing token discipline: USDY (+1300bp) and USDM render
  with a YLD chip + "NAV drift" sub-label instead of being misread
  as deeply-depegged. Engine still measures against $1.00 (v4.0
  work) but the UI no longer fools the reader.
- CEX / DEX / MIXED venue classification per token, surfaced as
  hero chip + ribbon-cell tooltip + rail-row tip line.
- AI Commentary returns in <500ms regardless of cache state —
  serves any-regime cached entry or deterministic fallback
  instantly + schedules background LLM refresh.
- P&L lens block in every commentary card — short hedged framing
  on path-to-profitability / path-to-loss per token.
- THE WIRE collapses consecutive same-glyph events to one row with
  count badge + symbol list + activity legend strip.
- Canary corpus auto-verification loop now persists last-sweep
  timestamp so dev cycles don't perpetually skip the first sweep.
- Forecast cone v4 — direct endpoint labels (p50/p80/p95 values),
  in-band labels, $1.00 anchor, NOW separator, HORIZON marker with
  absolute width, "MODEL SAYS" plain-English callout, outside-the-
  cone caveat. Each annotation researched (NHC / BoE / Metaculus).
- Per-token TRACK RECORD strip under the cone shows last 10
  resolved predictions as coloured markers + hit-rate trend chip
  (↑ improving / ↓ degrading vs prior-half).
- Soft reconciliation eliminates the jarring 20s redraw — DOM
  nodes survive, scroll position survives, tooltips survive. Six
  targeted in-place reconcilers + rAF batching + idle-callback
  for heavy panels + signature-skip when feed is unchanged.
- Market notice bar at top of F9 — five derived conditions (DEPEG,
  WIDE CONE, DISPUTED, MODEL MISS, SILENT), shimmer-on-entry, alert
  glyph pulse, auto-clear when condition resolves, manual dismiss
  persisted in localStorage with condition-keyed ids.
- Ticker sparklines update in place via tagged `data-sim-spark`
  attributes — line moves with the value, no full rebuild needed.
- THE DISCIPLINE TRADER — agentic simulated trader. Mean-reversion
  arb persona, deterministic rules, $10k fresh daily budget per
  UTC day, position-sized by cone width, marks-to-market + resolves
  at horizon. UI shows TODAY budget bar, all-time summary, streak
  chip, equity curve sparkline, prominent WIN/LOSS labels per
  trade, daily ledger (collapsible), best/worst day medals.
- Calibration archive + config panel are collapsible; state
  persists per-panel in localStorage.
- Chaos engineering background thread (`sca.movement.chaos`) runs
  5 invariant scenarios every 15min; persists pass/fail findings;
  failures fire warn-level observability events; findings feed
  into the LLM judge prompt as "Known fragility patterns" so the
  narrative is grounded in tested resilience state.
- File-based state durably isolated in tests via conftest
  monkeypatches (every cache + config + quota file is tmp_path).

**Settled decisions (do not reopen without a real trigger):**
- TICK NOW endpoint stays auth-optional — operator iterates on
  it during testing and sign-in is friction. Config save stays
  auth-gated.
- Judge runs on largest-mover-only per cycle, NOT per token.
  Editorial framing: "the judge focuses on what's moving."
- Brand colour per ticker is a LABEL ONLY — chip pill, rail bar,
  hero accent. Never a chart line. Lines stay amber.
- Five-color contract is non-negotiable (amber default, up green,
  down red, blue identifier, orange warning). No additional
  palette colors without specific justification.
- Bloomberg ribbon stays on desktop only; mobile hides it
  (redundant with the rail).
- Headless browser remains rejected (carried over from Round 5).
- Stack traces stay out of user-facing UI (carried over).

**Genuinely still open:**
- Snapshot store still local-disk (carried over from Round 5).
- Eval harness still thin (carried over). Movement simulator's
  calibration archive is the new defensible scoreboard.
- SSE endpoint has no integration test — TestClient's
  iter_text() hangs the async-generator while-loop; documented
  in `tests/integration/test_simulator_endpoints.py`. The
  snapshot branch IS exercised by `/feed` tests.
- The token universe is curated at 18 active; the brand-color
  palette + token-context registry can support 30+. Adding
  more is a config-only change once the symbols have live peg
  sources (CG handles ~all of them).
- Yield-bearing tokens (USDY, sUSDe, syrupUSDC, OUSG, BENJI)
  drift above $1 by design — they're tracked but the peg-
  deviation metric is misleading for them. A per-token
  `expected_peg` field exists in `TokenContext` but the engine
  doesn't yet shift its forecast around a non-1.0 anchor. v3.2
  fix.
