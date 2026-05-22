# Corpus staging

Drop raw source text here as `<source_id>.md`, where `<source_id>` matches
an entry in `../sources.yaml`. Use markdown headings (`#`, `##`) for the
document's sections — `ingest.py` chunks on them, and headings become the
citation anchors.

When you approve a source via `sca curate`, if a matching staged file
exists here it is ingested immediately and becomes retrievable by the agent.

Example — to make `mica-title-iii` citeable:

1. Drop `mica-title-iii.md` here, with the relevant articles under
   headings like `## Article 36 — Reserve of assets`.
2. Run `sca curate`, approve `mica-title-iii`.
3. It ingests automatically; the agent can now cite `mica-title-iii §...`.

Authoritative text only — primary regulation and standards. The corpus is
the agent's reasoning frame; weak sources here become weak citations.
