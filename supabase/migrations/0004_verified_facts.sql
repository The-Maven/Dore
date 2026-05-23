-- Doré — 0004 verified facts (immutable, time-series, object-agnostic).
--
-- NON-DESTRUCTIVE. Adds a new table alongside existing ones. Nothing is
-- altered or dropped; existing `analyses` and `monitor_snapshots` keep
-- their roles as the operational cache. This table is the AUDIT TRAIL.
--
-- The shape is deliberately generic so Lens 2 (agent payments — future)
-- uses the same table without a schema change. `claim_type` is the
-- discriminator: today 'reserves' / 'supply' / 'sanctions_hit';
-- tomorrow 'agent_payment' / 'authorisation'.
--
-- Discipline:
--   - Append-only. Corrections insert a new row and link `superseded_by`;
--     the original is never updated or deleted.
--   - Content-addressed via `content_hash` (sha256 of canonical value +
--     sources). Lets the writer detect "we already have this exact fact"
--     cheaply and avoid duplicates when a re-read confirms the prior.
--   - Point-in-time reproducibility: latest fact per
--     (claim_type, subject) with `observed_at <= '<past date>'` returns
--     exactly what the system reported then.

create table if not exists public.verified_facts (
  id            uuid primary key default gen_random_uuid(),
  claim_type    text not null,
                  -- Lens 1: 'reserves' | 'supply' | 'sanctions_hit' | 'attestation_url'
                  -- Lens 2 (future): 'agent_payment' | 'authorisation'
  subject       text not null,
                  -- the noun the claim is about; conventionally
                  --   'reserves'      -> '<SYMBOL>'           (e.g. 'USDC')
                  --   'supply'        -> '<SYMBOL>:<chain>'   (e.g. 'USDC:ethereum')
                  --   'sanctions_hit' -> '<address>'
  value         jsonb not null,
                  -- the fact itself — generic; numeric values + units typed inside
  sources       jsonb not null,
                  -- [{kind, url, ref, fetched_at}] — every source we reconciled against
  block_number  bigint,                -- nullable for off-chain facts
  chain         text,                  -- nullable
  observed_at   timestamptz not null default now(),  -- when WE observed it
  as_of         timestamptz,           -- the fact's own asserted timestamp
  status        text not null
                  check (status in ('verified', 'unverified', 'assumed')),
  content_hash  text not null,         -- sha256(canonical(value || sources))
  superseded_by uuid references public.verified_facts(id),
  notes         text not null default ''
);

-- Subject-recency lookup (the dominant query: "latest fact for X").
create index if not exists verified_facts_subject_observed_at_idx
  on public.verified_facts (subject, observed_at desc);

-- Claim-type filter (e.g. "all reserves facts").
create index if not exists verified_facts_claim_type_idx
  on public.verified_facts (claim_type);

-- Content-hash dedup (so a writer can skip the insert when the previous
-- row's hash matches; the second-read confirmation just updates
-- observed_at on the existing row through a separate path, NOT this
-- table — this table is append-only).
create index if not exists verified_facts_content_hash_idx
  on public.verified_facts (content_hash);

-- RLS: read by any authenticated user; insert by the server only (the
-- service-role key bypasses RLS). Client direct-writes are deliberately
-- blocked — the deterministic pipeline is the only legitimate writer.
alter table public.verified_facts enable row level security;

create policy "verified facts readable by authenticated"
  on public.verified_facts
  for select to authenticated using (true);

-- No client INSERT policy; only the server (service_role) writes.
