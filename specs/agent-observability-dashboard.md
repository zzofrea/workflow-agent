# Agent Observability Dashboard — Specification

## System Overview

A metrics and log pipeline that gives visual observability into workflow-agent
runs across all services. After each agent run, structured metrics (pass/fail,
duration, scenario counts) are pushed to Prometheus via Pushgateway, and run logs
are shipped to Loki via Promtail. A Grafana dashboard presents service status,
run history, and duration trends. This builds on the existing monitoring stack
(Prometheus + Grafana) with minimal new infrastructure.

**Who it serves:** Zack (sole operator), viewing agent run health in Grafana.

**Why it exists:** With multiple agents running on schedules across bid-scraper,
ETL, and future services, there is no visual way to see run history, duration
trends, or pass/fail patterns over time. Discord notifications provide
point-in-time alerts but no historical view.

## Behavioral Contract

### Metrics Emission

- When an agent run completes successfully, the system pushes run metrics
  (result, duration, scenario pass/fail counts) to Pushgateway within the same
  process, before returning control to the caller.
- When the report's `overall` field is `pass` or `complete`, the normalized
  result metric is `1`. When `fail`, `error`, or `incomplete`, the normalized
  result metric is `0`.
- When Pushgateway is unreachable or the push fails, the orchestrator logs a
  warning and continues. The agent run result is not affected.
- When multiple services/roles push metrics, each is distinguished by `service`
  and `role` labels.

### Log Shipping

- When an agent run archives output to `~/agent-output/<service>/<role>_<timestamp>/`,
  Promtail detects the new log files and ships them to Loki with labels parsed
  from the directory path (`service`, `role`, `run_id`).
- When a user queries logs in Grafana, they can filter by service, role, or
  run ID to see the full output of any specific run.

### Prometheus Scraping

- When Prometheus scrapes Pushgateway, it collects the latest agent run metrics
  per service/role combination.
- When Pushgateway has no data (fresh restart, no runs yet), Prometheus scrapes
  return empty and no dashboard panels error.

### Grafana Dashboard

- When a user opens the Agent Observability dashboard, they see:
  - A status panel showing the last run result for each service/role (pass/fail).
  - A timeline panel showing all runs over the selected time range with
    pass/fail color coding.
  - A time-series panel showing run duration trends per service/role.
  - A bar gauge showing scenario pass/fail counts per service/role.
- When no agent runs have occurred in the selected time range, the dashboard
  shows empty panels with no errors.
- When a user clicks a run in the timeline, they can drill down to the Loki
  logs for that run.

### Alerting

- When `agent_run_result == 0` for any service/role, Grafana fires an alert.
  The alert routes to the existing Discord notification channel (contact point).

## Explicit Non-Behaviors

- The system must not replace or modify existing Discord notifications via
  workflow-notify, because those serve a different audience (immediate operator
  alerting vs. historical trend analysis).
- The system must not capture or store Claude conversation logs, token counts,
  or LLM-level traces, because that is a separate scope requiring changes to
  the agent container entrypoint.
- The system must not trigger any remediation or re-run actions based on
  metrics, because observability is read-only.
- The system must not introduce a new UI beyond Grafana, because the goal is
  to extend the existing stack.
- The system must not modify workflow-agent's code, dependencies, or report
  format, because the framework should remain infrastructure-agnostic. Metrics
  emission belongs in the orchestration layer (workflow-platform).
- The system must not add abstractions or helper libraries for the metrics push
  — a single module in `workflow_platform/metrics.py` is sufficient for this
  volume (constitution rule 28).
- The system must not backfill historical agent runs into the metrics store,
  because Prometheus is designed for live time-series data, not batch imports.

## Integration Boundaries

### Prometheus Pushgateway (new)

- **Data out:** HTTP POST to `monitoring-pushgateway:9091` using
  `prometheus_client` Python library's `push_to_gateway()`. Labels: `service`,
  `role`.
- **Metrics pushed:** `agent_run_result` (gauge, 1=pass 0=fail),
  `agent_run_duration_seconds` (gauge), `agent_run_scenarios_pass` (gauge),
  `agent_run_scenarios_fail` (gauge).
- **Failure behavior:** If Pushgateway is unreachable, `push_to_gateway()` raises
  an exception. The calling code logs a warning and continues — metrics are
  best-effort, not a gate on agent run success.
- **Security:** Internal Docker network only. No secrets required. No external
  exposure.

### Prometheus (existing, modified config)

- **Data in:** Scrapes Pushgateway at `monitoring-pushgateway:9091` on the
  existing 30s interval.
- **Config change:** Add a `pushgateway` scrape job to
  `/home/docker/monitoring-config/prometheus.yml` with `honor_labels: true` to
  preserve the job/service/role labels from the push.
- **Failure behavior:** If Pushgateway is down at scrape time, Prometheus logs
  a scrape error. No data loss for previously scraped metrics.

### Grafana Loki (new)

- **Data in:** Receives log streams from Promtail over HTTP POST to
  `monitoring-loki:3100/loki/api/v1/push`.
- **Storage:** Local filesystem in single-binary mode. Retention: 15 days
  (matches Prometheus).
- **Failure behavior:** If Loki is down, Promtail buffers and retries. No log
  data lost unless Promtail's buffer fills (unlikely at this volume).
- **Security:** Internal Docker network only. No external exposure.

### Promtail (new)

- **Data in:** Tails files matching `/agent-output/**/*.log` and
  `/agent-output/**/report.json` (host path `/home/docker/agent-output/`
  mounted read-only into the container).
- **Label extraction:** Parses directory path via pipeline stages to extract
  `service` and `role` labels, and the full `<role>_<timestamp>` directory name
  as `run_id`.
- **Failure behavior:** If Loki is unreachable, Promtail buffers locally and
  retries with backoff.
- **Security:** Read-only mount. No write access to agent output.

### Grafana (existing, modified config)

- **Config change:** Add Loki as a second provisioned datasource in
  `/home/docker/monitoring-config/datasource.yml`.
- **Dashboard:** Provisioned via JSON file in `/home/docker/monitoring-config/`
  mounted into Grafana's provisioning directory.
- **Alert contact point:** Use the existing brain-dump Discord webhook for
  alert routing via `DISCORD_WEBHOOK_URL` environment variable.
- **Security:** Existing Traefik + Cloudflare tunnel auth. No new exposure.

### workflow-platform orchestrate.py (existing, modified)

- **Change:** Add a `push_metrics()` function in a new
  `workflow_platform/metrics.py` module. Call it from `cmd_build()` and
  `cmd_monitor()` after `_run_workflow_agent()` returns the report.
- **Dependency:** Add `prometheus_client` to workflow-platform's
  `pyproject.toml` dependencies.
- **Pattern:** Follows the existing try/except pattern used by
  `_send_deploy_notification()` — log warning and continue on failure.
- **Failure behavior:** Metrics push failure logs a warning but does not fail
  the agent run. Observability is best-effort at the orchestrator layer.
- **Note:** Direct `workflow-agent run` calls (not via orchestrator) will not
  push metrics. This is acceptable — scheduled/production runs always go
  through the orchestrator.

## Behavioral Scenarios

### Happy Path

#### Scenario 1: Auditor run metrics appear in Grafana

```
GIVEN an auditor agent is configured for the bid-scraper service
  AND Pushgateway and Prometheus are running
WHEN the agent completes a run with 7 scenarios passing and 2 failing
THEN the Grafana dashboard shows bid-scraper/auditor last result as "fail"
  AND the duration trend shows a new data point
  AND the scenario panel shows 7 pass, 2 fail
```

#### Scenario 2: Successful run normalizes to result 1

```
GIVEN an analyst agent is configured for the defendershield-etl service
WHEN the agent completes with overall status "complete"
THEN the Grafana dashboard shows defendershield-etl/analyst last result as "pass"
  AND the normalized result metric is 1
```

#### Scenario 3: Run logs searchable in Grafana

```
GIVEN an auditor agent completed a run for bid-scraper 10 minutes ago
  AND the run produced exec_output.log and report.json
WHEN a user opens Grafana and queries Loki for service=bid-scraper
THEN the log panel shows entries from that run
  AND the entries are labeled with the correct service, role, and run ID
```

### Error Scenarios

#### Scenario 4: Pushgateway down does not fail the run

```
GIVEN Pushgateway is not running
WHEN an agent run completes and the orchestrator attempts to push metrics
THEN the orchestrator logs a warning about the metrics push failure
  AND the agent run result is unaffected (pass/fail based on agent output only)
  AND Discord notifications still fire normally
```

#### Scenario 5: Agent run produces error report

```
GIVEN an agent run times out or fails to produce a report
WHEN the error report has overall status "error"
THEN the normalized result metric is 0
  AND the Grafana alert fires
  AND the alert notification reaches Discord
```

### Edge Cases

#### Scenario 6: First run after fresh deployment

```
GIVEN the observability stack was just deployed
  AND no agent runs have occurred yet
WHEN a user opens the Grafana dashboard
THEN all panels render without errors
  AND status panels show "No data" rather than error states
```

#### Scenario 7: Multiple roles for the same service

```
GIVEN bid-scraper has both an "auditor" role and a future "analyst" role
WHEN both roles complete runs within the same hour
THEN the dashboard shows separate entries for bid-scraper/auditor and
     bid-scraper/analyst
  AND metrics do not collide or overwrite each other
```

## Definition of Done

- [ ] Acceptance tests pass (scenarios 1-7 verified against live stack)
- [ ] Unit/integration tests pass for `push_metrics()` function
- [ ] Pushgateway, Loki, and Promtail deployed as Docker containers on
      dokploy-network with unique hostnames
- [ ] Pushgateway added to Prometheus scrape config with `honor_labels: true`
- [ ] Loki added as provisioned Grafana datasource
- [ ] Grafana dashboard provisioned with: status panel, timeline, duration
      trend, scenario bar gauge, and Loki log panel
- [ ] Grafana alert rule fires on `agent_run_result == 0` and routes to Discord
- [ ] Logging in `push_metrics()` sufficient for debugging push failures
- [ ] At least one real agent run produces visible metrics and logs in Grafana

## PROJECT.md

```markdown
# Agent Observability Dashboard

## Objective

Add visual observability for workflow-agent runs across all services using the
existing Grafana + Prometheus stack, extended with Pushgateway (metrics) and
Loki + Promtail (logs).

## Acceptance Criteria

- After any agent run, metrics (result, duration, scenario counts) appear in
  Grafana within one scrape interval (30s).
- Run logs are searchable in Grafana by service, role, and run ID.
- Dashboard shows: last run status per service/role, run timeline, duration
  trends, and scenario pass/fail counts.
- Alert fires to Discord when any agent run fails.
- Pushgateway failure logs a warning but does not fail the agent run.

## Constraints

- Deploys on existing Beelink Ser3 Mini alongside 22 existing containers.
- Total new RAM budget: ~400 MB (Pushgateway ~50 MB, Loki ~200 MB,
  Promtail ~100 MB).
- All containers on dokploy-network with unique hostnames.
- No new UIs — Grafana only.
- No changes to report.json format, archive directory structure, or
  workflow-agent code.

## What "Done" Means

- All 7 behavioral scenarios verified against live stack.
- Unit tests pass for push_metrics() including Pushgateway-down failure case.
- Dashboard provisioned and accessible at grafana.rustydata.tech.
- At least one real agent run visible end-to-end (metrics + logs).

## Out of Scope

- Claude conversation log capture (LLM-level tracing).
- Automated remediation or re-runs based on metrics.
- Replacing workflow-notify Discord notifications.
- Healthchecks dead-man's switch (future enhancement).
- Historical backfill of past agent runs.
```

## Resolved Ambiguities

1. **Deployment target:** Pushgateway, Loki, and Promtail are added to the
   existing monitoring compose (`fELaVEogZ7GW2wurD_Jx5`). Same stack, no
   compose sprawl.
2. **Promtail host volume mount:** `/home/docker/agent-output/` mounted
   read-only into the Promtail container. Confirmed acceptable.
3. **Grafana alert contact point:** Use the existing `brain-dump` Discord
   channel/webhook. No new contact point infrastructure needed.
4. **`prometheus_client` dependency:** Added to workflow-platform's
   pyproject.toml. No changes to workflow-agent.
5. **Loki retention:** `retention_period: 360h` (15 days) to match Prometheus.
6. **Dashboard provisioning path:**
   `/home/docker/monitoring-config/dashboard-provisioner.yml` for the
   provisioning config and
   `/home/docker/monitoring-config/dashboards/agent-observability.json` for
   the dashboard JSON.

## Implementation Constraints

- **Language:** Python (workflow-platform is Python).
- **Metrics library:** `prometheus_client` (standard Prometheus Python client).
- **Container images:** Official images — `prom/pushgateway:latest`,
  `grafana/loki:latest` (single-binary mode), `grafana/promtail:latest`.
- **Network:** All new containers join `dokploy-network` with unique hostnames:
  `monitoring-pushgateway`, `monitoring-loki`, `monitoring-promtail`.
- **Config files:** All config in `/home/docker/monitoring-config/`, mounted
  into containers via the existing monitoring compose volume pattern.
- **Linting:** `ruff check --fix .`, `ruff format .`, `pyright .` after code
  changes.
- **Test runner:** pytest.

## Constitution Compliance

| Rule | How Satisfied |
|------|---------------|
| 1-2. Specs before code, Given/When/Then | This document. 7 behavioral scenarios in Given/When/Then format. |
| 3. Specs describe WHAT not HOW | Scenarios describe observable outcomes (metrics in Grafana, alerts firing), not implementation internals. |
| 6. Spec current feature only | Scoped to metrics + logs. Conversation tracing and Healthchecks explicitly out of scope. |
| 7. Human approval before implementation | This spec requires approval before proceeding. |
| 8-9. Two test streams, tests first | Definition of Done requires both acceptance tests (scenarios) and unit/integration tests for push_metrics(). |
| 10. pytest | pytest for unit/integration tests. |
| 11. Type hints, docstrings, PEP 8 | Implementation constraint inherited from workflow-agent codebase conventions. |
| 12. Prefer functions over classes | push_metrics() is a standalone function in metrics.py, not a class. |
| 13. Never skip error handling | Pushgateway failure caught, logged as warning, does not crash orchestrator. |
| 14. No quick-and-dirty | Metrics are best-effort at orchestrator layer; agent runs are never gated on infra availability. |
| 15-16. Modular, standardized | Extends existing monitoring stack patterns (same config dir, same compose, same network). |
| 17. Env vars for secrets | No new secrets needed. Grafana admin password already in env vars. All traffic is internal Docker network. |
| 18. Validate at boundaries | push_metrics() reads report dict fields with safe .get() defaults. |
| 19. No OWASP vulnerabilities | No user-facing input. All internal Docker network traffic. |
| 20. Docker containers via Dokploy | All new containers in Dokploy-managed compose. |
| 21. Unique hostnames | monitoring-pushgateway, monitoring-loki, monitoring-promtail. |
| 22. PROJECT.md | Included above. |
| 23-26. Definition of Done | Acceptance tests, deployment, monitoring (the system IS monitoring), logging. |
| 27-28. No unrequested features | No conversation tracing, no remediation, no Healthchecks, no backfill. |
| 29. No premature abstractions | Single metrics.py module with push_metrics(). No metrics framework or helper library. |
| 30. No backwards-compat hacks | No existing metrics code to maintain compatibility with. |
