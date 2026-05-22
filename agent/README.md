# agent/ — the Doré agent home

This directory is the **Hermes home** for Doré's conversational analyst. On
Railway it is mounted as a persistent volume at the agent's home path
(default `~/.hermes`); locally it is the same layout.

## Layout

| Path | What it is |
|---|---|
| `SOUL.md` | The agent's identity — loaded first in its system prompt. The heart of the business. |
| `config.yaml` | Hermes configuration — the DeepSeek model and the `dore` MCP server. |
| `.env.example` | Template for the home's secrets. Copy to `.env`; never commit the real one. |
| `GUARDRAILS.md` | The security model — read this before changing anything. |
| `skills/` | Agent skills in the `agentskills.io` format. `skills/dore/compliance-analyst/` is the operating procedure. |
| `memories/` | Hermes-managed persistent memory (created at runtime). |
| `logs/`, `sessions/`, `cron/` | Hermes-managed at runtime. |

## How it is wired

```
  user ──▶ F7 console (web UI) ──▶ Doré server bridge ──▶ Hermes agent
                                                            │
                                          SOUL.md + skills/ ┘
                                                            │
                                          dore MCP server ◀─┘   (read-only)
                                                            │
                                              the sca package — Doré's
                                          deterministic verification engine
```

The agent reasons; it reaches the system **only** through the read-only
`dore` MCP server. It cannot write the database, run code, or read secrets.
See `GUARDRAILS.md`.

## Running it (deployment)

The agent is installed and run separately from the Doré web app — see the
repository `Dockerfile` and its commentary. Docker is not required to *build*
the integration; it is the deployment path. To run Hermes directly:

```
pip install hermes-agent && hermes postinstall
HERMES_HOME=/path/to/this/agent  hermes        # classic CLI
HERMES_HOME=/path/to/this/agent  hermes --tui  # TUI
```

`hermes model` walks through provider setup if `config.yaml` needs it;
`hermes doctor` diagnoses a broken environment.
