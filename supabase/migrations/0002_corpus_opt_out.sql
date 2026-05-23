-- ════════════════════════════════════════════════════════════════════
--  0002 — corpus opt-out model
-- ════════════════════════════════════════════════════════════════════
-- The corpus flips from opt-in to opt-out. Sources were `proposed` and
-- inactive until a human `approved` them; in practice nothing got approved
-- and the corpus was dead weight. New model: every source is `included`
-- and citable by default; a human may `excluded` one (reversible), and may
-- mark one explicitly `verified` — a quality signal, not a gate.
--
--   sources.status       'proposed'|'approved'|'rejected'
--                     -> 'included'|'excluded'   (default 'included')
--   curation_votes.decision  'approved'|'rejected'
--                     -> 'included'|'excluded'|'verified'

-- ── sources.status ────────────────────────────────────────────────────
alter table public.sources drop constraint if exists sources_status_check;

-- Carry existing rows over: an approved source becomes included; anything
-- else (proposed / rejected) also defaults to included — the opt-out flip
-- deliberately activates the previously-inactive corpus. A human re-excludes
-- any source they do not want cited.
update public.sources
   set status = case when status = 'rejected' then 'excluded'
                      else 'included' end;

alter table public.sources alter column status set default 'included';
alter table public.sources
  add constraint sources_status_check
  check (status in ('included', 'excluded'));

-- ── curation_votes.decision ───────────────────────────────────────────
alter table public.curation_votes
  drop constraint if exists curation_votes_decision_check;

-- Translate the historical ledger: an 'approved' vote is now redundant
-- (included is the default) — record it as 'included'; a 'rejected' vote
-- becomes 'excluded'.
update public.curation_votes
   set decision = case when decision = 'rejected' then 'excluded'
                       else 'included' end;

alter table public.curation_votes
  add constraint curation_votes_decision_check
  check (decision in ('included', 'excluded', 'verified'));
