-- ════════════════════════════════════════════════════════════════════
--  0005 — widen sources.tier to the full corpus vocabulary
-- ════════════════════════════════════════════════════════════════════
-- The sources.tier CHECK has been frozen at the 0001 vocabulary
-- since project birth:
--
--   sources.tier  'primary' | 'standard' | 'methodology' | 'research'
--
-- Two later additions never made it into the constraint:
--
--   - 'commentary' — vetted expert commentary, used sparingly
--   - 'tier1_official' — auto-discovery bucket for government /
--     standards-body publications (OFAC actions, BIS/CPMI papers,
--     FSB updates, EUR-Lex MiCA RTS, NYDFS industry letters)
--   - 'tier2_industry' — auto-discovery bucket for commercial
--     publications (issuer blogs, blockchain-analytics shops)
--
-- The Python registry (sca.corpus.sources.VALID_TIERS) accepts all
-- seven. Until this migration, supabase_store._ensure_source_row had
-- to coerce anything outside the legacy four to 'research' so the DB
-- would accept the upsert. This widens the CHECK to match the
-- canonical vocabulary so the shim can be retired.
--
-- No row update needed: every historical sources row is already in
-- the legacy set (the shim was forcing that on every write). The
-- widening is purely additive — existing values stay valid, new
-- values become permitted.

-- ── sources.tier ──────────────────────────────────────────────────────
alter table public.sources drop constraint if exists sources_tier_check;

alter table public.sources
  add constraint sources_tier_check
  check (tier in (
    'primary',           -- the law / regulation itself
    'standard',          -- attestation & accounting standards
    'methodology',       -- rating-agency / supervisory methodology
    'research',          -- central-bank / BIS / academic
    'commentary',        -- vetted expert commentary; use sparingly
    'tier1_official',    -- auto-discovered official sources
    'tier2_industry'     -- auto-discovered industry sources
  ));
