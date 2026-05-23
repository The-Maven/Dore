-- Doré — 0003 attestation URL overrides.
--
-- Non-destructive: adds a new table alongside existing ones; nothing is
-- altered or dropped. Safe to run on a live database without coordinating
-- a maintenance window.
--
-- Why this table exists:
--   `config/stablecoins.yaml` carries a `latest_attestation_url` as a
--   bootstrap seed — convenient for dev, but brittle in production: a
--   freshly-discovered URL would be lost on the next redeploy. This table
--   makes the operational URL DURABLE: discoveries from the background
--   canary and human curation both land here, and `attestation_fetch`
--   reads this before the YAML seed.
--
--   Append-only. Each row is a discovery / curation event with full
--   provenance (via, set_by, notes). The "current" URL for a symbol is
--   the most-recent row.

create table if not exists public.attestation_url_overrides (
  id          uuid primary key default gen_random_uuid(),
  symbol      text not null,
  url         text not null,
  via         text not null default 'manual'
                check (via in (
                  'manual', 'web_search', 'locator',
                  'paxos_resolver', 'seed', 'import'
                )),
  set_by      uuid references public.profiles (id) on delete set null,
                -- null = automatic discovery; non-null = a curator's user_id
  notes       text not null default '',
  set_at      timestamptz not null default now()
);

create index if not exists attestation_url_overrides_symbol
  on public.attestation_url_overrides (symbol, set_at desc);

-- RLS: read by any authenticated user; insert by any authenticated user.
-- The service_role key (server) bypasses RLS for the automatic-discovery
-- write path. Curators write through the server's POST endpoint.
alter table public.attestation_url_overrides enable row level security;

create policy "attestation overrides readable by authenticated"
  on public.attestation_url_overrides
  for select to authenticated using (true);

create policy "attestation overrides insertable by authenticated"
  on public.attestation_url_overrides
  for insert to authenticated with check (set_by = auth.uid());
