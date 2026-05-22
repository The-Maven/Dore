# ─────────────────────────────────────────────────────────────────────────
# Doré — by Rayleigh Stark. Deployment image.
#
# This image carries three things in one container:
#   1. the Doré web app (FastAPI / uvicorn)            — the terminal UI + API
#   2. the Doré MCP server (`dore-mcp`)                — the agent's read-only
#                                                        window onto the system
#   3. the Hermes agent runtime (`hermes-agent`)       — the F7 analyst
#
# Docker is NOT needed to build the integration — it is the deploy path.
# Target: Railway (or any container host). Mount a persistent volume at
# /app/agent so the Hermes home (SOUL.md, skills, memories, logs) survives.
#
# Hermes pins Python 3.11, so the image does too. Doré itself runs on 3.10+.
# ─────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# System packages the Hermes runtime expects. `hermes postinstall` would
# normally fetch these; installing them here keeps the image reproducible.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ripgrep ffmpeg curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Doré, with the `agent` extra — this installs `mcp` and the `dore-mcp`
# console script that the Hermes config (agent/config.yaml) spawns.
RUN pip install --no-cache-dir -e ".[agent,supabase,pdf]"

# The Hermes agent runtime, installed separately — Doré does not depend on it
# as a library; Hermes runs as its own process and reaches Doré only through
# the read-only `dore` MCP server. See agent/GUARDRAILS.md.
RUN pip install --no-cache-dir hermes-agent

# This repo's agent/ directory IS the Hermes home (SOUL.md, config.yaml,
# skills/). On Railway, mount a persistent volume here so memories and logs
# persist across restarts.
ENV HERMES_HOME=/app/agent

# Secrets are NOT baked in. On Railway set service variables:
#   OPENAI_API_KEY  (the DeepSeek key — Hermes' model provider)
#   LLM_API_KEY, SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY
# and let agent/.env be provided by the mounted volume or the platform.

EXPOSE 8000

# Starts the web app. The F7 console's bridge invokes the Hermes agent, which
# in turn spawns the `dore-mcp` server. For a first deploy, also run, once:
#   hermes postinstall   — completes Hermes' own environment setup
#   hermes doctor        — verifies what is present / missing
#   hermes config check  — confirms config.yaml keys (esp. the guardrail
#                          tool-restriction keys noted in agent/config.yaml)
# If you prefer Hermes as its own always-on service, split it into a second
# Railway service sharing the mounted agent/ volume.
CMD ["python", "-m", "uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
