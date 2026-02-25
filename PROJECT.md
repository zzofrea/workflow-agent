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
