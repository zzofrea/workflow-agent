"""Tests for CLI (cli.py)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workflow_agent.cli import cmd_list, cmd_validate


class TestCmdList:
    """Tests for cmd_list()."""

    def test_lists_roles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        agents_dir = tmp_path / "agents"
        roles_dir = agents_dir / "test-svc" / "roles"
        roles_dir.mkdir(parents=True)
        (roles_dir / "auditor.yaml").write_text("name: auditor\npolicy: reader")
        (roles_dir / "analyst.yaml").write_text("name: analyst\npolicy: reader")

        monkeypatch.setattr("workflow_agent.cli.AGENTS_DIR", agents_dir)

        args = MagicMock()
        args.target = "test-svc"
        cmd_list(args)

        captured = capsys.readouterr()
        assert "auditor" in captured.out
        assert "analyst" in captured.out

    def test_missing_service_exits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("workflow_agent.cli.AGENTS_DIR", tmp_path)
        args = MagicMock()
        args.target = "nonexistent"
        with pytest.raises(SystemExit):
            cmd_list(args)


class TestCmdValidate:
    """Tests for cmd_validate()."""

    def test_valid_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        agents_dir = tmp_path / "agents"
        svc_dir = agents_dir / "test-svc"
        (svc_dir / "roles").mkdir(parents=True)
        (svc_dir / "policies").mkdir(parents=True)
        (svc_dir / "specs").mkdir(parents=True)

        role_yaml = textwrap.dedent("""\
            name: test-auditor
            policy: reader
            spec: audit.md
            runtime: claude-cli
            system_prompt: "Test"
        """)
        (svc_dir / "roles" / "auditor.yaml").write_text(role_yaml)

        policy_yaml = textwrap.dedent("""\
            name: test-reader
            databases:
              - name: primary
                hostname: test-postgres
                database: testdb
                user: ro
                password: trust
            tools:
              - "Read"
        """)
        (svc_dir / "policies" / "reader.yaml").write_text(policy_yaml)
        (svc_dir / "specs" / "audit.md").write_text("# Spec")

        monkeypatch.setattr("workflow_agent.cli.AGENTS_DIR", agents_dir)
        monkeypatch.setattr("workflow_agent.role.AGENTS_DIR", agents_dir)
        monkeypatch.setattr("workflow_agent.policy.AGENTS_DIR", agents_dir)

        args = MagicMock()
        args.target = "test-svc"
        args.role = "auditor"

        # Mock Docker checks since we're not running Docker in tests
        with (
            patch(
                "workflow_agent.cli.resolve_container_names",
                return_value={"test-postgres": "test-postgres"},
            ),
            patch("workflow_agent.cli.check_container_running", return_value=True),
        ):
            cmd_validate(args)

        captured = capsys.readouterr()
        assert "PASSED" in captured.out

    def test_missing_role_exits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("workflow_agent.cli.AGENTS_DIR", tmp_path)
        args = MagicMock()
        args.target = "nonexistent"
        args.role = "missing"
        with pytest.raises(SystemExit):
            cmd_validate(args)


class TestCmdRun:
    """Tests for cmd_run() with mocked runner."""

    @patch("workflow_agent.cli.run_agent")
    @patch("workflow_agent.cli.archive_output")
    @patch("workflow_agent.cli.route_notifications")
    def test_happy_path(
        self,
        mock_notify: MagicMock,
        mock_archive: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Set up agents dir
        agents_dir = tmp_path / "agents"
        svc_dir = agents_dir / "test-svc"
        (svc_dir / "roles").mkdir(parents=True)
        (svc_dir / "policies").mkdir(parents=True)
        (svc_dir / "specs").mkdir(parents=True)

        role_yaml = textwrap.dedent("""\
            name: test-auditor
            policy: reader
            spec: audit.md
            runtime: claude-cli
            system_prompt: "Test"
        """)
        (svc_dir / "roles" / "auditor.yaml").write_text(role_yaml)

        policy_yaml = textwrap.dedent("""\
            name: test-reader
            databases: []
            tools:
              - "Read"
        """)
        (svc_dir / "policies" / "reader.yaml").write_text(policy_yaml)
        (svc_dir / "specs" / "audit.md").write_text("# Spec")

        monkeypatch.setattr("workflow_agent.cli.AGENTS_DIR", agents_dir)
        monkeypatch.setattr("workflow_agent.role.AGENTS_DIR", agents_dir)
        monkeypatch.setattr("workflow_agent.policy.AGENTS_DIR", agents_dir)

        mock_run.return_value = {"overall": "pass", "summary": "All good"}
        mock_archive.return_value = "/tmp/archive"

        from workflow_agent.cli import cmd_run

        args = MagicMock()
        args.role = "auditor"
        args.target = "test-svc"
        args.model = None
        args.timeout = None
        args.max_turns = None
        args.no_notify = False

        cmd_run(args)

        mock_run.assert_called_once()
        mock_archive.assert_called_once()
        mock_notify.assert_called_once()
