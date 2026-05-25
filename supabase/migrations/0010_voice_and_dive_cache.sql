-- ════════════════════════════════════════════════════════════════════
--  0010 — voice + commentary-dive caches in the store
-- ════════════════════════════════════════════════════════════════════
-- v5.1 cleanup. The trader voice (daily brief, per-trade narration,
-- end-of-day reflection) and commentary dive caches lived as local
-- JSON files under data/. In a multi-replica deployment each replica
-- would generate its own copy, burn LLM tokens N times over, and
-- present inconsistent narrative across requests.
--
-- These tables move all three caches into the same archive as
-- peg_ticks / predictions / resolutions / trader_trades.
--
-- All purely additive. Local JSON files remain as a fallback / cache
-- but the store is now the durable source of truth.

-- ─ trader voice: daily brief + reflection (one row per UTC day) ─
create table if not exists public.trader_voice_brief (
  day_utc text primary key,
  inputs_hash text not null,
  body text not null,
  fallback boolean not null default false,
  generated_at timestamptz not null default now(),
  inserted_at timestamptz not null default now()
);

create table if not exists public.trader_voice_reflection (
  day_utc text primary key,
  inputs_hash text not null,
  body text not null,
  stats jsonb,
  fallback boolean not null default false,
  generated_at timestamptz not null default now(),
  inserted_at timestamptz not null default now()
);

-- ─ trader voice: per-trade narration (one row per trade id) ─
create table if not exists public.trader_voice_narration (
  trade_id text primary key,
  body text not null,
  generated_at timestamptz not null default now(),
  inserted_at timestamptz not null default now()
);

-- ─ commentary dive (one row per (symbol, inputs_hash)) ─
create table if not exists public.commentary_dive (
  id text primary key,            -- "<SYMBOL>:<inputs_hash>"
  symbol text not null,
  inputs_hash text not null,
  intro text,
  sections jsonb not null,
  citations jsonb,
  fallback boolean not null default false,
  generated_at timestamptz not null default now(),
  inserted_at timestamptz not null default now()
);

create index if not exists commentary_dive_symbol_inserted
  on public.commentary_dive (symbol, inserted_at desc);
