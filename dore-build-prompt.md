# Doré + Rayleigh Stark: Hardening, Two-Lens Architecture, and Consultancy Site Build

You are working across two codebases (the Doré product, and the Rayleigh
Stark consultancy site). Do the work in the order below. Do NOT refactor or
rewrite anything that already works unless a step explicitly calls for it.
Preserve existing functionality, themes, and structure. Build on what is there.

## STANDING PRINCIPLE: GUARDRAILS FIRST

Before and after every change, respect these non-negotiables. They are the
core of the product's value (verifiable trust) and must never regress:

- The model proposes, deterministic code disposes. Any number a user sees
  must come from auditable code, never from LLM free-reasoning. If you find
  any displayed figure being produced by the LLM rather than computed in
  code, flag it and fix it.
- Every fact must be traceable to a source. Each verified value carries: the
  claim, the source(s) checked against, timestamp, block number (for
  on-chain), and a verification status (verified / unverified / assumed).
- Sanity bounds and tolerance bands gate every value before it enters
  reconciliation or display. Reject implausible values (impossible coverage
  ratios, decimals errors, zero/negative supply) rather than surfacing them.
- The system must be honest about what it does not know. "Unverified" and
  "assumed" states must remain visible in the UI, never silently hidden.
- Hermes (the agent) must answer only from the verified fact store, cite
  every figure, and hedge or refuse when it cannot ground an answer. Never
  let Hermes assert a number it cannot trace.
- Conservative posture on claims: the system surfaces and reconciles facts;
  it must never assert a verdict about an issuer (e.g. "under-reserved",
  "fraudulent"). Surface the data, let the user judge.

If any change would weaken any of the above, stop and explain rather than
proceeding.

## PHASE 0: AUDIT AND DOCUMENT (do this first, change nothing yet)

1. Produce a current-state architecture document (ARCHITECTURE.md):
   - Map every component: ingestion (attestation scraping, RPC readers),
     verification engine, fact store / persistence, guardrail layer, Hermes,
     the views (Monitor, Analyze, Corpus, Evals, Sanctions, Redemptions,
     Analyst), and the surfaces.
   - For each, document what it does, its inputs/outputs, and its current
     guardrail coverage.
   - Explicitly identify where the verification engine is or is not
     object-agnostic (whether it hardcodes "reserves" or could generalise to
     other claim types later). Note this; do not refactor yet.
2. Produce a guardrail coverage report: list every point where a value
   enters the system or is displayed, and whether it is currently
   sanity-bounded, source-traced, and verification-tagged. Flag gaps.
3. Produce a data-quality report covering the three layers: extraction
   (DeepSeek span-grounding), on-chain (RPC reliability, decimals,
   block-pinning), and registry (contract address verification status,
   native/bridged flags). Flag every "assumed" that should be "verified",
   especially any unverified contract addresses currently feeding headline
   figures.

## PHASE 0.5: PRESERVE THE TWO-LENS FUTURE (architect for it, do NOT build it)

We are building toward a fused platform: ONE verification core, TWO lenses.
You are only building Lens 1 now. Your job in this phase is to ensure the
hardening in later phases does NOT foreclose Lens 2. Do not build Lens 2.

The fused architecture (document this in ARCHITECTURE.md as the target):

- SHARED CORE (build/harden once, both lenses depend on it):
  - Verification Engine: takes a CLAIM, reconciles it against an independent
    source of truth, returns a verified result with provenance. It must be
    OBJECT-AGNOSTIC: it must not hardcode "reserves". A claim is a generic
    thing ("a financial assertion to be checked against reality"), whether
    that is "issuer reserves = $X as of date" (Lens 1) or "agent A paid $Y to
    agent B at block N" (Lens 2, future).
  - Fact Store: one immutable, sourced, timestamped record of every verified
    fact, with a generic schema shape that fits both reserve facts and
    (future) agent-payment facts. Same provenance fields for both.
  - Guardrail Layer: tolerance bands, sanity bounds, anomaly detection. In
    Lens 1 these guard reserve numbers. In Lens 2 (future) the SAME logic
    becomes agent spend-controls (budget limits, allowlists, velocity
    anomaly, kill switches). Build it generic so it is reusable.
  - Hermes: one conversational surface over the whole fact store, designed to
    span both lenses (answer reserve questions AND, later, agent-spend
    questions) from the same cited fact store.
  - On-chain/RPC layer: shared. Both lenses ultimately verify against
    on-chain settlement.

- LENS 1 (Reserves — Doré today, the only thing you build now): claim-type is
  issuer backing; sources are attestations + on-chain supply; views are
  Monitor / Analyze / Sanctions / Redemptions / Corpus. Question: "is this
  money real?"

- LENS 2 (Agent payments — FUTURE, do NOT build): claim-type will be agent
  payments and authorisations; sources will be x402 / AP2 / AWS AgentCore
  transaction streams plus on-chain settlement plus authorisation records;
  the headline feature will be an auditable link from every agent payment
  back to a human principal's authorisation (the named regulatory blocker for
  enterprise agent deployment). Question: "can the machine spending it be
  trusted?"

THE ONE DISCIPLINE: as you harden Lens 1 in the phases below, keep the
Verification Engine, Fact Store, and Guardrail Layer object-agnostic. Where
you find them hardcoding "reserves" in a way that would block a future
claim-type, refactor toward a generic claim/fact/guardrail abstraction — but
ONLY as far as cleanly supports Lens 1 today. Do not speculatively build Lens
2 structures. The test: adding Lens 2 later should be "a new ingestion
connector + a new claim-type + new views", never "rewrite the engine".

## PHASE 1: SECURITY HARDENING

1. Secrets: confirm no secrets (Supabase keys, DeepSeek key, RPC keys) are in
   the repo or client-shipped code. All must be server-side env vars. Ensure
   .env is gitignored and a .env.example exists with names only. If any
   secret is found tracked in git history, flag it for rotation (do not
   attempt rotation yourself).
2. Access control: review auth on every endpoint that triggers cost (DeepSeek
   calls, RPC reads) or exposes data. Anything expensive must be behind
   authentication. Add per-user/per-IP rate limiting on endpoints that
   trigger paid work.
3. Input validation: validate and sanitise all inputs, especially anything
   that reaches the LLM (guard against prompt injection into Hermes) or the
   database (guard against injection).
4. Supabase: review row-level security policies. Confirm users can only
   read/write their own data. Confirm the service key is never exposed
   client-side.
5. Network: document the intended deployment (Railway behind Cloudflare), and
   ensure the app validates it is receiving traffic via the proxy rather than
   allowing direct origin access. Note any steps that must be done in the
   Railway/Cloudflare dashboards (do not attempt those here).

## PHASE 2: PERSISTENCE HARDENING

MIGRATION SAFETY: all schema migrations in this phase must be NON-DESTRUCTIVE
and reversible. Never drop, overwrite, or alter existing tables in place.
Introduce new structures alongside existing ones and migrate data by copying,
so the current working state is always recoverable. This session may run
unsupervised — do nothing destructive.

1. The fact store is the moat. Ensure every verified fact is persisted
   immutably with full provenance (claim, sources, timestamp, block,
   verification status, result). Verified facts should be append-only;
   corrections create new records rather than overwriting, preserving history.
2. Point-in-time reproducibility: ensure the schema can reconstruct exactly
   what the system reported on any past date, with inputs frozen. If the
   current schema only stores latest snapshots, design the migration to a
   time-series/historical model (this addresses the "snapshot only" gap).
   Preserve existing data through any migration (non-destructively, per above).
3. Caching: cache aggressively to protect free-tier RPC limits and DeepSeek
   spend. Attestation documents (which change rarely) should be cached by
   version/hash and never re-parsed if unchanged. On-chain reads cached for a
   sensible short window with the block pinned.
4. Multi-endpoint on-chain reads: where supply figures feed displayed
   numbers, read from at least two independent RPC endpoints at the same
   pinned block and compare; agreement raises confidence, divergence flags
   for review. Build this as a reusable abstraction so paid providers can slot
   in later without rework.
5. Store decimals, native/bridged flags, and contract verification status per
   token per chain in the registry as first-class persisted data, not
   hardcoded.

## PHASE 3: DOCUMENTATION

1. ARCHITECTURE.md (from Phases 0 and 0.5), kept current — including the
   two-lens target architecture.
2. A README for each repo: what it is, how to run it, env vars required, the
   guardrail principles, the data model.
3. Inline documentation of the verification flow and the guardrail checks, so
   the reasoning is auditable by a future engineer or a technical investor
   doing diligence.
4. A SECURITY.md documenting the security posture and the secrets handling.

## PHASE 4: CONSULTANCY SITE (Rayleigh Stark) — only after Phases 0–3

Switch to the Rayleigh Stark consultancy codebase.

1. Audit the existing site: it has grown organically, so map the current
   pages and structure, and identify inconsistencies, dead ends, outdated
   content, and pages that have drifted from the brand. Do NOT rewrite the
   brand voice or visual identity; preserve "Complex systems. Clean
   decisions." and the established aesthetic. Improve coherence, not identity.
2. Improve existing pages: tighten copy, fix structural and navigation
   issues, ensure consistency of tone and layout across pages, improve
   responsiveness and performance where weak.
3. Build a new Products page that tells a compelling story:
   - Surface Doré as the flagship: what it is, the thesis (machine-speed
     financial trust), the capability (verify reserves against on-chain
     reality, sanctions screening, redemptions, the two honest coverage
     numbers, Hermes the conversational agent), framed in the established
     institutional, restrained brand voice.
   - Use high-quality screenshots/shots of the built product to show, not
     tell. Treat the visuals as the centrepiece. (Image assets will be
     provided; structure the page to showcase them well, with placeholders and
     clear guidance on what each shot should be and where it goes.)
   - Maintain the conservative claims posture: describe what Doré does
     (surfaces and reconciles, traceable to source), never overclaim or assert
     verdicts about issuers. No specific live coverage figures for named
     issuers in marketing copy.
   - Structure it so additional products (e.g. a future agent-payments lens)
     can be added later without redesign.
4. Ensure the Products page links cleanly into the rest of the site and that
   the overall narrative (operator-led, built on real systems) holds.

## OUTPUT

At the end, produce a summary of: what changed, what was preserved, every
guardrail touched (and confirmation none regressed), any secrets/security
issues found, the object-agnostic refactors made (and confirmation Lens 2 was
NOT built), and a prioritised list of anything still outstanding that you
could not complete or that requires my action (e.g. dashboard config, image
assets, key rotation).
