# AGENTS.md — working on Doré

Instructions for any AI agent (or human) contributing to this codebase. Read
this before changing anything. The product overview is in `README.md`; the
deep design is `docs/architecture-booklet.html`.

## What Doré is, in one line

A stablecoin reserve-verification system: it reconciles issuers' *claimed*
reserves against *on-chain reality*, across three surfaces — attestation,
sanctions, redemption.

## The non-negotiable principles

These are the spine of the product. Violating one is a real defect, not a
style nit.

1. **Facts are deterministic. Never invent a figure.** Every number a user
   sees must come from a deterministic check (`src/sca/tools/`, `validation`,
   `metrics`). No estimating, no rounding into existence, no LLM-guessed
   numbers. This extends to the UI: no fabricated motion, no random-walked
   values, no placeholder data dressed as real.
2. **Judgements are cited.** A judgement cites the approved corpus, to the
   section. If the corpus cannot support a claim, say so and stop short — an
   uncited judgement is not allowed.
3. **The human curation gate holds.** Code (and the agent) may *propose* a
   corpus source or flag an address; only a human *approves*. Never add a
   path that auto-approves curation.
4. **`n/a` is a valid, honest answer.** When data cannot be verified, surface
   it plainly. A clearly-labelled gap is a finding. Never paper over it.
5. **The audience is financial / compliance, not engineers.** User-facing
   output must not expose system internals — no "tool", "endpoint", "MCP",
   no technical identifiers. Provenance reads as "on-chain data" / "the
   attestation". (Internal mechanisms may keep technical names; transform
   them at the display layer.)

## The codebase

    src/sca/
      tools/       LAYER 1 — facts. Deterministic, no LLM judgement.
      corpus/      LAYER 2 — the human-gated reasoning frame.
      agent/       LAYER 3 — synthesis: analyze / screen_token / assess_redemption.
      store/       persistence: a Store interface; FileStore + SupabaseStore.
      validation.py  deterministic guardrails (Check / Gap).
      models.py    typed dataclasses — the boundaries between layers.
      mcp_server.py  the read-only MCP surface for the Hermes agent.
      cli.py, theme.py, cache.py, llm/, config.py
    web/           FastAPI server + the terminal SPA (web/static/).
    agent/         the Hermes home — SOUL.md, config.yaml, skills, GUARDRAILS.md.
    config/, corpus/, supabase/, skill/, evals/, tests/, docs/, brand/

## Working rules

- **Tests are the contract.** `.venv/bin/python -m pytest -q` — ~125 tests,
  and they must stay **green and hermetic**: fully offline, no network, no
  database. The suite forces the file backend regardless of `.env`. If a
  change makes tests need a network or a DB, the change is wrong.
- **The `Store` abstraction.** Durable state goes through `get_store()` —
  never write files or hit Supabase directly from feature code. `FileStore`
  preserves offline behaviour; `SupabaseStore` is production. New durable
  state means a Store method + a migration, not an ad-hoc write.
- **Don't break the core loop.** The deterministic pipeline (supply →
  attestation → metrics → guardrails → corpus → synthesis → citation
  verification) is the audited heart. The agent layer sits *on top* of it;
  it does not replace it.
- **Secrets** live only in `.env` (gitignored). Never commit them, never
  hardcode them, never print them.
- **The design system** is the gold/dark terminal aesthetic (`theme.py`,
  `web/static/app.css`). Keep it consistent; the brand wordmark is in
  `brand/`.

## Running things

    .venv/bin/python -m pytest -q                              # tests
    .venv/bin/python -m uvicorn web.server:app --port 8000     # web app
    .venv/bin/sca analyze USDC                                 # CLI
    .venv/bin/python -m sca.mcp_server                         # the MCP server

## The agent (Hermes) integration

Doré's conversational analyst runs on `nousresearch/hermes-agent`. It is a
*constrained* agent: its only capability is the read-only `dore` MCP server.
Before touching that integration, read `agent/GUARDRAILS.md` in full — the
boundary is layered and deliberate.
