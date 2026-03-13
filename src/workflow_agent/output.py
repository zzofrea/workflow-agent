"""Output archival and notification routing.

Archives agent output to ~/agent-output/<service>/<role>_<timestamp>/
and routes notifications through workflow-notify.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import structlog

try:
    from workflow_notify import NotifyConfig, fanout
except ImportError:
    NotifyConfig = None  # type: ignore[assignment,misc]
    fanout = None  # type: ignore[assignment]

from workflow_agent.config import OUTPUT_BASE

log = structlog.get_logger("workflow_agent.output")


def archive_dir(service: str, role_name: str, run_id: str | None = None) -> str:
    """Build the archive directory path for agent output.

    Format: ``{role}_{timestamp}`` or ``{role}_{timestamp}_{run_id}`` when
    a run_id is provided.  The run_id suffix enables deterministic lookup
    by the orchestrator.
    """
    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d_%H%M%S")
    suffix = f"_{run_id}" if run_id else ""
    return os.path.join(OUTPUT_BASE, service, f"{role_name}_{date_str}{suffix}")


def archive_output(
    service: str,
    role_name: str,
    report: dict[str, Any],
    run_id: str | None = None,
) -> str:
    """Archive the agent report to ~/agent-output/<service>/<role>_<timestamp>[_<run_id>]/.

    Only writes report.json. Returns the archive directory path.
    """
    dest = archive_dir(service, role_name, run_id)
    os.makedirs(dest, exist_ok=True)

    with open(os.path.join(dest, "report.json"), "w") as f:
        json.dump(report, f, indent=2)

    log.info("output.archived", path=dest, service=service, role=role_name, run_id=run_id)
    print(f"Report archived to {dest}/")
    return dest


def route_notifications(
    report: dict[str, Any],
    service: str,
    role_name: str,
) -> None:
    """Send notifications based on agent output."""
    if NotifyConfig is None or fanout is None:
        log.warning("output.notify_unavailable", reason="workflow-notify not installed")
        return

    try:
        config = NotifyConfig()
        overall = report.get("overall", "error")

        if overall == "pass":
            fanout(
                config=config,
                service=service,
                severity="success",
                message=f"Agent [{role_name}] PASSED: {report.get('summary', '')}",
                channel="agent_logs",
            )
        elif overall == "complete":
            fanout(
                config=config,
                service=service,
                severity="success",
                message=f"Agent [{role_name}] completed: {report.get('summary', '')}",
                channel="agent_logs",
            )
        elif overall == "fail":
            failures = [s for s in report.get("scenarios", []) if s.get("status") == "fail"]
            failure_details = "; ".join(
                f"Scenario {s.get('id', '?')}: {s.get('observation', 'N/A')}" for s in failures[:3]
            )
            severity = _is_critical(failures)
            fanout(
                config=config,
                service=service,
                severity=severity,
                message=f"Agent [{role_name}] FAILED ({len(failures)}): {failure_details}",
                observation=f"Agent failed: {report.get('summary', '')}",
                evidence=failure_details,
                suggested_action="Review agent report and investigate",
                channel="agent_logs",
            )
        elif overall in ("error", "incomplete"):
            fanout(
                config=config,
                service=service,
                severity="warning",
                message=f"Agent [{role_name}] {overall.upper()}: {report.get('summary', '')}",
                observation=f"Agent {overall}: {report.get('summary', '')}",
                evidence=report.get("incomplete_reason", "See report for details"),
                suggested_action="Check agent logs and re-run",
                channel="agent_logs",
            )
    except Exception:
        log.exception("output.notify_failed", service=service, role=role_name)


def _is_critical(failures: list[dict[str, Any]]) -> str:
    """Classify notification severity based on failure types.

    Service-down or zero-data failures are critical; others are warnings.
    """
    for f in failures:
        obs = (f.get("observation", "") + f.get("evidence", "")).lower()
        if any(
            kw in obs
            for kw in [
                "unreachable",
                "connection refused",
                "no records",
                "service down",
                "0 rows",
            ]
        ):
            return "critical"
    return "warning"
