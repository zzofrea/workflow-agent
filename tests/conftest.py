"""Shared test fixtures for workflow-agent."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_agents_dir(tmp_path: Path) -> Path:
    """Create a temporary agents directory with a test service."""
    service_dir = tmp_path / "agents" / "test-service"
    (service_dir / "policies").mkdir(parents=True)
    (service_dir / "roles").mkdir(parents=True)
    (service_dir / "specs").mkdir(parents=True)
    return tmp_path / "agents"


@pytest.fixture()
def sample_policy_yaml() -> str:
    """Valid policy YAML content."""
    return textwrap.dedent("""\
        name: test-reader
        description: Read-only access to test database

        databases:
          - name: primary
            type: postgres
            hostname: test-postgres
            port: 5432
            database: testdb
            user: test_ro
            password: ${TEST_DB_PASSWORD}
            env_prefix: PG

        tools:
          - "Read"
          - "Bash(psql*)"
          - "Bash(python3*)"
          - "Bash(date*)"
    """)


@pytest.fixture()
def sample_role_yaml() -> str:
    """Valid role YAML content."""
    return textwrap.dedent("""\
        name: test-auditor
        description: Test behavioral auditor
        policy: reader
        spec: audit.md

        runtime: claude-cli
        model: sonnet
        max_turns: 50
        timeout: 300
        notify: true
        output_format: json

        system_prompt: |
          You are a test auditor.
    """)


@pytest.fixture()
def env_with_test_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set TEST_DB_PASSWORD in environment."""
    monkeypatch.setenv("TEST_DB_PASSWORD", "s3cret-from-env")
