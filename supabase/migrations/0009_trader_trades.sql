-- ════════════════════════════════════════════════════════════════════
--  0009 — trader_trades: simulated trade ledger
-- ════════════════════════════════════════════════════════════════════
-- Audit finding 3.1: the Discipline Trader's history lives in a local
-- JSON file (data/discipline_trader.json). In a multi-replica cloud
-- deployment that file would diverge per replica, corrupting P&L
-- aggregates and the daily-budget counter.
--
-- This migration moves the trade ledger into the same Supabase archive
-- as peg_ticks, predictions, and resolutions. Each row mirrors the
-- Trade dataclass at v5: open fields are populated at insert, the
-- resolution fields are patched in place when the trade settles.
--
-- Local JSON stays as a fast in-process cache; the durable copy lives
-- here so the ledger survives restarts and replica failover.

create table if not exists public.trader_trades (
  id text primary key,
  symbol text not null,
  direction text not null check (direction in ('long', 'short')),
  status text not null check (status in ('open', 'resolved')) default 'open',
  -- open-side fields
  opened_at timestamptz not null,
  resolves_at timestamptz,
  prediction_made_at text,
  day_utc text,
  entry_bps numeric not null,
  forecast_point_bps numeric,
  p80_low numeric,
  p80_high numeric,
  notional_usd numeric not null,
  cone_width_bps numeric,
  confidence_word text,
  rationale text,
  edge_bps numeric,
  entry_sources jsonb,
  entry_consensus_kind text,
  entry_max_disagreement_bps numeric,
  entry_consensus_price numeric,
  forecast_p50_low numeric,
  forecast_p50_high numeric,
  forecast_p95_low numeric,
  forecast_p95_high numeric,
  horizon_minutes integer,
  strategy text,
  paired_with text,
  venue_outlier text,
  priority_score numeric,
  entry_news_context jsonb,
  payout_timeline_label text,
  capital_cost_scale numeric,
  conviction text,
  narration text,
  -- resolution-side fields (filled in by update_trade_resolution)
  exit_bps numeric,
  resolved_at_real timestamptz,
  pnl_usd numeric,
  pnl_bps numeric,
  outcome text check (outcome is null or outcome in (
    'WIN', 'LOSS', 'FLAT', 'STALE'
  )),
  outcome_note text,
  exit_sources jsonb,
  exit_consensus_kind text,
  exit_max_disagreement_bps numeric,
  exit_consensus_price numeric,
  landed_inside_p50 boolean,
  landed_inside_p80 boolean,
  landed_inside_p95 boolean,
  inserted_at timestamptz not null default now()
);

-- Index for the "show me today's trades" + "newest-first" queries the
-- trader panel + track-record API run on every page load.
create index if not exists trader_trades_opened_at_desc
  on public.trader_trades (opened_at desc);

-- Per-day aggregate is the dominant query on this table.
create index if not exists trader_trades_day_utc
  on public.trader_trades (day_utc, opened_at desc)
  where day_utc is not null;

-- Open trades only — the trader scans this every cycle to resolve.
create index if not exists trader_trades_open
  on public.trader_trades (resolves_at)
  where status = 'open';
