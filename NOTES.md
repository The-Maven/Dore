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

## Where we stand right now

Updated as of the end of Round 5. Always rewrite this block, never
append to it.

**Live, healthy, no open work:**
- All four Supabase migrations applied (0001 initial, 0002 corpus
  opt-out, 0003 attestation URL overrides, 0004 verified facts).
- Curator-set + canary-discovered attestation URLs persist across
  redeploys.
- Time-series `verified_facts` writes on every completed analyze run.
- All four result surfaces (analyze, sanctions, redemption, evals)
  share the cached-first + freshness-strip + latency-UX + backgrounded-
  toast + friendly-error pattern.
- Compendium REFRESH triggers the actual canary sweep.
- 11/14 fiat tokens auto-resolve to a verified attestation through
  the static discovery chain on the 6-hourly canary cycle.
- LLM-emitted briefs are em-dash-free and follow the voice rule.

**Settled decisions (do not reopen without a real trigger):**
- Three JS-rendered issuer pages (USDP, USDG, EURC) stay on the AI
  Context fallback. Headless browser is not coming. See `AGENTS.md`.
- Stack traces stay out of the user-facing UI; operators find them
  in server logs + the F1 ops feed.

**Genuinely still open:**
- Tier CHECK on `sources.tier` still legacy (only accepts the
  pre-discovery vocabulary). `_ensure_source_row` coerces unknown
  tiers to `research`. A future `0005_tier_widening.sql` migration
  would close this analogously to how 0002 closed the status side.
- Snapshot store is still local-disk. Real durability tier (S3 /
  Supabase Storage) remains the next infra step.
- Eval harness is still thin (1-2 cases per surface). Hasn't bitten
  anyone yet, but it's a known shallow guardrail.
