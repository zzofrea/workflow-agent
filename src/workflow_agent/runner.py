"""Container lifecycle: network setup, container launch, cleanup.

Generalizes the auditor's run_audit() for multi-DB, policy-driven config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from typing import Any

import structlog

from workflow_agent.config import (
    AGENT_IMAGE,
    CLAUDE_AUTH_DIR,
    CLAUDE_AUTH_JSON,
    CONTAINER_NAME_PREFIX,
    DEFAULT_TIMEOUT,
)
from workflow_agent.policy import Policy, validate_env_value, validate_password

log = structlog.get_logger("workflow_agent.runner")


def resolve_container_names(hostnames: set[str]) -> dict[str, str]:
    """Resolve Docker DNS hostnames to actual container names.

    On dokploy-network, containers are addressable by hostname or alias
    but docker network connect requires the real container name.

    Returns a dict mapping hostname -> container name.
    """
    if not hostnames:
        return {}

    mapping: dict[str, str] = {}

    result = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "dokploy-network",
            "--format",
            "{{range .Containers}}{{.Name}} {{end}}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )

    if result.returncode != 0:
        log.warning("runner.network_inspect_failed", stderr=result.stderr[:500])
        return {h: h for h in hostnames}

    containers = result.stdout.strip().split()
    for container in containers:
        if not container:
            continue

        hostname_result = subprocess.run(
            ["docker", "inspect", container, "--format", "{{.Config.Hostname}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if hostname_result.returncode == 0:
            ctr_hostname = hostname_result.stdout.strip()
            if ctr_hostname in hostnames:
                mapping[ctr_hostname] = container

        alias_result = subprocess.run(
            [
                "docker",
                "inspect",
                container,
                "--format",
                '{{index .NetworkSettings.Networks "dokploy-network" "Aliases"}}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if alias_result.returncode == 0:
            aliases_str = alias_result.stdout.strip().strip("[]")
            for alias in aliases_str.split():
                if alias in hostnames:
                    mapping[alias] = container

    for h in hostnames:
        if h not in mapping:
            mapping[h] = h

    return mapping


def check_container_running(name: str) -> bool:
    """Check if a Docker container is running."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def image_exists_locally(image: str = AGENT_IMAGE) -> bool:
    """Check if the agent image is available locally."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def pull_image(image: str = AGENT_IMAGE) -> bool:
    """Pull the agent image from registry."""
    result = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        log.error("runner.pull_failed", image=image, stderr=result.stderr[:2000])
        return False
    log.info("runner.image_pulled", image=image)
    return True


def build_env_vars(policy: Policy, role_name: str, service: str) -> list[tuple[str, str]]:
    """Build environment variable pairs for the agent container.

    Resolves ${VAR_NAME} passwords and namespaces env vars per database
    using the env_prefix from each database config.
    """
    env_vars: list[tuple[str, str]] = []

    for db in policy.databases:
        prefix = db.env_prefix
        resolved_password = validate_password(db.password)

        env_vars.append((f"{prefix}HOST", db.hostname))
        env_vars.append((f"{prefix}PORT", str(db.port)))
        env_vars.append((f"{prefix}USER", db.user))
        env_vars.append((f"{prefix}DATABASE", db.database))

        if resolved_password:
            env_vars.append((f"{prefix}PASSWORD", resolved_password))

    env_vars.append(("AGENT_SERVICE", service))
    env_vars.append(("AGENT_ROLE", role_name))

    # Custom environment variables from policy
    builtin_keys = {k for k, _ in env_vars}
    builtin_keys.update({"AGENT_MODEL", "AGENT_MAX_TURNS", "AGENT_TOOLS", "HOME"})

    for key, value in policy.environment.items():
        if key in builtin_keys:
            log.warning("runner.env_builtin_override_blocked", key=key)
            continue
        resolved = validate_env_value(key, value)
        env_vars.append((key, resolved))

    return env_vars


def build_docker_cmd(
    input_dir: str,
    output_dir: str,
    *,
    service: str,
    role_name: str,
    policy: Policy,
    network: str,
    model: str = "sonnet",
    max_turns: int = 50,
    image: str = AGENT_IMAGE,
) -> list[str]:
    """Construct the docker run command for the agent container."""
    container_name = f"{CONTAINER_NAME_PREFIX}-{service}-{role_name}"

    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        network,
        "--cap-drop",
        "ALL",
        # Mount Claude auth to staging dir (read-only)
        "-v",
        f"{CLAUDE_AUTH_JSON}:/agent/auth/.claude.json:ro",
        "-v",
        f"{CLAUDE_AUTH_DIR}:/agent/auth/.claude:ro",
        # Mount input (read-only)
        "-v",
        f"{input_dir}:/agent/input:ro",
        # Mount output (read-write)
        "-v",
        f"{output_dir}:/agent/output:rw",
    ]

    # Add env vars from policy
    env_pairs = build_env_vars(policy, role_name, service)
    for key, value in env_pairs:
        cmd.extend(["-e", f"{key}={value}"])

    # Agent config env vars
    cmd.extend(["-e", f"AGENT_MODEL={model}"])
    cmd.extend(["-e", f"AGENT_MAX_TURNS={max_turns}"])

    # Tools from policy
    if policy.tools:
        tools_str = ",".join(policy.tools)
        cmd.extend(["-e", f"AGENT_TOOLS={tools_str}"])

    cmd.extend(["-e", "HOME=/home/node"])
    cmd.append(image)

    return cmd


def run_agent(
    role_name: str,
    policy: Policy,
    service: str,
    *,
    role_config: dict[str, Any],
    spec_content: str = "",
    timeout_override: int | None = None,
    model: str = "sonnet",
    max_turns: int = 50,
    image: str = AGENT_IMAGE,
) -> dict[str, Any]:
    """Run a sandboxed agent: temp network, multi-DB connect, launch, cleanup.

    Returns the agent's output report dict.
    """
    total_timeout = timeout_override or DEFAULT_TIMEOUT

    # Ensure image is available
    if not image_exists_locally(image):
        log.info("runner.image_missing_locally", image=image)
        if not pull_image(image):
            return _error_report(service, role_name, f"Could not pull image: {image}")

    # Collect all hostnames and resolve to container names
    hostnames = {db.hostname for db in policy.databases}
    if not hostnames:
        log.warning("runner.no_databases", service=service, role=role_name)

    host_to_container = resolve_container_names(hostnames) if hostnames else {}

    # Check all DB containers are running
    for db in policy.databases:
        ctr_key = db.hostname
        container_name = host_to_container.get(ctr_key) or ctr_key
        if not check_container_running(container_name):
            return _error_report(
                service,
                role_name,
                f"Database container '{container_name}' ({db.hostname}) is not running",
            )

    # Create temp network
    net_name = f"agent-{service}-{uuid.uuid4().hex[:12]}"
    agent_container = f"{CONTAINER_NAME_PREFIX}-{service}-{role_name}"
    connected_containers: list[str] = []

    try:
        subprocess.run(
            ["docker", "network", "create", net_name],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        log.info("runner.network_created", network=net_name)

        # Connect each DB container to temp network with hostname alias
        for db in policy.databases:
            ctr_key = db.hostname
            container_name = host_to_container.get(ctr_key) or ctr_key
            subprocess.run(
                [
                    "docker",
                    "network",
                    "connect",
                    "--alias",
                    db.hostname,
                    net_name,
                    container_name,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            connected_containers.append(container_name)
            log.info(
                "runner.db_connected",
                network=net_name,
                container=container_name,
                alias=db.hostname,
            )

        # Create temp directories for input/output
        with (
            tempfile.TemporaryDirectory(prefix="agent-input-") as input_dir,
            tempfile.TemporaryDirectory(prefix="agent-output-") as output_dir,
        ):
            _prepare_input(input_dir, role_config, spec_content)

            cmd = build_docker_cmd(
                input_dir,
                output_dir,
                service=service,
                role_name=role_name,
                policy=policy,
                network=net_name,
                model=model,
                max_turns=max_turns,
                image=image,
            )

            log.info(
                "runner.container_starting",
                service=service,
                role=role_name,
                model=model,
                network=net_name,
            )
            print(f"Starting agent: service={service} role={role_name} model={model}")

            timed_out = False
            result = subprocess.CompletedProcess(args=[], returncode=-1, stdout="", stderr="")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=total_timeout,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                log.warning(
                    "runner.timeout",
                    service=service,
                    role=role_name,
                    timeout_seconds=total_timeout,
                )
                print(f"Agent timed out after {total_timeout}s -- killing container")
                subprocess.run(
                    ["docker", "kill", agent_container],
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    ["docker", "rm", "-f", agent_container],
                    capture_output=True,
                    timeout=30,
                )

            if not timed_out:
                if result.stdout:
                    print(result.stdout)
                if result.stderr:
                    print(result.stderr, file=sys.stderr)

            return _collect_output(output_dir, service, role_name, timed_out, total_timeout, result)

    except subprocess.CalledProcessError as exc:
        log.error("runner.network_setup_failed", network=net_name, error=str(exc))
        return _error_report(service, role_name, f"Network setup failed: {exc}")

    finally:
        # Disconnect all DB containers from temp network
        for ctr in connected_containers:
            subprocess.run(
                ["docker", "network", "disconnect", net_name, ctr],
                capture_output=True,
                text=True,
                timeout=30,
            )
        # Remove temp network
        subprocess.run(
            ["docker", "network", "rm", net_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        log.info("runner.network_cleaned", network=net_name)


def _prepare_input(input_dir: str, role_config: dict[str, Any], spec_content: str) -> None:
    """Write role config and spec to the container input directory."""
    with open(os.path.join(input_dir, "role.json"), "w") as f:
        json.dump(role_config, f, indent=2)

    if spec_content:
        with open(os.path.join(input_dir, "spec.md"), "w") as f:
            f.write(spec_content)


def _collect_output(
    output_dir: str,
    service: str,
    role_name: str,
    timed_out: bool,
    timeout_seconds: int,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Read and return the agent's output report."""
    report_json_path = os.path.join(output_dir, "report.json")

    if timed_out:
        return _error_report(service, role_name, f"Agent timed out after {timeout_seconds} seconds")

    if os.path.exists(report_json_path):
        with open(report_json_path) as f:
            report: dict[str, Any] = json.load(f)
        log.info("runner.report_collected", overall=report.get("overall"))
        return report

    raw_stdout = result.stdout[:5000]
    report = _error_report(service, role_name, "No report produced by agent container")
    report["raw_output"] = raw_stdout
    log.error("runner.no_report", stdout=raw_stdout[:500])
    return report


def _error_report(service: str, role_name: str, summary: str) -> dict[str, Any]:
    """Build a minimal error report."""
    return {
        "overall": "error",
        "service": service,
        "role": role_name,
        "summary": summary,
        "scenarios": [],
    }
