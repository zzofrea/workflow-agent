"""Role file parsing and validation.

A role defines what an agent does: system prompt, output format, model,
timeout, and references a policy for resource access.
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, field_validator

from workflow_agent.config import (
    AGENTS_DIR,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
)

log = structlog.get_logger("workflow_agent.role")


class Role(BaseModel):
    """Parsed role file."""

    name: str
    description: str = ""
    policy: str
    spec: str = ""
    runtime: str = "claude-cli"
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    timeout: int = DEFAULT_TIMEOUT
    notify: bool = True
    output_format: str = "json"
    system_prompt: str = ""

    @field_validator("runtime")
    @classmethod
    def check_runtime(cls, v: str) -> str:
        """Only claude-cli is supported in v1."""
        if v != "claude-cli":
            raise ValueError(f"Unsupported runtime: {v!r}. Only 'claude-cli' is supported in v1.")
        return v


def load_role(path: str | Path) -> Role:
    """Parse and validate a role YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Role file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Role file must be a YAML mapping: {path}")

    role = Role(**raw)
    log.info("role.loaded", name=role.name, policy=role.policy, runtime=role.runtime)
    return role


def resolve_role_for_service(service: str, role_name: str) -> Role:
    """Load a role by service and role name from the agents directory."""
    path = AGENTS_DIR / service / "roles" / f"{role_name}.yaml"
    return load_role(path)
