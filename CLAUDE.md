# CLAUDE.md

Guidance for Claude Code working in this repository.

**Read `AGENTS.md` first** — it is the canonical guide to the codebase: the
architecture, the non-negotiable principles, the working rules, and how to
run things. Everything there applies.

## Claude-specific notes

- **The test suite is the contract.** Run `.venv/bin/python -m pytest -q`
  after any change to `src/`. ~125 tests; they must stay green and hermetic
  (offline — no network, no database). Do not introduce a test that needs
  either.
- **Never commit or echo secrets.** `.env` holds live API keys and is
  gitignored. Don't read it aloud, hardcode its values, or stage it.
- **Verify, don't assume.** When work spans the web UI, load it in a browser
  and look — screenshots, not just HTTP 200s. When it spans the agent
  boundary, check `agent/GUARDRAILS.md` holds. An agent's summary describes
  intent; confirm the actual change.
- **The five principles in `AGENTS.md` are defects if violated**, not
  preferences: deterministic facts / cited judgements / the human curation
  gate / `n/a` is honest / financial-audience output. Hold all five.
- **Don't replace the deterministic core.** The audited pipeline is the
  product. New capability layers on top of it.
- **Ask before remote or destructive actions** — pushing, force-pushing,
  deleting branches, dropping tables. Local edits and tests are free; things
  others see are not.
