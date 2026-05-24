# Doré — autonomous work notes

> **Historical document.** This file is a scratch log from a single
> autonomous work session. It captures the reasoning at that point in
> time and is preserved for context, not as current truth. Most items
> below have since been delivered, superseded, or revised — read
> `ARCHITECTURE.md`, `README.md`, and `AGENTS.md` for the canonical
> current state. Don't act on this file directly.

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

