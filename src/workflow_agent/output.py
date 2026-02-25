"""Output archival and notification routing.

Archives agent output to ~/agent-output/<service>/<role>_<timestamp>/
and routes notifications through workflow-notify.
"""

from __future__ import annotations

import json
import os
import shutil
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


def archive_dir(service: str, role_name: str) -> str:
    """Build the archive directory path for agent output."""
    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d_%H%M%S")
    return os.path.join(OUTPUT_BASE, service, f"{role_name}_{date_str}")


def archive_output(
    output_dir: str,
    service: str,
    role_name: str,
    report: dict[str, Any],
) -> str:
    """Copy agent output to the archive directory.

    Returns the archive directory path.
    """
    dest = archive_dir(service, role_name)
    os.makedirs(dest, exist_ok=True)

    # Write report.json
    with open(os.path.join(dest, "report.json"), "w") as f:
        json.dump(report, f, indent=2)

    # Copy report.md if it exists in the output dir
    report_md = os.path.join(output_dir, "report.md")
    if os.path.exists(report_md):
        shutil.copy2(report_md, os.path.join(dest, "report.md"))

    # Copy any other files from the output dir
    for entry in os.listdir(output_dir):
        src = os.path.join(output_dir, entry)
        dst = os.path.join(dest, entry)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    log.info("output.archived", path=dest, service=service, role=role_name)
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
            )
        elif overall == "complete":
            fanout(
                config=config,
                service=service,
                severity="success",
                message=f"Agent [{role_name}] completed: {report.get('summary', '')}",
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
