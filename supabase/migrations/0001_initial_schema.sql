-- Rayleigh Stark — stablecoin compliance agent
-- 0001 initial schema: durable runtime state.
--
-- Scope: only state that must survive a restart lives here. Version-controlled
-- config (stablecoins.yaml, SKILL.md, corpus staging text) stays in files;
-- pure speed caches stay in memory.

-- ── profiles ──────────────────────────────────────────────────────────
-- Per-user app data, 1:1 with Supabase Auth users.
create table if not exists public.profiles (
  id            uuid primary key references auth.users (id) on delete cascade,
  email         text,
  full_name     text,
  organisation  text,
  role          text not null default 'analyst'
                  check (role in ('analyst', 'curator', 'admin')),
  created_at    timestamptz not null default now()
);

-- ── analyses ──────────────────────────────────────────────────────────
-- Every analysis / sanctions / redemption run. One table serves three
-- purposes: the job store (status), the result history, and the result
-- cache (input_fingerprint is the cache key).
create table if not exists public.analyses (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid references public.profiles (id) on delete set null,
  surface            text not null
                       check (surface in ('attestation', 'sanctions', 'redemption')),
  symbol             text not null,
  status             text not null default 'pending'
                       check (status in ('pending', 'running', 'done', 'error')),
  input_fingerprint  text,
  result             jsonb,
  error              text,
  created_at         timestamptz not null default now(),
  started_at         timestamptz,
  completed_at       timestamptz
);
create index if not exists analyses_lookup
  on public.analyses (surface, symbol, status, created_at desc);
create index if not exists analyses_fingerprint
  on public.analyses (input_fingerprint)
  where input_fingerprint is not null;
create index if not exists analyses_user on public.analyses (user_id);

-- ── sources ───────────────────────────────────────────────────────────
-- The corpus source registry (was corpus/sources.yaml).
create table if not exists public.sources (
  id          text primary key,                 -- slug, e.g. 'mica-title-iii'
  title       text not null,
  tier        text not null
                check (tier in ('primary', 'standard', 'methodology', 'research')),
  status      text not null default 'proposed'
                check (status in ('proposed', 'approved', 'rejected')),
  url         text not null default '',
  summary     text not null default '',
  notes       text not null default '',
  created_at  timestamptz not null default now()
);

-- ── corpus_passages ───────────────────────────────────────────────────
-- Ingested, chunked corpus text (was corpus/data/*.json).
create table if not exists public.corpus_passages (
  id          uuid primary key default gen_random_uuid(),
  source_id   text not null references public.sources (id) on delete cascade,
  ordinal     int not null,
  section     text not null default '',
  heading     text not null default '',
  text        text not null,
  citation    text not null,
  page        int,
  created_at  timestamptz not null default now()
);
create index if not exists corpus_passages_source
  on public.corpus_passages (source_id, ordinal);

-- ── curation_votes ────────────────────────────────────────────────────
-- Append-only ledger of source-approval decisions; the latest row wins.
create table if not exists public.curation_votes (
  id          uuid primary key default gen_random_uuid(),
  source_id   text not null,
  decision    text not null check (decision in ('approved', 'rejected')),
  decided_by  uuid references public.profiles (id) on delete set null,
  created_at  timestamptz not null default now()
);
create index if not exists curation_votes_source
  on public.curation_votes (source_id, created_at desc);

-- ── address_decisions ─────────────────────────────────────────────────
-- Append-only ledger of contract-address verifications; latest row wins.
create table if not exists public.address_decisions (
  id          uuid primary key default gen_random_uuid(),
  symbol      text not null,
  chain       text not null,
  contract    text not null,
  decision    text not null check (decision in ('verified', 'rejected')),
  decided_by  uuid references public.profiles (id) on delete set null,
  created_at  timestamptz not null default now()
);
create index if not exists address_decisions_lookup
  on public.address_decisions (symbol, chain, contract, created_at desc);

-- ── monitor_snapshots ─────────────────────────────────────────────────
-- On-chain supply readings over time — durable monitoring history.
create table if not exists public.monitor_snapshots (
  id             uuid primary key default gen_random_uuid(),
  symbol         text not null,
  total_supply   numeric not null,
  native_supply  numeric,
  bridged_supply numeric,
  per_chain      jsonb,
  warnings       jsonb,
  read_at        timestamptz not null default now()
);
create index if not exists monitor_snapshots_symbol
  on public.monitor_snapshots (symbol, read_at desc);

-- ── row-level security ────────────────────────────────────────────────
-- The FastAPI server uses the service_role key and bypasses RLS; these
-- policies govern any direct client access. This is a shared compliance
-- workspace: authenticated users read shared data; writes are scoped.
-- Org-level scoping is a deliberate future refinement.
alter table public.profiles          enable row level security;
alter table public.analyses          enable row level security;
alter table public.sources           enable row level security;
alter table public.corpus_passages   enable row level security;
alter table public.curation_votes    enable row level security;
alter table public.address_decisions enable row level security;
alter table public.monitor_snapshots enable row level security;

create policy "profiles readable by authenticated" on public.profiles
  for select to authenticated using (true);
create policy "profiles updatable by owner" on public.profiles
  for update to authenticated using (id = auth.uid());
create policy "profile insertable by owner" on public.profiles
  for insert to authenticated with check (id = auth.uid());

create policy "analyses readable by authenticated" on public.analyses
  for select to authenticated using (true);
create policy "analyses writable by owner" on public.analyses
  for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy "sources readable by authenticated" on public.sources
  for select to authenticated using (true);
create policy "corpus readable by authenticated" on public.corpus_passages
  for select to authenticated using (true);
create policy "snapshots readable by authenticated" on public.monitor_snapshots
  for select to authenticated using (true);

create policy "curation votes readable by authenticated" on public.curation_votes
  for select to authenticated using (true);
create policy "curation votes insertable by authenticated" on public.curation_votes
  for insert to authenticated with check (decided_by = auth.uid());

create policy "address decisions readable by authenticated"
  on public.address_decisions
  for select to authenticated using (true);
create policy "address decisions insertable by authenticated"
  on public.address_decisions
  for insert to authenticated with check (decided_by = auth.uid());

-- sources / corpus_passages / monitor_snapshots have no client-write policy:
-- those are written only by the server via the service_role key.
