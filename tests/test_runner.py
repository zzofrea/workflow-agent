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

    def test_custom_env_vars_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMTP_PASS", "resolved-secret")
        policy = _make_policy(databases=[_make_db()])
        policy.environment = {
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PASSWORD": "${SMTP_PASS}",
        }
        env = build_env_vars(policy, "analyst", "etl")
        env_dict = dict(env)

        assert env_dict["SMTP_HOST"] == "smtp.gmail.com"
        assert env_dict["SMTP_PASSWORD"] == "resolved-secret"

    def test_builtin_key_override_blocked(self) -> None:
        policy = _make_policy(databases=[_make_db()])
        policy.environment = {"AGENT_SERVICE": "hacked"}
        env = build_env_vars(policy, "auditor", "test-service")
        env_dict = dict(env)

        # Original value preserved, not overridden
        assert env_dict["AGENT_SERVICE"] == "test-service"


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


class TestExtraHostsInCmd:
    """Tests for --add-host from policy.extra_hosts."""

    def test_extra_hosts_in_docker_cmd(self) -> None:
        policy = _make_policy(databases=[_make_db()])
        policy.extra_hosts = ["db.example.com:host-gateway", "host.docker.internal:host-gateway"]
        cmd = build_docker_cmd(
            "/tmp/in",
            "/tmp/out",
            service="svc",
            role_name="auditor",
            policy=policy,
            network="net",
        )
        cmd_str = " ".join(cmd)
        assert "--add-host db.example.com:host-gateway" in cmd_str
        assert "--add-host host.docker.internal:host-gateway" in cmd_str

    def test_no_extra_hosts_by_default(self) -> None:
        policy = _make_policy(databases=[_make_db()])
        cmd = build_docker_cmd(
            "/tmp/in",
            "/tmp/out",
            service="svc",
            role_name="auditor",
            policy=policy,
            network="net",
        )
        assert "--add-host" not in cmd


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

    @patch("workflow_agent.runner.image_exists_locally", return_value=True)
    @patch("workflow_agent.runner.resolve_container_names")
    @patch("workflow_agent.runner.check_container_running")
    def test_external_db_skips_container_checks(
        self,
        mock_check: MagicMock,
        mock_resolve: MagicMock,
        mock_exists: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """docker=False databases should not trigger resolve or running checks."""
        monkeypatch.setenv("EXT_PASS", "test-secret")
        external_db = DatabaseConfig(
            name="external",
            type="mssql",
            hostname="db.example.com",
            port=1433,
            database="mydb",
            user="reader",
            password="${EXT_PASS}",
            env_prefix="MSSQL_",
            docker=False,
        )
        policy = _make_policy(databases=[external_db])
        mock_resolve.return_value = {}
        mock_check.return_value = False

        with patch("workflow_agent.runner.subprocess") as mock_subprocess:
            mock_subprocess.CalledProcessError = subprocess.CalledProcessError
            mock_subprocess.TimeoutExpired = subprocess.TimeoutExpired
            mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_agent(
                "auditor",
                policy,
                "svc",
                role_config={"name": "test"},
            )

        # No docker DBs means empty hostnames set, so resolve is skipped
        mock_resolve.assert_not_called()
        # check_container_running should never be called for external DBs
        mock_check.assert_not_called()
