-- ════════════════════════════════════════════════════════════════════
--  0008 — peg_ticks: multi-source consensus columns
-- ════════════════════════════════════════════════════════════════════
-- Audit #7: a single peg source is a single point of failure for the
-- entire calibration archive. If Coinbase's quote is wrong, the
-- reliability diagram reflects Coinbase's noise, not the true peg.
-- The two-source rule that governs the rest of Doré now applies to
-- the ground truth too.
--
-- This migration adds three columns to peg_ticks, all NULL-able so
-- existing single-source rows continue to read cleanly:
--
--   consensus_kind        — 'single' | 'agreed' | 'disputed'
--                           single   = only one source responded
--                           agreed   = ≥2 sources, all within
--                                      tolerance (default 5bp)
--                           disputed = ≥2 sources, max gap > tol
--
--   sources               — jsonb array of {name, price, fetched_at}
--                           the per-source readings that produced
--                           this row. The resolver inspects this
--                           when grading so a disputed tick can be
--                           cited as "ground truth contested
--                           across sources" in the narrative.
--
--   max_disagreement_bps  — |max(prices) - min(prices)| × 10000
--                           0 for single-source rows; the spread
--                           for multi-source. Indexed for the
--                           freshness panel's outlier query.
--
-- Purely additive. Existing rows remain valid (consensus_kind
-- defaults to 'single' for legacy rows when read by code).

alter table public.peg_ticks
  add column if not exists consensus_kind text
    check (consensus_kind is null or consensus_kind in (
      'single', 'agreed', 'disputed'
    ));

alter table public.peg_ticks
  add column if not exists sources jsonb;

alter table public.peg_ticks
  add column if not exists max_disagreement_bps numeric;

-- Index for the "show me disputed peg ticks in the last 24h" query
-- the simulator state surface will run.
create index if not exists peg_ticks_consensus_kind_read_at
  on public.peg_ticks (consensus_kind, read_at desc)
  where consensus_kind is not null;
