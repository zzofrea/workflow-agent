"""CLI entry point for workflow-agent.

Subcommands:
  run       -- Run an agent role against a service
  list      -- List available roles for a service
  validate  -- Validate policy + role files without launching
"""

from __future__ import annotations

import argparse
import sys

import structlog

from workflow_agent.config import AGENTS_DIR
from workflow_agent.output import archive_output, route_notifications
from workflow_agent.policy import resolve_policy_for_service, validate_password
from workflow_agent.role import resolve_role_for_service
from workflow_agent.runner import (
    check_container_running,
    resolve_container_names,
    run_agent,
)

log = structlog.get_logger("workflow_agent.cli")


def cmd_run(args: argparse.Namespace) -> None:
    """Run an agent role against a target service."""
    service = args.target
    role_name = args.role

    # Load and validate role + policy
    role = resolve_role_for_service(service, role_name)
    policy = resolve_policy_for_service(service, role.policy)

    # Apply CLI overrides
    model = args.model or role.model
    timeout = args.timeout or role.timeout
    max_turns = args.max_turns or role.max_turns
    notify = role.notify and not args.no_notify

    # Load spec if referenced
    spec_content = ""
    if role.spec:
        spec_path = AGENTS_DIR / service / "specs" / role.spec
        if spec_path.exists():
            spec_content = spec_path.read_text()
        else:
            log.warning("cli.spec_not_found", path=str(spec_path))

    # Build role config dict for container
    role_config = {
        "name": role.name,
        "system_prompt": role.system_prompt,
        "output_format": role.output_format,
        "model": model,
        "max_turns": max_turns,
        "runtime": role.runtime,
    }

    log.info(
        "cli.run_starting",
        service=service,
        role=role_name,
        policy=role.policy,
        model=model,
        timeout=timeout,
    )

    report = run_agent(
        role_name=role_name,
        policy=policy,
        service=service,
        role_config=role_config,
        spec_content=spec_content,
        timeout_override=timeout,
        model=model,
        max_turns=max_turns,
    )

    # Archive output
    archive_output(".", service, role_name, report)

    # Notify
    if notify:
        route_notifications(report, service, role_name)

    overall = report.get("overall", "error")
    print(f"\nAgent complete: {overall}")
    if overall in ("fail", "error"):
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    """List available roles for a service."""
    service = args.target
    roles_dir = AGENTS_DIR / service / "roles"

    if not roles_dir.exists():
        print(f"No roles directory found for service: {service}")
        sys.exit(1)

    yaml_files = sorted(roles_dir.glob("*.yaml"))
    if not yaml_files:
        print(f"No roles found for service: {service}")
        return

    print(f"Available roles for {service}:")
    for f in yaml_files:
        print(f"  {f.stem}")


def cmd_validate(args: argparse.Namespace) -> None:
    """Validate policy + role files and check database containers."""
    service = args.target
    role_name = args.role

    errors: list[str] = []

    # Load role
    try:
        role = resolve_role_for_service(service, role_name)
        print(f"Role: {role.name} (runtime={role.runtime}, model={role.model})")
    except (FileNotFoundError, ValueError) as e:
        print(f"FAIL: Role validation error: {e}")
        sys.exit(1)

    # Load policy
    try:
        policy = resolve_policy_for_service(service, role.policy)
        print(f"Policy: {policy.name} ({len(policy.databases)} database(s))")
        print(f"Tools: {', '.join(policy.tools)}")
    except (FileNotFoundError, ValueError) as e:
        print(f"FAIL: Policy validation error: {e}")
        sys.exit(1)

    # Validate credentials
    for db in policy.databases:
        try:
            validate_password(db.password)
            print(f"Database [{db.name}]: {db.hostname}:{db.port}/{db.database} -- credentials OK")
        except ValueError as e:
            errors.append(f"Database [{db.name}]: credential error: {e}")

    # Check spec exists if referenced
    if role.spec:
        spec_path = AGENTS_DIR / service / "specs" / role.spec
        if spec_path.exists():
            print(f"Spec: {role.spec} -- found")
        else:
            errors.append(f"Spec file not found: {spec_path}")

    # Check database containers
    hostnames = {db.hostname for db in policy.databases}
    if hostnames:
        host_to_container = resolve_container_names(hostnames)
        for db in policy.databases:
            ctr_key = db.hostname
            container_name = host_to_container.get(ctr_key) or ctr_key
            if check_container_running(container_name):
                print(f"Container [{db.hostname}]: {container_name} -- running")
            else:
                errors.append(f"Container [{db.hostname}]: {container_name} -- NOT running")

    if errors:
        print(f"\nValidation FAILED ({len(errors)} error(s)):")
        for err in errors:
            print(f"  {err}")
        sys.exit(1)
    else:
        print("\nValidation PASSED")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Templated agent sandbox framework",
        prog="workflow-agent",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run subcommand
    run_parser = sub.add_parser("run", help="Run an agent role against a service")
    run_parser.add_argument("role", help="Role name (e.g., auditor, analyst)")
    run_parser.add_argument("--target", required=True, help="Service name (e.g., bid-scraper)")
    run_parser.add_argument("--model", default=None, help="Override Claude model")
    run_parser.add_argument("--timeout", type=int, default=None, help="Override timeout (seconds)")
    run_parser.add_argument("--max-turns", type=int, default=None, help="Override max turns")
    run_parser.add_argument("--no-notify", action="store_true", help="Skip notifications")

    # list subcommand
    list_parser = sub.add_parser("list", help="List available roles for a service")
    list_parser.add_argument("--target", required=True, help="Service name")

    # validate subcommand
    validate_parser = sub.add_parser("validate", help="Validate role + policy files")
    validate_parser.add_argument("--target", required=True, help="Service name")
    validate_parser.add_argument("--role", required=True, help="Role name to validate")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "validate":
        cmd_validate(args)


if __name__ == "__main__":
    main()
