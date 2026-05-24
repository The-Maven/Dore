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
2. **Judgements are cited.** A judgement cites the corpus, to the section.
   If the corpus cannot support a claim, say so and stop short — an
   uncited judgement is not allowed.
3. **The human curation lever holds.** The corpus is **opt-out**: every
   registered source is citable by default. A human can exclude a source
   (and may mark one explicitly verified — a badge, not a gate). Address
   verification has two paths: an *auto* path (the contract self-reports
   `symbol()`/`decimals()` and the registry agrees, with a zero-supply
   guard) and a *human* path through `sca curate`. The human vote is
   always sovereign — it wins over any auto entry, and auto never touches
   a deployment a human has already decided on. Anything ambiguous
   (mismatch, unsupported chain, RPC failure) stays flagged for a human;
   auto never papers over a doubt. Surface which path verified an address.
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
      corpus/      LAYER 2 — the curated reasoning frame (opt-out).
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

- **Tests are the contract.** `.venv/bin/python -m pytest -q` — the suite
  must stay **green and hermetic**: fully offline, no network, no database.
  The suite forces the file backend regardless of `.env`. If a change makes
  tests need a network or a DB, the change is wrong.
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

## JavaScript-rendered issuer pages: the AI Context is the answer

Three fiat-backed tokens — **USDP, USDG, EURC** — publish their
attestations behind JavaScript-rendered transparency pages (paxos.com,
circle.com). Doré's static discovery chain (Brave → DDG → domain-scoped
follow-up → locator) cannot extract a PDF URL from a SPA.

**This is a closed question, not a backlog item.** The AI Context path
is the answer for these tokens. The augmentation card leads with the
qualitative picture (Paxos publishes monthly via Withum, Circle via
Deloitte, etc.), the issuer transparency URL is one click away, a
small parsing-difficulty footnote names exactly why automation didn't
fetch the PDF, and on-chain supply still resolves cleanly. The 6-hourly
canary keeps retrying the static chain anyway — if any of these
issuers ever rotate to a server-rendered page or a stable PDF URL,
the override lands automatically without code changes.

**Do not add a headless browser.** Playwright or equivalent means a
~200MB Chromium dependency, a separate worker process to keep the
browser off the FastAPI event loop, retry + timeout logic around flaky
JS pages, and a new failure surface (browser crashes, page-load
timeouts, selector drift each time the issuer redesigns). None of that
is worth three tokens. It's bloat, it's expensive to run, and the AI
Context already does the job for the reader.

If the JS-rendered set ever grows materially (say, a tenth of tracked
tokens) the trade reopens. Until then this is settled.
