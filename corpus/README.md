# Corpus — the reasoning-frame layer

The agent's *facts* come from `tools/` (deterministic). Its *judgement
frame* comes from here: a curated set of authoritative sources the agent
must cite whenever it interprets those facts.

## The curation lever (opt-out)

Every source registered in `sources.yaml` is `status: included` and
citable by **default**. A human may opt a source OUT — `status: excluded`
(via `sca curate` or the web F3 Corpus view) — and re-include it at any
time. A human may also mark a source explicitly **verified** — a
quality badge surfaced on its citations, never a gate on whether it is
used. The agent may **propose** a new source; it enters as `included`,
like any other.

This is a deliberate flip from an earlier opt-in design that left the
corpus dead weight because nothing got approved. The opt-out default
activates the reasoning frame; the human keeps the lever to drop a
source that does not belong.

What still holds: facts are deterministic, judgements are cited, and the
agent never invents a figure. The opt-out flip changes the corpus
*inclusion default*, not the citation discipline.

## Source tiers (most to least authoritative)

1. `primary` — the law / regulation itself
2. `standard` — attestation & accounting standards
3. `methodology` — rating-agency / supervisory methodology
4. `research` — central-bank / BIS / academic
5. `commentary` — vetted expert commentary; use sparingly

Weight `primary` and `standard` first. For attestation analysis the
examiner reasons from the rules, not from commentary.

## How candidate sources arrive

Two paths:

1. **Curator hand-add** — a human writes the entry into `sources.yaml`
   directly and stages the text. The classical path; still the right
   choice for foundational documents (laws, accounting standards) the
   curator already has on disk.
2. **Agent-proposed via discovery** — the 6-hourly background thread
   issues six typed Brave-augmented queries (regulator news,
   attestation-standard updates, supervisory speeches, enforcement
   actions, accounting guidance, BIS / IMF research) and surfaces
   candidates a curator can promote or reject from the F3 Corpus view.
   Discovery proposes; the curator disposes. Nothing enters the corpus,
   citable or otherwise, until a human has reviewed it.

The Brave layer is a *lead source* — it shortens the curator's research
loop, it does not replace their judgement. See
`src/sca/discovery_brave.py` for the query set; see
`src/sca/web_discovery.py` for the origin-filter and authoritative-
domain ranking that protects against drive-by S3 PDFs.

## Ingestion

A source only contributes citable text once that text is staged at
`staging/<id>.md` and ingested. Sources are chunked with structure
preserved — section anchors, headings, page numbers — so a citation
can point to `genius-act §4(b)`, not just a PDF. See `ingest.py`.
