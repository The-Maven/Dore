# Corpus staging

Drop raw source text here as `<source_id>.md`, where `<source_id>` matches
an entry in `../sources.yaml`. Use markdown headings (`#`, `##`) for the
document's sections — `ingest.py` chunks on them, and headings become the
citation anchors.

The corpus is opt-out: every registered source is citable by default, but
a source only contributes *text* to a citation once its text is staged
here and ingested. `sca curate` (and the web Corpus view) ingests any
staged file when you take an include/verify action on a source.

Example — to make `mica-title-iii` produce citable passages:

1. Drop `mica-title-iii.md` here, with the relevant articles under
   headings like `## Article 36 — Reserve of assets`.
2. Run `sca curate` and pick `[i]nclude` (or `[v]erify`) on
   `mica-title-iii`. Staged text is ingested automatically; the agent
   can now cite `mica-title-iii §...`.

Authoritative text only — primary regulation and standards. The corpus
is the agent's reasoning frame; weak sources here become weak citations.
