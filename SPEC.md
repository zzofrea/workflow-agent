# workflow-agent: Templated Agent Sandbox Framework

## System Overview

workflow-agent is a CLI framework that lets Zack define **agent roles** (what an
agent does) and **access policies** (what an agent can reach), then launch
sandboxed Claude instances that execute those roles against his services. It
generalizes the behavioral auditor pattern from workflow-platform into a reusable
template system where the same container infrastructure can run audits, analyses,
reports, or any other task -- just by swapping the role definition and policy.

**Who it serves:** Zack (sole operator), invoked manually or by workflow-platform
orchestrator.

**Why it exists:** Every data service Zack runs (bid scraper, ETL, future
services) could benefit from an autonomous insight layer -- not just pass/fail
audits, but trend analyses, anomaly detection, summary reports, idea generation.
The auditor proved the sandboxed-Claude-with-scoped-tools pattern works. This
framework makes it reusable without copy-pasting Dockerfiles and shell scripts.

## Behavioral Contract

### Primary flows

- When the operator defines a policy file for a service, it declares database
  connections, tool permissions, and any other resource access. The policy grants
  nothing by default -- every permission is explicit opt-in.

- When the operator defines a role file for an agent, it declares a system
  prompt, an output format, and references a policy. The role describes what the
  agent should do and how it should report results.

- When the operator runs `workflow-agent run <role> --target <service>`, the
  framework validates the role and policy files, resolves all database container
  hostnames to Docker container names, verifies each target container is running,
  creates a temporary Docker network, connects the required database containers
  to it, launches a sandboxed Claude container with the scoped tools and
  credentials from the policy, and waits for the agent to complete.

- When the agent completes, its output is archived to
  `~/agent-output/<service>/<role>_<timestamp>/` and optionally routed through
  workflow-notify.

### Error flows

- When any target database container is not running, the framework fails
  immediately before launching the agent container. An error report is archived
  and the operator is notified.

- When the agent container exceeds the configured timeout (default: 5 minutes),
  the container is killed, an error report is generated, and the operator is
  notified.

- When a policy or role file is missing, malformed, or references nonexistent
  resources, the framework fails immediately with a descriptive error. No
  container is launched.

- When a policy references an environment variable via `${VAR_NAME}` syntax and
  that variable is not set in the host environment, the framework fails
  immediately with a clear error naming the missing variable. No container is
  launched.

- When the agent produces output that cannot be parsed as the expected format,
  the raw output is preserved in the archive for manual review.

### Boundary conditions

- When the temporary Docker network cannot be created or a container cannot be
  connected, the framework fails before launching the agent.

- When the framework is interrupted (SIGINT/SIGTERM), it cleans up the temporary
  network and any connected containers before exiting.

- When the Claude CLI auth is expired or missing, the agent container exits
  non-zero and the framework reports the auth failure.

## Explicit Non-Behaviors

- The system must not grant any permissions by default because the security model
  is explicit opt-in only. A policy with no databases and no tools means the
  agent gets no databases and no tools.

- The system must not connect the agent container to dokploy-network because that
  would expose all services. Only specifically listed database containers are
  bridged onto the temporary network.

- The system must not implement agent-to-agent communication because standalone
  jobs are simpler and sufficient for current use cases.

- The system must not implement long-running daemon agents because run-once-and-
  exit is the current design target. Timeout enforcement assumes finite jobs.

- The system must not retry failed agent runs because the operator debugs
  failures manually and retries are handled by re-invocation or the next
  scheduled run.

- The system must not add a plugin system, provider pattern, or generic
  abstraction layer because the framework is simple file-based configuration
  (policy YAML + role YAML + spec markdown). Over-engineering the config layer
  defeats the purpose.

- The system must not store plaintext secrets in any file that is committed to
  git (policy YAML, role YAML, specs, or any other config). All secrets use
  `${VAR_NAME}` syntax and are resolved from host environment variables at
  runtime. This is a hard security requirement (constitution rule 17).

- The system must not manage database roles or credentials because those are
  created manually per service (e.g., `auditor_ro` in Postgres). The framework
  only consumes credentials, never creates them.

- The system must not implement composable/inheritable roles because roles are
  standalone definitions. If two roles need similar instructions, duplication is
  acceptable (constitution rule 29: three similar lines beats a premature
  abstraction).

- The system must not implement non-claude-cli runtimes in v1 because the
  tool-use agentic loop is complex and Claude CLI handles it reliably. The
  `runtime` field in the role file is a one-field extension point, not a plugin
  system. Adding a second runtime is a future spec.

## Integration Boundaries

### Docker Engine (host-side)

- **Operations:** Create/remove temporary networks, connect/disconnect
  containers, resolve container hostnames on dokploy-network, run agent
  containers, inspect container state, kill timed-out containers.
- **Unavailability:** Docker daemon down = immediate failure, no retry.
- **Security:** No Docker socket mounted into agent containers. Host-side code
  runs Docker commands via subprocess.

### Target Databases (via temporary Docker network)

- **Inbound to agent:** Connection via database client (psql for Postgres,
  other clients as needed). Connection details passed as environment variables.
- **Multiple databases:** A single policy can declare multiple database
  connections. Each target container is connected to the temporary network with
  its hostname as an alias. Environment variables are namespaced per connection
  (e.g., `DB_1_HOST`, `DB_1_PORT`, ... or `PGHOST`/`PGPORT` for the primary
  Postgres connection with standard libpq vars).
- **Non-Postgres databases:** Supported by declaring the database type in the
  policy. The container image must include the appropriate client. The base image
  ships with `psql`; additional clients can be added by extending the
  Dockerfile or by building role-specific images.
- **Unavailability:** Host-side check verifies each database container is running
  before launching the agent. If any is down, fail immediately with error.
- **Security:** Credentials via environment variables only (constitution rule 17).
  Policy files use `${VAR_NAME}` references, never plaintext secrets. The
  framework resolves these from the host environment at runtime and injects them
  as container env vars. Database roles enforce access level (e.g., SELECT-only
  for read-only access). The framework does not enforce DB-level permissions --
  that is the operator's responsibility when creating the database role.

### Anthropic API (via temporary Docker network with outbound NAT)

- **Outbound from agent:** Claude CLI calls Anthropic API for inference.
- **Network:** Temporary network is a standard (non-internal) Docker bridge,
  providing outbound NAT. The agent has internet access for the API.
- **Unavailability:** Claude CLI fails, agent container exits non-zero, framework
  generates error report.
- **Security:** API key in Claude auth files, copied from read-only mount to
  writable home at container startup (existing pattern from auditor). Outbound
  exfiltration mitigated by scoped `--allowedTools` -- only tools declared in
  the policy are available.

### workflow-notify (host-side, optional)

- **Outbound:** Fanout notification with service name, role, severity, summary.
- **Format:** Python function call to `workflow_notify.fanout()`.
- **Unavailability:** Notification failure is logged but does not fail the run.
  The output is still archived.

### Dokploy API (host-side, optional)

- **Operations:** Query service status, resolve compose container names, future
  integration with workflow-platform orchestrator.
- **Format:** HTTP API with `x-api-key` header.
- **Unavailability:** Framework falls back to direct Docker commands if Dokploy
  API is unreachable.

## File Structure

```
workflow-agent/
  agents/
    bid-scraper/
      policies/
        reader.yaml          # Read-only access to bid scraper DB
      roles/
        auditor.yaml          # Behavioral audit (port of existing auditor)
        analyst.yaml          # Analysis/insights role
      specs/
        audit.md              # Given/When/Then behavioral spec
        analysis.md           # Analysis instructions/prompts
    defendershield-etl/
      policies/
        reader.yaml
      roles/
        auditor.yaml
      specs/
        audit.md
  src/
    workflow_agent/
      __init__.py
      cli.py                  # CLI entry point
      runner.py               # Container lifecycle (network, launch, cleanup)
      policy.py               # Policy file parsing and validation
      role.py                 # Role file parsing and validation
      config.py               # Framework config (archive paths, defaults)
      output.py               # Output archival and notification routing
  container/
    Dockerfile                # Base agent container image
    entrypoint.py             # Container-side: auth setup, Claude invocation
  tests/
  pyproject.toml
  PROJECT.md
```

## Policy File Format

```yaml
# agents/bid-scraper/policies/reader.yaml
name: bid-scraper-reader
description: Read-only access to bid scraper database

databases:
  - name: primary                     # Logical name, used for env var prefix
    type: postgres                    # Database type (determines client + env vars)
    hostname: gov-bid-postgres        # Docker hostname on dokploy-network
    port: 5432
    database: govbids
    user: auditor_ro
    password: ${BID_SCRAPER_DB_PASSWORD}  # Resolved from host env at runtime
    env_prefix: PG                    # Generates PGHOST, PGPORT, etc.

tools:
  - "Read"
  - "Bash(psql*)"
  - "Bash(python3*)"
  - "Bash(date*)"
```

For multiple databases:

```yaml
databases:
  - name: bids
    type: postgres
    hostname: gov-bid-postgres
    port: 5432
    database: govbids
    user: auditor_ro
    password: ${BID_SCRAPER_DB_PASSWORD}  # Resolved from host env at runtime
    env_prefix: PG              # Primary: PGHOST, PGPORT, etc.

  - name: etl
    type: postgres
    hostname: ds-etl-postgres
    port: 5432
    database: defendershield
    user: auditor_ro
    password: trust             # Literal "trust" = no password (trust auth)
    env_prefix: ETL_DB          # Secondary: ETL_DB_HOST, ETL_DB_PORT, etc.

tools:
  - "Read"
  - "Bash(psql*)"
  - "Bash(python3*)"
  - "Bash(date*)"
```

**Credential resolution rules:**
- `${VAR_NAME}` -- resolved from host environment at runtime. Fails fast if unset.
- `trust` -- treated as no password (trust authentication). No env var needed.
- Any other literal string -- rejected. The framework refuses to pass plaintext
  secrets. All non-trust passwords must use `${VAR_NAME}` syntax.

## Role File Format

```yaml
# agents/bid-scraper/roles/auditor.yaml
name: bid-scraper-auditor
description: Behavioral audit of bid scraper service
policy: reader                        # References policies/reader.yaml
spec: audit.md                        # References specs/audit.md (optional)

runtime: claude-cli                   # Agent runtime (v1: claude-cli only)
model: sonnet                         # Model name (interpreted by runtime)
max_turns: 50                         # Max agentic turns (default: 50)
timeout: 300                          # Seconds before kill (default: 300)
notify: true                          # Send workflow-notify on completion

output_format: json                   # Expected output: json, markdown, or text

system_prompt: |
  You are a behavioral auditor. You verify that a running service meets its
  specification by querying its database and checking the results. You act like
  a user or downstream consumer of this service -- you check observable outcomes,
  not implementation details.

  You have psql access to the service database (connection details are in your
  environment variables). Read the spec, run queries to verify each scenario,
  and produce a JSON report.

  Output format -- respond with ONLY a JSON object (no markdown fencing, no
  extra text):
  {
    "scenarios": [
      {
        "id": 1,
        "description": "Brief description of the scenario",
        "status": "pass" | "fail" | "error",
        "observation": "What you actually observed",
        "evidence": "Concrete data: query results, counts, timestamps, etc.",
        "expected": "What the spec says should happen"
      }
    ],
    "summary": "One-line overall assessment"
  }
```

An analyst role using the same policy:

```yaml
# agents/bid-scraper/roles/analyst.yaml
name: bid-scraper-analyst
description: Generate insights from bid scraper data
policy: reader
spec: analysis.md

runtime: claude-cli
model: sonnet
max_turns: 30
timeout: 300
notify: true
output_format: markdown

system_prompt: |
  You are a procurement data analyst. You have read-only access to a government
  bid tracking database. Your job is to produce a concise, actionable summary
  of recent activity.

  You have psql access (connection details in environment variables). Read the
  analysis spec, query the data, and produce a markdown report covering:
  - New opportunities in the last 7 days
  - Upcoming bid deadlines
  - Any notable patterns or anomalies

  Write your report as clean markdown. Be concise and data-driven.
```

## CLI Interface

```
workflow-agent run <role> --target <service> [options]

Arguments:
  role                    Role name (e.g., "auditor", "analyst")
  --target SERVICE        Service directory under agents/ (e.g., "bid-scraper")

Options:
  --model MODEL           Override role's Claude model
  --timeout SECONDS       Override role's timeout (default: 300)
  --max-turns N           Override role's max turns (default: 50)
  --no-notify             Skip workflow-notify notifications
  --dry-run               Validate policy + role files without launching container

workflow-agent list --target <service>
  List available roles for a service.

workflow-agent validate --target <service> --role <role>
  Validate policy and role files, check database containers are running.
```

## Behavioral Scenarios

### Happy Path

#### Scenario 1: Existing auditor behavior is preserved

GIVEN the bid-scraper auditor role and reader policy are defined.
AND the bid scraper database is running with populated tables.
WHEN the operator runs `workflow-agent run auditor --target bid-scraper`.
THEN a sandboxed container launches with psql, python3, date, and Read tools.
AND the agent produces a JSON report with pass/fail results for each scenario.
AND the report is archived to `~/agent-output/bid-scraper/auditor_<timestamp>/`.
AND a Discord notification is sent with the overall result.

#### Scenario 2: New analyst role produces a report

GIVEN a bid-scraper analyst role and reader policy are defined.
AND the bid scraper database is running.
WHEN the operator runs `workflow-agent run analyst --target bid-scraper`.
THEN a sandboxed container launches with the same database access as the auditor.
AND the agent produces a markdown report with recent opportunity insights.
AND the report is archived to `~/agent-output/bid-scraper/analyst_<timestamp>/`.

#### Scenario 3: Policy with multiple databases connects all targets

GIVEN a policy declares two database connections (bids + etl).
AND both database containers are running.
WHEN the operator runs an agent with this policy.
THEN the temporary network has both database containers connected.
AND the agent container has environment variables for both connections.
AND the agent can query both databases.

#### Scenario 4: Dry run validates without launching

GIVEN valid role and policy files exist for a service.
WHEN the operator runs `workflow-agent validate --target bid-scraper --role auditor`.
THEN the framework parses and validates both files.
AND confirms each database container is running.
AND logs the resolved policy (databases, tools) for operator review.
AND no container is launched.

### Error Path

#### Scenario 5: Database container is not running

GIVEN a policy references a database whose container is stopped.
WHEN the operator runs an agent.
THEN the framework detects the stopped container before launching.
AND an error report is archived with a clear message.
AND the operator is notified.

#### Scenario 6: Role references nonexistent policy

GIVEN a role file references a policy name that does not exist.
WHEN the operator runs the role.
THEN the framework fails immediately with a file-not-found error.
AND no container is launched.

### Edge Cases

#### Scenario 7: Trust authentication (no password)

GIVEN a policy declares a database with `password: trust`.
WHEN the agent container is launched.
THEN the password environment variable is omitted or empty.
AND the agent connects successfully via trust auth.

#### Scenario 8: Agent times out

GIVEN an agent is running but exceeds the configured timeout.
WHEN the timeout is reached.
THEN the container is killed.
AND an error report is archived with the timeout reason.
AND the temporary network is cleaned up.
AND the operator is notified.

#### Scenario 9: Agent output cannot be parsed

GIVEN an agent completes but produces output that does not match the expected
format (e.g., JSON role but output is prose).
WHEN the output parser runs.
THEN the raw output is preserved in the archive.
AND the report is marked as "incomplete" with the raw output for manual review.

## Definition of Done

- [ ] Acceptance tests pass (scenarios 1-9 above, mocked Docker commands)
- [ ] Unit/integration tests pass (policy parsing, role parsing, env var
      construction, hostname resolution, output archival)
- [ ] Existing auditor can be expressed as a role + policy with identical
      behavior to today's `workflow-audit run`
- [ ] A new analyst role can be defined and executed against bid-scraper
- [ ] Deployed as installable CLI (`workflow-agent`) via pyproject.toml
- [ ] Output archived to `~/agent-output/<service>/<role>_<timestamp>/`
- [ ] Docker image built and tagged (`workflow-agent:latest`)
- [ ] Monitoring: Discord notifications flowing for all roles
- [ ] Logging: structlog events for policy validation, container launch, network
      setup/teardown, timeout, and all failure modes
- [ ] Execution log includes resolved policy summary (databases, tools) for
      debugging

## PROJECT.md

```markdown
# workflow-agent: Templated Agent Sandbox Framework

## Objective

Generalize the behavioral auditor pattern into a reusable framework where agent
roles (what to do) and access policies (what to reach) are defined as simple
YAML files. Launch sandboxed Claude instances against any service without
duplicating Docker/networking infrastructure.

## Acceptance Criteria

- Auditor role reproduces identical behavior to workflow-platform's workflow-audit
- New roles (analyst, etc.) can be defined in under 10 minutes
- Policy/role validation catches errors before any container launches
- All database containers are verified running before agent launch
- Temporary Docker network isolates agent from dokploy-network
- Output archived to standardized path with optional Discord notification
- 5-minute default timeout with kill + cleanup on expiry

## Constraints

- Explicit opt-in permissions only (nothing by default)
- Credentials via environment variables, never baked into images
- No Docker socket inside agent containers
- Cap-drop ALL on agent containers
- Single temporary Docker network per run (non-internal, outbound NAT)
- Claude auth via read-only mount copied to writable home at startup
- Base image: node:20-slim + python3 + psql + Claude CLI

## What "Done" Means

- All acceptance and unit tests pass
- Auditor role + reader policy defined for bid-scraper and ETL
- At least one non-auditor role (analyst) defined and working
- CLI installable via pip (pyproject.toml entry point)
- Docker image builds and runs successfully

## Out of Scope

- Agent-to-agent communication
- Long-running daemon agents
- Database role/credential management
- Composable/inheritable roles
- Retry logic for failed runs
- Plugin systems or provider abstractions
- Migration of workflow-platform's auditor (it continues to work; this is a new
  standalone tool that can eventually replace it)
```

## Resolved Decisions

### 1. Container image strategy: single base image

A single base Docker image (`workflow-agent:latest`) with psql, python3, and
Claude CLI. If a role needs additional tools (e.g., `mysql` client, `curl`,
`git`), add them to the base image. Revisit with per-role Dockerfiles only if
image size or conflicting dependencies become a problem.

### 2. Env var namespacing for multiple databases

The first database with `env_prefix: PG` gets the standard libpq vars
(`PGHOST`, `PGPORT`, etc.) that psql reads automatically. Additional databases
use custom prefixes (e.g., `ETL_DB_HOST`, `ETL_DB_PORT`) and the system prompt
tells the agent about them. The agent uses explicit `-h`/`-p` flags for
non-primary databases.

### 3. Standalone first, orchestrator integration later

workflow-agent is a standalone CLI. The workflow-platform orchestrator currently
calls `workflow-audit run` directly. Migration of the orchestrator to call
`workflow-agent run` instead is a future task, not part of this spec. The
interfaces are designed to be compatible.

### 4. Postgres-only in v1

The base image ships only `psql`. The policy format supports declaring other
database types, but no additional clients are installed. Non-Postgres support
is a future capability.

### 5. Runtime field: claude-cli only in v1, designed for future providers

The role file includes a `runtime` field that determines which agent runtime
manages the tool-use loop. In v1, only `claude-cli` is implemented (invokes
`claude --print --dangerously-skip-permissions --allowedTools ...`). The field
exists so future runtimes (e.g., `litellm` for local models via Ollama on the
M4 Pro, or direct API calls) can be added without changing the role file format
or the container lifecycle. The entrypoint dispatches on `runtime` to select
the appropriate agent loop.

Future example (not implemented in v1):
```yaml
runtime: litellm
model: qwen2.5:32b
endpoint: http://mac-mini:11434       # Ollama on M4 Pro
```

### 6. No plaintext secrets in config files

All credentials use `${VAR_NAME}` syntax in policy YAML, resolved from host
environment variables at runtime. The only accepted literal password value is
`trust` (for trust authentication). The framework rejects any other plaintext
password string.

## Remaining Ambiguity Warnings

No unresolved ambiguities. All design decisions have been made.

## Implementation Constraints

- **Language:** Python 3.12+
- **Test runner:** pytest
- **Logging:** structlog
- **Config parsing:** PyYAML + pydantic for validation
- **Credential resolution:** `${VAR_NAME}` syntax in policy YAML, resolved via
  `os.environ` at runtime. Fail fast with `ValueError` if unset. Literal
  `trust` is the only accepted non-env-var password value.
- **Agent runtime:** `claude-cli` in v1. Entrypoint dispatches on `runtime`
  field from role config. Single code path today, but the dispatch structure
  makes adding `litellm` or other runtimes a localized change.
- **CLI framework:** argparse (matches workflow-platform pattern)
- **Docker image:** Based on node:20-slim (for Claude CLI) + python3 +
  postgresql-client
- **Linting:** ruff check + ruff format + pyright

## Constitution Compliance

| Rule | How Satisfied |
|------|---------------|
| 1-6 (Specs before code, Given/When/Then) | 9 behavioral scenarios in plain domain language, external observables only |
| 7 (Human approval before implementation) | This spec requires user approval via /spec workflow |
| 8-10 (Two test streams, red-green, pytest) | Definition of Done requires both acceptance and unit/integration tests |
| 11 (Type hints, docstrings, PEP 8) | Implementation constraint: enforced by ruff + pyright |
| 12 (Simple functions over classes) | File structure uses module-level functions (policy.py, role.py, runner.py) |
| 13-14 (Error handling, no shortcuts) | Error flows specified: DB down, timeout, parse failure, missing files, auth failure |
| 15-16 (Modular, standardized patterns) | Same container pattern, same network isolation, same notification flow as auditor |
| 17 (Env vars for secrets) | All credentials use `${VAR_NAME}` syntax in policy files, resolved from host env at runtime. Plaintext secrets in config files are explicitly rejected. Credentials injected as container env vars, never baked into images |
| 18 (Validate at boundaries) | Policy/role files validated before container launch. DB containers verified running |
| 19 (No injection) | Connection details via standard env vars. Tool scoping prevents exfiltration |
| 20-21 (Docker + Dokploy, unique hostnames) | Agent runs as Docker container on temp network. Hostname resolution uses dokploy-network aliases |
| 22 (PROJECT.md) | Included above |
| 23-26 (Definition of Done) | Acceptance tests, deployment (CLI installable), monitoring (Discord), logging (structlog) |
| 27-30 (Simplicity) | No plugin system, no composable roles, no provider pattern. YAML files + CLI. Explicit non-behaviors prevent scope creep |
