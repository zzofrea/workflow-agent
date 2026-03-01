"""Policy file parsing and validation.

A policy defines what resources an agent can access: database connections
and tool permissions. All credentials use ${VAR_NAME} syntax resolved
from host environment variables at runtime. The only accepted literal
password is "trust" (for trust authentication).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, field_validator

from workflow_agent.config import AGENTS_DIR

log = structlog.get_logger("workflow_agent.policy")

SECRET_KEY_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN|KEY)", re.IGNORECASE)


def resolve_env_var(value: str) -> str:
    """Resolve ${VAR_NAME} references from host environment.

    Returns the resolved value, or raises ValueError if the env var is not set.
    Literal strings (no ${...} syntax) are returned as-is.
    """
    match = re.fullmatch(r"\$\{(\w+)\}", value)
    if not match:
        return value
    var_name = match.group(1)
    resolved = os.environ.get(var_name)
    if resolved is None:
        raise ValueError(f"Environment variable '{var_name}' is not set")
    return resolved


def validate_password(value: str) -> str:
    """Validate a password field from a policy file.

    Accepted values:
    - "${VAR_NAME}" -- resolved from host environment at runtime
    - "trust" -- trust authentication (no password)

    Any other literal string is rejected as a plaintext secret.
    """
    if value == "trust":
        return ""
    if re.fullmatch(r"\$\{(\w+)\}", value):
        return resolve_env_var(value)
    raise ValueError(
        f"Plaintext password rejected. Use '${{VAR_NAME}}' syntax or 'trust'. Got: {value!r}"
    )


def validate_env_value(key: str, value: str) -> str:
    """Validate and resolve an environment variable from a policy's environment block.

    If the key matches SECRET_KEY_PATTERN (PASSWORD, SECRET, TOKEN, KEY),
    the value must use ${VAR_NAME} syntax -- plaintext is rejected.
    All ${VAR_NAME} values are resolved from the host environment.
    """
    is_env_ref = bool(re.fullmatch(r"\$\{(\w+)\}", value))

    if SECRET_KEY_PATTERN.search(key) and not is_env_ref:
        raise ValueError(
            f"Sensitive key '{key}' must use '${{VAR_NAME}}' syntax, got plaintext: {value!r}"
        )

    return resolve_env_var(value)


class DatabaseConfig(BaseModel):
    """A single database connection declared in a policy."""

    name: str
    type: str = "postgres"
    hostname: str
    port: int = 5432
    database: str
    user: str
    password: str
    env_prefix: str = "PG"

    @field_validator("password", mode="before")
    @classmethod
    def check_password(cls, v: str) -> str:
        """Validate password is not a plaintext secret."""
        if v == "trust":
            return "trust"
        if re.fullmatch(r"\$\{(\w+)\}", v):
            return v
        raise ValueError(
            f"Plaintext password rejected. Use '${{VAR_NAME}}' syntax or 'trust'. Got: {v!r}"
        )


class Policy(BaseModel):
    """Parsed policy file: databases + tool permissions + custom environment."""

    name: str
    description: str = ""
    databases: list[DatabaseConfig] = []
    tools: list[str] = []
    environment: dict[str, str] = {}

    @field_validator("environment", mode="before")
    @classmethod
    def check_environment(cls, v: Any) -> dict[str, str]:
        """Reject plaintext values for sensitive-pattern keys at parse time.

        Does NOT resolve ${VAR_NAME} -- resolution happens at container launch.
        """
        if not isinstance(v, dict):
            raise ValueError("environment must be a mapping of key-value pairs")
        for key, value in v.items():
            is_env_ref = bool(re.fullmatch(r"\$\{(\w+)\}", str(value)))
            if SECRET_KEY_PATTERN.search(key) and not is_env_ref:
                raise ValueError(
                    f"Sensitive key '{key}' must use '${{VAR_NAME}}' syntax, "
                    f"got plaintext: {value!r}"
                )
        return v


def load_policy(path: str | Path) -> Policy:
    """Parse and validate a policy YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Policy file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Policy file must be a YAML mapping: {path}")

    policy = Policy(**raw)
    log.info("policy.loaded", name=policy.name, databases=len(policy.databases), tools=policy.tools)
    return policy


def resolve_policy_for_service(service: str, policy_name: str) -> Policy:
    """Load a policy by service and policy name from the agents directory."""
    path = AGENTS_DIR / service / "policies" / f"{policy_name}.yaml"
    return load_policy(path)
