"""Tests for container lifecycle (runner.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workflow_agent.policy import DatabaseConfig, Policy
from workflow_agent.runner import (
    _collect_output,
    _error_report,
    _prepare_input,
    build_docker_cmd,
    build_env_vars,
    run_agent,
)


def _make_policy(
    databases: list[DatabaseConfig] | None = None,
    tools: list[str] | None = None,
) -> Policy:
    """Helper to build a Policy for tests."""
    return Policy(
        name="test-policy",
        databases=databases or [],
        tools=tools or ["Read", "Bash(psql*)"],
    )


def _make_db(
    name: str = "primary",
    hostname: str = "test-postgres",
    password: str = "trust",
    env_prefix: str = "PG",
) -> DatabaseConfig:
    return DatabaseConfig(
        name=name,
        hostname=hostname,
        database="testdb",
        user="ro_user",
        password=password,
        env_prefix=env_prefix,
    )


class TestBuildEnvVars:
    """Tests for build_env_vars()."""

    def test_single_db_trust_auth(self) -> None:
        policy = _make_policy(databases=[_make_db()])
        env = build_env_vars(policy, "auditor", "test-service")
        env_dict = dict(env)

        assert env_dict["PGHOST"] == "test-postgres"
        assert env_dict["PGPORT"] == "5432"
        assert env_dict["PGUSER"] == "ro_user"
        assert env_dict["PGDATABASE"] == "testdb"
        assert "PGPASSWORD" not in env_dict  # trust auth = no password

    def test_single_db_with_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_PASS", "secret123")
        policy = _make_policy(databases=[_make_db(password="${DB_PASS}")])
        env = build_env_vars(policy, "auditor", "test-service")
        env_dict = dict(env)

        assert env_dict["PGPASSWORD"] == "secret123"

    def test_multi_db_env_vars(self) -> None:
        db1 = _make_db(name="bids", hostname="bid-postgres", env_prefix="PG")
        db2 = _make_db(name="etl", hostname="etl-postgres", env_prefix="ETL_DB")
        policy = _make_policy(databases=[db1, db2])
        env = build_env_vars(policy, "auditor", "svc")
        env_dict = dict(env)

        assert env_dict["PGHOST"] == "bid-postgres"
        assert env_dict["ETL_DBHOST"] == "etl-postgres"
        assert env_dict["ETL_DBDATABASE"] == "testdb"

    def test_service_and_role_vars(self) -> None:
        policy = _make_policy()
        env = build_env_vars(policy, "analyst", "bid-scraper")
        env_dict = dict(env)

        assert env_dict["AGENT_SERVICE"] == "bid-scraper"
        assert env_dict["AGENT_ROLE"] == "analyst"


class TestBuildDockerCmd:
    """Tests for build_docker_cmd()."""

    def test_contains_required_flags(self) -> None:
        policy = _make_policy(databases=[_make_db()])
        cmd = build_docker_cmd(
            "/tmp/in",
            "/tmp/out",
            service="test-svc",
            role_name="auditor",
            policy=policy,
            network="test-net",
        )

        assert "--rm" in cmd
        assert "--cap-drop" in cmd
        assert "ALL" in cmd
        assert "--network" in cmd
        assert "test-net" in cmd

    def test_tools_in_env(self) -> None:
        policy = _make_policy(tools=["Read", "Bash(psql*)", "Bash(python3*)"])
        cmd = build_docker_cmd(
            "/tmp/in",
            "/tmp/out",
            service="svc",
            role_name="role",
            policy=policy,
            network="net",
        )
        cmd_str = " ".join(cmd)
        assert "AGENT_TOOLS=Read,Bash(psql*),Bash(python3*)" in cmd_str

    def test_container_name_format(self) -> None:
        policy = _make_policy()
        cmd = build_docker_cmd(
            "/tmp/in",
            "/tmp/out",
            service="bid-scraper",
            role_name="auditor",
            policy=policy,
            network="net",
        )
        name_idx = cmd.index("--name") + 1
        assert cmd[name_idx] == "agent-bid-scraper-auditor"


class TestPrepareInput:
    """Tests for _prepare_input()."""

    def test_writes_role_and_spec(self, tmp_path: Path) -> None:
        input_dir = str(tmp_path)
        role_config = {"name": "test", "system_prompt": "test prompt"}
        _prepare_input(input_dir, role_config, "# Test Spec\n")

        role_path = tmp_path / "role.json"
        spec_path = tmp_path / "spec.md"
        assert role_path.exists()
        assert spec_path.exists()
        assert json.loads(role_path.read_text())["name"] == "test"
        assert spec_path.read_text() == "# Test Spec\n"

    def test_no_spec_skips_file(self, tmp_path: Path) -> None:
        _prepare_input(str(tmp_path), {"name": "test"}, "")
        assert not (tmp_path / "spec.md").exists()


class TestCollectOutput:
    """Tests for _collect_output()."""

    def test_reads_report_json(self, tmp_path: Path) -> None:
        report = {"overall": "pass", "summary": "All good"}
        (tmp_path / "report.json").write_text(json.dumps(report))

        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        output = _collect_output(str(tmp_path), "svc", "role", False, 300, result)
        assert output["overall"] == "pass"

    def test_timeout_returns_error(self, tmp_path: Path) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=-1, stdout="", stderr="")
        output = _collect_output(str(tmp_path), "svc", "role", True, 300, result)
        assert output["overall"] == "error"
        assert "timed out" in output["summary"]

    def test_no_report_returns_error(self, tmp_path: Path) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="some output", stderr="")
        output = _collect_output(str(tmp_path), "svc", "role", False, 300, result)
        assert output["overall"] == "error"
        assert "No report" in output["summary"]


class TestErrorReport:
    """Tests for _error_report()."""

    def test_structure(self) -> None:
        report = _error_report("svc", "role", "Something broke")
        assert report["overall"] == "error"
        assert report["service"] == "svc"
        assert report["role"] == "role"
        assert report["summary"] == "Something broke"
        assert report["scenarios"] == []


class TestRunAgent:
    """Tests for run_agent() with mocked Docker."""

    @patch("workflow_agent.runner.image_exists_locally", return_value=False)
    @patch("workflow_agent.runner.pull_image", return_value=False)
    def test_image_unavailable(self, mock_pull: MagicMock, mock_exists: MagicMock) -> None:
        policy = _make_policy(databases=[_make_db()])
        result = run_agent(
            "auditor",
            policy,
            "svc",
            role_config={"name": "test"},
        )
        assert result["overall"] == "error"
        assert "Could not pull" in result["summary"]

    @patch("workflow_agent.runner.image_exists_locally", return_value=True)
    @patch("workflow_agent.runner.resolve_container_names")
    @patch("workflow_agent.runner.check_container_running", return_value=False)
    def test_db_not_running(
        self, mock_check: MagicMock, mock_resolve: MagicMock, mock_exists: MagicMock
    ) -> None:
        mock_resolve.return_value = {"test-postgres": "actual-container-name"}
        policy = _make_policy(databases=[_make_db()])
        result = run_agent(
            "auditor",
            policy,
            "svc",
            role_config={"name": "test"},
        )
        assert result["overall"] == "error"
        assert "not running" in result["summary"]
