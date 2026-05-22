# Corpus — the reasoning-frame layer

The agent's *facts* come from `tools/` (deterministic). Its *judgement
frame* comes from here: a curated set of authoritative sources the agent
must cite whenever it interprets those facts.

## The curation rule

The agent may **propose** a source — append it to `sources.yaml` with
`status: proposed`. Only a **human** may change a source to
`status: approved`. The agent must never cite a non-approved source, and
must never reason from general internet knowledge in place of the corpus.

This is deliberate. Deciding what counts as authoritative is domain
judgement. If the agent picks its own authorities it drifts toward
popular-but-wrong. The human gate is the point.

## Source tiers (most to least authoritative)

1. `primary` — the law / regulation itself
2. `standard` — attestation & accounting standards
3. `methodology` — rating-agency / supervisory methodology
4. `research` — central-bank / BIS / academic
5. `commentary` — vetted expert commentary; use sparingly

Weight `primary` and `standard` first. For attestation analysis the
examiner reasons from the rules, not from commentary.

## Ingestion

Sources are ingested with structure preserved — section anchors, headings,
page numbers — so a citation can point to `genius-act §4(b)`, not just a
PDF. See `ingest.py`.
