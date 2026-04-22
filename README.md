# workflow-agent

Templated agent sandbox framework. Define what a Claude agent can access (policy YAML) and what it should do (role YAML), then launch it in an isolated Docker container against any service — no infrastructure duplication required.

## How it works

1. **Policy file** (`agents/<service>/policies/<name>.yaml`) — declares database connections (with `${ENV_VAR}` credential refs), and an explicit allowlist of Claude tools (e.g. `Bash(psql*)`, `Read`). Plaintext secrets are rejected at load time.

2. **Role file** (`agents/<service>/roles/<name>.yaml`) — declares the system prompt, Claude model, timeout, max turns, optional spec file reference, and whether to send a Discord notification on completion.

3. **Runner** — creates a temporary Docker network per run, connects the agent container and any declared database containers, injects credentials as environment variables, mounts Claude auth read-only, then launches `ghcr.io/zzofrea/workflow-agent:latest` (node:20-slim + python3 + psql + Claude CLI) with `--cap-drop ALL` and no Docker socket. The network and container are cleaned up on exit or timeout.

4. **Output** — the agent's JSON report is archived to `~/agent-output/<service>/<role>_<timestamp>/`. Notifications route through `workflow-notify`.

## CLI

```
# Run an agent role against a service
workflow-agent run auditor --target defendershield-etl

# Override model, timeout, or max turns
workflow-agent run auditor --target defendershield-etl --model claude-opus-4-5 --timeout 600

# List available roles for a service
workflow-agent list --target daily-briefing

# Validate policy + role files and check that database containers are running
workflow-agent validate --target open-brain --role auditor
```

`run` exits 1 on `fail` or `error` outcome, making it safe to use as a CI gate or cron step.

## Agent definitions

```
agents/
  <service>/
    policies/   # access policies (DB connections, allowed tools)
    roles/      # behavioral roles (system prompt, model, timeout)
    specs/      # plain-text spec files referenced by roles
```

Defined services: `defendershield-etl`, `open-brain`, `daily-briefing`, `bid-scraper`.

## Installation

```bash
pip install -e .
```

Requires Docker on the host (container launches via subprocess). Claude auth must exist at `~/.claude/` before running.

## Container image

`container/Dockerfile` — node:20-slim base with Python 3, psql client, and the Claude Code CLI installed globally. Built locally:

```bash
docker build -t ghcr.io/zzofrea/workflow-agent:latest container/
```

## Output location

`~/agent-output/<service>/<role>_<timestamp>/` — each run produces a JSON report and a structured log. Promtail tails this directory tree and ships logs to Loki.
