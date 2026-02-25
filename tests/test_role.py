"""Tests for role parsing and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from workflow_agent.role import Role, load_role, resolve_role_for_service


class TestRole:
    """Tests for Role model."""

    def test_valid_role(self) -> None:
        role = Role(
            name="test-auditor",
            policy="reader",
            system_prompt="You are a test auditor.",
        )
        assert role.name == "test-auditor"
        assert role.runtime == "claude-cli"
        assert role.model == "sonnet"
        assert role.max_turns == 50
        assert role.timeout == 300
        assert role.notify is True
        assert role.output_format == "json"

    def test_unsupported_runtime_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported runtime"):
            Role(
                name="bad-role",
                policy="reader",
                runtime="litellm",
            )

    def test_custom_values(self) -> None:
        role = Role(
            name="custom",
            policy="reader",
            model="opus",
            max_turns=30,
            timeout=600,
            notify=False,
            output_format="markdown",
            system_prompt="Custom prompt.",
        )
        assert role.model == "opus"
        assert role.max_turns == 30
        assert role.timeout == 600
        assert role.notify is False
        assert role.output_format == "markdown"


class TestLoadRole:
    """Tests for load_role() and YAML parsing."""

    def test_valid_role_file(self, tmp_path: Path, sample_role_yaml: str) -> None:
        role_file = tmp_path / "auditor.yaml"
        role_file.write_text(sample_role_yaml)

        role = load_role(role_file)
        assert role.name == "test-auditor"
        assert role.policy == "reader"
        assert role.spec == "audit.md"
        assert "test auditor" in role.system_prompt.lower()

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_role("/nonexistent/role.yaml")

    def test_missing_policy_ref_validation(self, tmp_path: Path) -> None:
        """Role must have a policy reference."""
        yaml_content = textwrap.dedent("""\
            name: no-policy-role
            system_prompt: "test"
        """)
        role_file = tmp_path / "bad.yaml"
        role_file.write_text(yaml_content)
        with pytest.raises(ValueError):
            load_role(role_file)

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        role_file = tmp_path / "bad.yaml"
        role_file.write_text("just a string")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_role(role_file)


class TestResolveRoleForService:
    """Tests for resolve_role_for_service()."""

    def test_missing_service_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("workflow_agent.role.AGENTS_DIR", Path("/nonexistent"))
        with pytest.raises(FileNotFoundError):
            resolve_role_for_service("no-service", "auditor")
