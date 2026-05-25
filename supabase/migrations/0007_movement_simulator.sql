-- ════════════════════════════════════════════════════════════════════
--  0007 — coin movement simulator: predictions, resolutions, peg_ticks
-- ════════════════════════════════════════════════════════════════════
-- The product is the track record. Every prediction Doré makes is
-- written here, immutable, addressable by id, with a future resolves_at
-- that the resolver thread will score when reality catches up.
--
-- Design discipline:
--   * predictions is APPEND-ONLY. Once made_at is set, no row updates
--     are allowed except by the resolver inserting a paired row into
--     resolutions. That's the calibration archive — investors must be
--     able to trust that the history hasn't been edited.
--   * resolutions is 1:1 with predictions (or 0:1 — unresolved). The
--     foreign key + unique constraint enforces this.
--   * peg_ticks is the cheap intraday price/peg series we use for
--     peg-deviation predictions. Free Coinbase spot is the v1 source;
--     adding Kraken or a DEX adapter is purely additive.
--
-- The schema is intentionally explicit — wide on what we predict, what
-- bands we publish, what drivers we cite, what confidence word we
-- attach. An IPCC-style ladder word lives next to a probability band
-- so a reader who skips the math still reads the same claim.

-- ── peg_ticks ─────────────────────────────────────────────────────────
-- Intraday spot price per (symbol, source). Used to (a) compute peg
-- deviation in basis points, (b) resolve peg-deviation predictions
-- against reality, (c) seed driver attribution ("the peg drifted 8bp
-- in the prior hour"). Source field is intentionally free-form so we
-- can layer adapters later without a migration.
create table if not exists public.peg_ticks (
  id              uuid primary key default gen_random_uuid(),
  symbol          text not null,
  source          text not null,
  price           numeric not null,
  deviation_bps   numeric not null,   -- (price - 1.0) * 10000, signed
  read_at         timestamptz not null default now()
);
create index if not exists peg_ticks_symbol_read_at
  on public.peg_ticks (symbol, read_at desc);
create index if not exists peg_ticks_source_read_at
  on public.peg_ticks (source, read_at desc);

-- ── predictions ───────────────────────────────────────────────────────
-- Every tick the engine emits a row here. The schema is wide on
-- forecast structure (point + bands + confidence word) because the UI
-- needs all three to render a fan chart honestly and a calibration
-- diagram correctly. drivers carries the cited attribution; model is a
-- short label so we can A/B model versions transparently in the
-- archive.
create table if not exists public.predictions (
  id              uuid primary key default gen_random_uuid(),
  symbol          text not null,
  kind            text not null
                    check (kind in ('peg_deviation', 'net_flow_direction',
                                    'net_flow_magnitude')),
  -- Cadence and horizon are decoupled by design. Made_at is when the
  -- engine emitted the prediction; resolves_at is when the resolver
  -- should grade it. The default app cadence is 10 minutes between
  -- emits; the horizon (e.g. 60 minutes ahead) is configured per call.
  made_at         timestamptz not null default now(),
  horizon_minutes int not null check (horizon_minutes between 1 and 1440),
  resolves_at     timestamptz not null,
  -- Forecast structure. point is the central estimate; the band
  -- columns capture the uncertainty cone we render on the fan chart.
  -- p50_low/high is the 50% interval (1-in-2 reality lands inside);
  -- 80% and 95% similarly. v1 emits SYMMETRIC bands; the schema
  -- supports asymmetric bands (separate low/high columns) so a
  -- future model version can populate them differently without a
  -- migration.
  point           numeric not null,
  p50_low         numeric,
  p50_high        numeric,
  p80_low         numeric,
  p80_high        numeric,
  p95_low         numeric,
  p95_high        numeric,
  -- For direction predictions, the engine emits a probability that
  -- the outcome falls in the positive direction (e.g. net_flow > 0).
  -- Calibration plots are computed off this field.
  prob_positive   numeric check (prob_positive is null
                                  or prob_positive between 0 and 1),
  -- IPCC-style ladder word for headline-readers: 'virtually_certain',
  -- 'very_likely', 'likely', 'about_as_likely_as_not', 'unlikely',
  -- 'very_unlikely', 'exceptionally_unlikely'. Decoupled from the
  -- numeric band so an honest "high confidence the answer is 50/50"
  -- can still render.
  confidence_word text check (confidence_word is null or confidence_word in (
    'virtually_certain', 'very_likely', 'likely',
    'about_as_likely_as_not', 'unlikely',
    'very_unlikely', 'exceptionally_unlikely'
  )),
  -- Cited drivers — an array of {source_id, url, weight, summary}
  -- objects. Empty array is honest ("we have no leading signal to
  -- cite") and that's surfaced in the UI as 'no driver cited'.
  drivers         jsonb not null default '[]'::jsonb,
  -- Model identity. Bumping this string in code creates an A/B trail
  -- in the archive — we can see which model wrote what.
  model           text not null,
  -- Notes from the engine (e.g. 'baseline_ewma_v1: insufficient
  -- history', 'corpus_signal_count=3'). Free-form, never user-edited.
  notes           text not null default '',
  -- LLM judge output: synthesis + insight + pitch, written AFTER the
  -- deterministic forecast lands. Three labelled fields so the UI can
  -- render them in distinct panels. NULL = no judge layer ran for this
  -- row (e.g. LLM offline or feature gated off); the UI falls back to
  -- the deterministic forecast prose.
  judge_synthesis text,
  judge_insight   text,
  judge_pitch     text,
  judge_model     text
);
create index if not exists predictions_symbol_made_at
  on public.predictions (symbol, made_at desc);
create index if not exists predictions_kind_resolves_at
  on public.predictions (kind, resolves_at);
-- The resolver job hits this index every minute to find predictions
-- whose resolves_at has passed but which don't yet have a resolution.
-- Plain b-tree index on resolves_at — PostgreSQL refuses to use
-- now() in a partial index predicate because it is not IMMUTABLE
-- (the predicate would silently shift meaning over time, which
-- can't be safely maintained). The resolver query filters with
-- `lte resolves_at, now()` at runtime; the index here is enough
-- to make that filter fast.
create index if not exists predictions_unresolved
  on public.predictions (resolves_at);

-- ── resolutions ───────────────────────────────────────────────────────
-- The scored outcome of one prediction. The 1:1 with predictions is
-- enforced by `unique (prediction_id)` — a prediction is graded once.
-- Brier is for binary/categorical direction predictions; CRPS is for
-- continuous magnitude or peg-deviation predictions. Both are written
-- when applicable and null when not, so the calibration UI can pick
-- the right metric per row.
create table if not exists public.resolutions (
  id              uuid primary key default gen_random_uuid(),
  prediction_id   uuid not null
                    references public.predictions (id) on delete restrict,
  resolved_at     timestamptz not null default now(),
  -- Realised value at the prediction's resolves_at. For
  -- peg_deviation, this is the realised bps; for net_flow_direction,
  -- the realised direction (+1, -1, 0); for net_flow_magnitude, the
  -- realised absolute change.
  actual_value    numeric not null,
  -- Strictly proper scoring rules. Brier in [0, 1] (lower better);
  -- CRPS in [0, inf] (lower better). Outcome_kind is a categorical
  -- shortcut for the UI: 'hit' if actual landed in p50 band, 'partial'
  -- if in p80 but not p50, 'miss' if outside p80, 'inside_p95' for the
  -- band-only outcome on continuous outputs.
  brier_score     numeric check (brier_score is null
                                  or brier_score between 0 and 1),
  crps_score      numeric check (crps_score is null or crps_score >= 0),
  outcome_kind    text not null
                    check (outcome_kind in ('hit', 'partial', 'miss',
                                            'inside_p95', 'inside_p80',
                                            'inside_p50', 'outside')),
  -- Honest post-mortem prose. Written by the resolver from the data
  -- (deterministic) or by an LLM voice with citation discipline.
  narrative       text not null default '',
  -- Baseline comparisons — every resolution carries the score the
  -- naive baselines would have earned on the same row. Investors
  -- need to see whether the model actually adds skill over
  -- persistence + climatology.
  baseline_persistence_brier numeric,
  baseline_climatology_brier numeric,
  unique (prediction_id)
);
create index if not exists resolutions_resolved_at
  on public.resolutions (resolved_at desc);

-- ── row-level security ────────────────────────────────────────────────
-- Same posture as monitor_snapshots: server writes via service_role
-- and bypasses RLS; authenticated clients can read.
alter table public.peg_ticks   enable row level security;
alter table public.predictions enable row level security;
alter table public.resolutions enable row level security;

create policy "peg_ticks readable by authenticated" on public.peg_ticks
  for select to authenticated using (true);
create policy "predictions readable by authenticated" on public.predictions
  for select to authenticated using (true);
create policy "resolutions readable by authenticated" on public.resolutions
  for select to authenticated using (true);
