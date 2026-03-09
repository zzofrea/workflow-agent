#!/usr/bin/env python3
"""Agent container entrypoint -- runs inside the Docker container.

Reads role config and spec from /agent/input/, invokes Claude CLI with
constrained tools, parses the response into the expected output format.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

INPUT_DIR = "/agent/input"
OUTPUT_DIR = "/agent/output"
AUTH_STAGING_DIR = "/agent/auth"


def setup_claude_auth() -> None:
    """Copy Claude auth from read-only staging mount to writable home.

    Claude CLI needs to write to ~/.claude.json and ~/.claude/. We mount
    the host auth read-only, then copy so the CLI has writable copies.
    """
    home = Path.home()
    staging = Path(AUTH_STAGING_DIR)

    src_json = staging / ".claude.json"
    if src_json.exists():
        shutil.copy2(src_json, home / ".claude.json")
        print("Copied .claude.json to home", file=sys.stderr)

    src_dir = staging / ".claude"
    if src_dir.is_dir():
        dest_dir = home / ".claude"
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(
            src_dir,
            dest_dir,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )
        print("Copied .claude/ directory to home", file=sys.stderr)


def read_role_config() -> dict:
    """Read the role configuration from /agent/input/role.json."""
    path = os.path.join(INPUT_DIR, "role.json")
    if not os.path.exists(path):
        print(f"Warning: {path} not found", file=sys.stderr)
        return {}
    with open(path) as f:
        return json.load(f)


def read_spec() -> str:
    """Read the spec file from /agent/input/spec.md."""
    path = os.path.join(INPUT_DIR, "spec.md")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def build_prompt(spec: str) -> str:
    """Build the user prompt from spec content."""
    parts = []
    if spec:
        parts.append("## Specification\n")
        parts.append(spec)
        parts.append("\n\nFollow the specification above and produce your output.")
    else:
        parts.append("Execute your role as described in your system prompt.")
    return "\n".join(parts)


def run_claude_cli(
    prompt: str,
    system_prompt: str,
    tools: str,
    model: str = "sonnet",
    max_turns: int = 50,
    retries: int = 2,
    retry_delay: float = 10.0,
) -> tuple[str, float]:
    """Invoke Claude CLI and return (output_text, duration_seconds).

    Retries on transient failures (non-zero exit with empty stdout) up to
    ``retries`` times, waiting ``retry_delay`` seconds between attempts.
    """
    cmd = [
        "claude",
        "--print",
        "--model",
        model,
        "--output-format",
        "text",
        "--system-prompt",
        system_prompt,
        "--allowedTools",
        tools,
        "--no-session-persistence",
        "--dangerously-skip-permissions",
    ]

    if max_turns > 0:
        cmd.extend(["--max-turns", str(max_turns)])

    total_duration = 0.0
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1 + max(retries, 0)):
        start = time.monotonic()
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=600,
        )
        elapsed = time.monotonic() - start
        total_duration += elapsed

        # Success or non-empty output -- return immediately
        if result.returncode == 0 or result.stdout.strip():
            if result.returncode != 0:
                print(f"Claude CLI stderr: {result.stderr}", file=sys.stderr)
            return result.stdout, total_duration

        # Transient failure: non-zero exit with empty stdout
        if attempt < retries:
            print(
                f"Claude CLI returned empty output (exit {result.returncode}), "
                f"retrying in {retry_delay}s (attempt {attempt + 1}/{retries})...",
                file=sys.stderr,
            )
            time.sleep(retry_delay)
        else:
            print(
                f"Claude CLI failed after {1 + retries} attempts "
                f"(exit {result.returncode}): {result.stderr}",
                file=sys.stderr,
            )
            return result.stdout, total_duration

    # Unreachable: loop always executes at least once and returns above
    return "", total_duration


def parse_json_output(raw_output: str) -> dict | None:
    """Extract JSON from Claude's output.

    Handles direct JSON, markdown-fenced JSON, and brace extraction.
    """
    text = raw_output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for marker in ["```json", "```"]:
        if marker in text:
            start = text.index(marker) + len(marker)
            end = text.index("```", start)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    return None


def build_output(
    parsed: dict | None,
    raw_output: str,
    output_format: str,
    role_name: str,
    service: str,
    model: str,
    duration: float,
) -> dict:
    """Build the final output report based on the expected output format."""
    now = datetime.now(UTC).isoformat()

    if output_format == "json":
        return _build_json_report(parsed, raw_output, role_name, service, model, duration, now)
    elif output_format == "markdown":
        return _build_markdown_output(raw_output, role_name, service, model, duration, now)
    else:
        return _build_text_output(raw_output, role_name, service, model, duration, now)


def _build_json_report(
    parsed: dict | None,
    raw_output: str,
    role_name: str,
    service: str,
    model: str,
    duration: float,
    now: str,
) -> dict:
    """Build a JSON report (audit-style with scenarios)."""
    scenarios = []
    overall = "error"
    incomplete = False
    incomplete_reason = ""

    if not raw_output.strip():
        incomplete = True
        incomplete_reason = "Empty response from Claude CLI"
    elif parsed is None:
        incomplete = True
        incomplete_reason = "Could not parse structured report from output"

    if parsed and "scenarios" in parsed:
        scenarios = parsed["scenarios"]
        statuses = [s.get("status", "error") for s in scenarios]
        if all(s == "pass" for s in statuses):
            overall = "pass"
        elif any(s == "fail" for s in statuses):
            overall = "fail"
        else:
            overall = "partial"

    if incomplete:
        overall = "incomplete"

    return {
        "role": role_name,
        "service": service,
        "date": now,
        "model": model,
        "overall": overall,
        "duration_seconds": round(duration, 1),
        "scenarios_total": len(scenarios),
        "scenarios_pass": sum(1 for s in scenarios if s.get("status") == "pass"),
        "scenarios_fail": sum(1 for s in scenarios if s.get("status") == "fail"),
        "scenarios_error": sum(1 for s in scenarios if s.get("status") not in ("pass", "fail")),
        "incomplete": incomplete,
        "incomplete_reason": incomplete_reason,
        "scenarios": scenarios,
        "summary": parsed.get("summary", "") if parsed else "Failed to parse output",
        "raw_output": raw_output[:5000] if not parsed else "",
    }


def _build_markdown_output(
    raw_output: str,
    role_name: str,
    service: str,
    model: str,
    duration: float,
    now: str,
) -> dict:
    """Build output for markdown-format roles."""
    report: dict = {
        "role": role_name,
        "service": service,
        "date": now,
        "model": model,
        "overall": "complete" if raw_output.strip() else "incomplete",
        "duration_seconds": round(duration, 1),
        "content": raw_output,
        "summary": f"{role_name} report for {service}",
    }

    metrics_path = os.path.join(OUTPUT_DIR, "metrics.json")
    if os.path.exists(metrics_path):
        try:
            with open(metrics_path) as f:
                metrics = json.load(f)
            report["metrics"] = metrics
            if metrics.get("fact_check_passed") is False:
                report["overall"] = "error"
                report["summary"] = (
                    f"Fact-check failed: {len(metrics.get('discrepancies', []))} "
                    f"discrepancy(ies) found. Email not sent."
                )
        except (json.JSONDecodeError, OSError) as exc:
            report["overall"] = "error"
            report["summary"] = f"Failed to read metrics.json: {exc}"

    return report


def _build_text_output(
    raw_output: str,
    role_name: str,
    service: str,
    model: str,
    duration: float,
    now: str,
) -> dict:
    """Build output for text-format roles."""
    return {
        "role": role_name,
        "service": service,
        "date": now,
        "model": model,
        "overall": "complete" if raw_output.strip() else "incomplete",
        "duration_seconds": round(duration, 1),
        "content": raw_output,
        "summary": f"{role_name} output for {service}",
    }


def build_markdown_report(report: dict) -> str:
    """Render a JSON report as human-readable markdown."""
    lines = [
        "---",
        f"role: {report.get('role', 'unknown')}",
        f"service: {report.get('service', 'unknown')}",
        f"date: {report.get('date', '')}",
        f"model: {report.get('model', '')}",
        f"overall: {report.get('overall', '')}",
        f"duration_seconds: {report.get('duration_seconds', 0)}",
        "---",
        "",
        f"# Agent Report: {report.get('service', 'unknown')}",
        "",
        f"**Role:** {report.get('role', 'unknown')}  ",
        f"**Date:** {report.get('date', '')}  ",
        f"**Model:** {report.get('model', '')}  ",
        f"**Overall:** {report.get('overall', '')}  ",
        f"**Duration:** {report.get('duration_seconds', 0)}s  ",
        "",
    ]

    if report.get("incomplete"):
        lines.append(f"> **INCOMPLETE:** {report.get('incomplete_reason', '')}")
        lines.append("")

    if report.get("summary"):
        lines.append(f"**Summary:** {report['summary']}")
        lines.append("")

    # For JSON reports with scenarios
    if report.get("scenarios"):
        lines.append("## Scenarios")
        lines.append("")
        for s in report["scenarios"]:
            status_icon = {"pass": "[PASS]", "fail": "[FAIL]", "error": "[ERROR]"}.get(
                s.get("status", "error"), "[???]"
            )
            desc = s.get("description", "N/A")
            lines.append(f"### {status_icon} Scenario {s.get('id', '?')}: {desc}")
            lines.append("")
            lines.append(f"**Expected:** {s.get('expected', 'N/A')}")
            lines.append("")
            lines.append(f"**Observation:** {s.get('observation', 'N/A')}")
            lines.append("")
            if s.get("evidence"):
                lines.append("**Evidence:**")
                lines.append("```")
                lines.append(s["evidence"])
                lines.append("```")
                lines.append("")

    # For markdown/text reports with content
    if report.get("content"):
        lines.append("## Content")
        lines.append("")
        lines.append(report["content"])

    if report.get("raw_output"):
        lines.append("## Raw Output (parse failed)")
        lines.append("```")
        lines.append(report["raw_output"])
        lines.append("```")

    return "\n".join(lines)


def dispatch_runtime(runtime: str, config: dict, spec: str) -> None:
    """Dispatch to the appropriate agent runtime."""
    if runtime == "claude-cli":
        _run_claude_cli_runtime(config, spec)
    else:
        print(f"Error: Unsupported runtime '{runtime}'", file=sys.stderr)
        sys.exit(1)


def _run_claude_cli_runtime(config: dict, spec: str) -> None:
    """Run the claude-cli runtime: invoke Claude, parse output, write report."""
    system_prompt = config.get("system_prompt", "")
    output_format = config.get("output_format", "json")
    model = os.environ.get("AGENT_MODEL") or config.get("model") or "sonnet"
    max_turns_str = os.environ.get("AGENT_MAX_TURNS") or str(config.get("max_turns", 50))
    max_turns = int(max_turns_str)
    tools = os.environ.get("AGENT_TOOLS") or "Read"
    service = os.environ.get("AGENT_SERVICE") or "unknown"
    role_name = os.environ.get("AGENT_ROLE") or "unknown"

    prompt = build_prompt(spec)
    raw_output, duration = run_claude_cli(prompt, system_prompt, tools, model, max_turns)
    print(f"Agent completed in {duration:.1f}s")

    parsed = parse_json_output(raw_output) if output_format == "json" else None

    report = build_output(
        parsed=parsed,
        raw_output=raw_output,
        output_format=output_format,
        role_name=role_name,
        service=service,
        model=model,
        duration=duration,
    )

    md_report = build_markdown_report(report)

    with open(os.path.join(OUTPUT_DIR, "report.json"), "w") as f:
        json.dump(report, f, indent=2)

    with open(os.path.join(OUTPUT_DIR, "report.md"), "w") as f:
        f.write(md_report)

    print(f"Reports written to {OUTPUT_DIR}/")
    print(f"Overall: {report.get('overall', 'unknown')}")

    if report.get("overall") in ("fail", "error"):
        sys.exit(1)


def main() -> None:
    """Entrypoint: setup auth, load config, dispatch runtime."""
    print("Agent container starting...")
    setup_claude_auth()

    config = read_role_config()
    spec = read_spec()
    runtime = config.get("runtime", "claude-cli")

    print(f"Runtime: {runtime}, Service: {os.environ.get('AGENT_SERVICE', 'unknown')}")
    dispatch_runtime(runtime, config, spec)


if __name__ == "__main__":
    main()
