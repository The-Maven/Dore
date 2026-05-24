-- ════════════════════════════════════════════════════════════════════
--  0006 — widen attestation_url_overrides.via to include 'auto_validate'
-- ════════════════════════════════════════════════════════════════════
-- Phase 2 (cache validation) introduces a new origin path: a cached
-- entry is re-checked against the issuer's published reports and
-- upgraded when a newer one is found. This needs its own `via` value
-- so an operator can tell at a glance that a URL came from automatic
-- validation, not original discovery or a curator's hand.
--
-- Existing values stay valid. Purely additive; no row updates needed.

alter table public.attestation_url_overrides
  drop constraint if exists attestation_url_overrides_via_check;

alter table public.attestation_url_overrides
  add constraint attestation_url_overrides_via_check
  check (via in (
    'manual',         -- a curator set this URL through the F8 Compendium
    'web_search',     -- original discovery via Brave / DDG
    'locator',        -- static HTML scrape of a transparency page
    'paxos_resolver', -- the deterministic Paxos URL pattern probe
    'seed',           -- bootstrap from config/stablecoins.yaml (deprecated)
    'import',         -- bulk import from another Doré instance
    'auto_validate'   -- the cache validator upgraded to a newer issuer report
  ));
