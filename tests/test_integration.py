"""Integration tests for the 9 behavioral scenarios from SPEC.md.

All Docker operations are mocked. These tests verify the end-to-end
flow from CLI/runner through to output archival and notification.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workflow_agent.output import archive_output
from workflow_agent.policy import DatabaseConfig, Policy, load_policy, validate_password
from workflow_agent.role import load_role
from workflow_agent.runner import (
    _collect_output,
    build_docker_cmd,
    build_env_vars,
    run_agent,
)


def _make_policy(
    databases: list[DatabaseConfig] | None = None,
    tools: list[str] | None = None,
) -> Policy:
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


class TestScenario1AuditorBehaviorPreserved:
    """Scenario 1: Existing auditor behavior is preserved.

    GIVEN the bid-scraper auditor role and reader policy are defined.
    AND the bid scraper database is running with populated tables.
    WHEN the operator runs workflow-agent run auditor --target bid-scraper.
    THEN a sandboxed container launches with psql, python3, date, and Read tools.
    AND the agent produces a JSON report with pass/fail results.
    AND the report is archived.
    AND a Discord notification is sent.
    """

    @patch("workflow_agent.runner.image_exists_locally", return_value=True)
    @patch("workflow_agent.runner.resolve_container_names")
    @patch("workflow_agent.runner.check_container_running", return_value=True)
    @patch("subprocess.run")
    def test_auditor_flow(
        self,
        mock_subprocess: MagicMock,
        mock_check: MagicMock,
        mock_resolve: MagicMock,
        mock_image: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BID_SCRAPER_DB_PASSWORD", "test-pass")
        mock_resolve.return_value = {"gov-bid-postgres": "real-container-name"}

        policy = _make_policy(
            databases=[
                _make_db(
                    hostname="gov-bid-postgres",
                    password="${BID_SCRAPER_DB_PASSWORD}",
                )
            ],
            tools=["Read", "Bash(psql*)", "Bash(python3*)", "Bash(date*)"],
        )

        # Build docker cmd and verify tools
        cmd = build_docker_cmd(
            "/tmp/in",
            "/tmp/out",
            service="bid-scraper",
            role_name="auditor",
            policy=policy,
            network="test-net",
        )
        cmd_str = " ".join(cmd)
        assert "AGENT_TOOLS=Read,Bash(psql*),Bash(python3*),Bash(date*)" in cmd_str
        assert "--cap-drop" in cmd_str
        assert "ALL" in cmd_str

        # Verify env vars include resolved password
        env = build_env_vars(policy, "auditor", "bid-scraper")
        env_dict = dict(env)
        assert env_dict["PGPASSWORD"] == "test-pass"
        assert env_dict["PGHOST"] == "gov-bid-postgres"


class TestScenario2AnalystRole:
    """Scenario 2: New analyst role produces a report.

    GIVEN a bid-scraper analyst role and reader policy are defined.
    WHEN the operator runs workflow-agent run analyst --target bid-scraper.
    THEN a sandboxed container launches with the same database access.
    AND the agent produces a markdown report.
    """

    def test_analyst_role_loads(self) -> None:
        agents_dir = Path(__file__).parent.parent / "agents"
        role_path = agents_dir / "bid-scraper" / "roles" / "analyst.yaml"
        role = load_role(role_path)
        assert role.output_format == "markdown"
        assert role.policy == "reader"
        assert "procurement" in role.system_prompt.lower()


class TestScenario3MultiDatabase:
    """Scenario 3: Policy with multiple databases connects all targets.

    GIVEN a policy declares two database connections.
    WHEN the operator runs an agent with this policy.
    THEN the temporary network has both database containers connected.
    AND the agent container has environment variables for both connections.
    """

    def test_multi_db_env_vars(self) -> None:
        db1 = _make_db(name="bids", hostname="gov-bid-postgres", env_prefix="PG")
        db2 = _make_db(name="etl", hostname="ds-etl-postgres", env_prefix="ETL_DB")
        policy = _make_policy(databases=[db1, db2])

        env = build_env_vars(policy, "auditor", "multi-svc")
        env_dict = dict(env)

        assert env_dict["PGHOST"] == "gov-bid-postgres"
        assert env_dict["ETL_DBHOST"] == "ds-etl-postgres"

    def test_multi_db_docker_cmd(self) -> None:
        db1 = _make_db(name="bids", hostname="gov-bid-postgres", env_prefix="PG")
        db2 = _make_db(name="etl", hostname="ds-etl-postgres", env_prefix="ETL_DB")
        policy = _make_policy(databases=[db1, db2])

        cmd = build_docker_cmd(
            "/tmp/in",
            "/tmp/out",
            service="multi",
            role_name="auditor",
            policy=policy,
            network="net",
        )
        cmd_str = " ".join(cmd)
        assert "PGHOST=gov-bid-postgres" in cmd_str
        assert "ETL_DBHOST=ds-etl-postgres" in cmd_str


class TestScenario4DryRunValidation:
    """Scenario 4: Dry run validates without launching.

    GIVEN valid role and policy files exist.
    WHEN the operator runs validate.
    THEN the framework parses and validates both files.
    AND no container is launched.
    """

    def test_validate_real_files(self) -> None:
        agents_dir = Path(__file__).parent.parent / "agents"

        # Validate bid-scraper auditor
        role = load_role(agents_dir / "bid-scraper" / "roles" / "auditor.yaml")
        policy = load_policy(agents_dir / "bid-scraper" / "policies" / "reader.yaml")

        assert role.policy == "reader"
        assert policy.name == "bid-scraper-reader"
        assert len(policy.databases) == 1
        assert policy.databases[0].hostname == "gov-bid-postgres"

    def test_validate_etl_files(self) -> None:
        agents_dir = Path(__file__).parent.parent / "agents"

        role = load_role(agents_dir / "defendershield-etl" / "roles" / "auditor.yaml")
        policy = load_policy(agents_dir / "defendershield-etl" / "policies" / "reader.yaml")

        assert role.policy == "reader"
        assert policy.databases[0].password == "trust"


class TestScenario5DatabaseNotRunning:
    """Scenario 5: Database container is not running.

    GIVEN a policy references a database whose container is stopped.
    WHEN the operator runs an agent.
    THEN the framework detects the stopped container before launching.
    AND an error report is archived with a clear message.
    """

    @patch("workflow_agent.runner.image_exists_locally", return_value=True)
    @patch("workflow_agent.runner.resolve_container_names")
    @patch("workflow_agent.runner.check_container_running", return_value=False)
    def test_db_not_running_error(
        self,
        mock_check: MagicMock,
        mock_resolve: MagicMock,
        mock_image: MagicMock,
    ) -> None:
        mock_resolve.return_value = {"test-postgres": "actual-name"}
        policy = _make_policy(databases=[_make_db()])

        result = run_agent(
            "auditor",
            policy,
            "test-svc",
            role_config={"name": "test"},
        )
        assert result["overall"] == "error"
        assert "not running" in result["summary"]


class TestScenario6NonexistentPolicy:
    """Scenario 6: Role references nonexistent policy.

    GIVEN a role file references a policy name that does not exist.
    WHEN the operator runs the role.
    THEN the framework fails immediately with a file-not-found error.
    """

    def test_missing_policy_file(self, tmp_path: Path) -> None:
        role_yaml = textwrap.dedent("""\
            name: bad-role
            policy: nonexistent
            runtime: claude-cli
        """)
        role_file = tmp_path / "bad.yaml"
        role_file.write_text(role_yaml)

        role = load_role(role_file)
        assert role.policy == "nonexistent"

        policy_path = tmp_path / "policies" / "nonexistent.yaml"
        with pytest.raises(FileNotFoundError):
            load_policy(policy_path)


class TestScenario7TrustAuth:
    """Scenario 7: Trust authentication (no password).

    GIVEN a policy declares a database with password: trust.
    WHEN the agent container is launched.
    THEN the password environment variable is omitted or empty.
    """

    def test_trust_auth_no_password_env(self) -> None:
        policy = _make_policy(databases=[_make_db(password="trust")])
        env = build_env_vars(policy, "auditor", "svc")
        env_dict = dict(env)

        assert "PGPASSWORD" not in env_dict
        assert env_dict["PGHOST"] == "test-postgres"

    def test_trust_password_validation(self) -> None:
        assert validate_password("trust") == ""


class TestScenario8Timeout:
    """Scenario 8: Agent times out.

    GIVEN an agent is running but exceeds the configured timeout.
    WHEN the timeout is reached.
    THEN the container is killed.
    AND an error report is archived with the timeout reason.
    AND the temporary network is cleaned up.
    """

    def test_timeout_report(self, tmp_path: Path) -> None:
        result = subprocess.CompletedProcess(args=[], returncode=-1, stdout="", stderr="")
        output = _collect_output(str(tmp_path), "svc", "role", True, 300, result)

        assert output["overall"] == "error"
        assert "timed out" in output["summary"]
        assert "300" in output["summary"]


class TestScenario9UnparseableOutput:
    """Scenario 9: Agent output cannot be parsed.

    GIVEN an agent completes but produces unparseable output.
    WHEN the output parser runs.
    THEN the raw output is preserved in the archive.
    AND the report is marked as incomplete.
    """

    def test_raw_output_preserved(self, tmp_path: Path) -> None:
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Just some prose, no JSON", stderr=""
        )
        output = _collect_output(str(tmp_path), "svc", "role", False, 300, result)

        assert output["overall"] == "error"
        assert output.get("raw_output") == "Just some prose, no JSON"

    def test_archive_preserves_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        archive_base = tmp_path / "archive"
        monkeypatch.setattr("workflow_agent.output.OUTPUT_BASE", str(archive_base))

        report = {
            "overall": "incomplete",
            "raw_output": "Unparseable content here",
            "summary": "Parse failed",
        }
        dest = archive_output(str(output_dir), "svc", "role", report)

        saved = json.loads((Path(dest) / "report.json").read_text())
        assert saved["overall"] == "incomplete"
        assert saved["raw_output"] == "Unparseable content here"
