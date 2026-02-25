"""Tests for policy parsing and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from workflow_agent.policy import (
    DatabaseConfig,
    load_policy,
    resolve_env_var,
    resolve_policy_for_service,
    validate_password,
)


class TestResolveEnvVar:
    """Tests for resolve_env_var()."""

    def test_resolves_set_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET", "hunter2")
        assert resolve_env_var("${MY_SECRET}") == "hunter2"

    def test_raises_on_unset_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        with pytest.raises(ValueError, match="NONEXISTENT_VAR"):
            resolve_env_var("${NONEXISTENT_VAR}")

    def test_literal_string_passthrough(self) -> None:
        assert resolve_env_var("trust") == "trust"
        assert resolve_env_var("plain-text") == "plain-text"

    def test_empty_string_passthrough(self) -> None:
        assert resolve_env_var("") == ""


class TestValidatePassword:
    """Tests for validate_password()."""

    def test_trust_returns_empty(self) -> None:
        assert validate_password("trust") == ""

    def test_env_var_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_PASS", "resolved-pass")
        assert validate_password("${DB_PASS}") == "resolved-pass"

    def test_plaintext_rejected(self) -> None:
        with pytest.raises(ValueError, match="Plaintext password rejected"):
            validate_password("my-secret-password")

    def test_partial_env_var_rejected(self) -> None:
        with pytest.raises(ValueError, match="Plaintext password rejected"):
            validate_password("prefix_${VAR}")


class TestDatabaseConfig:
    """Tests for DatabaseConfig model."""

    def test_trust_password_accepted(self) -> None:
        db = DatabaseConfig(
            name="primary",
            hostname="test-postgres",
            database="testdb",
            user="ro_user",
            password="trust",
        )
        assert db.password == "trust"

    def test_env_var_password_accepted(self) -> None:
        db = DatabaseConfig(
            name="primary",
            hostname="test-postgres",
            database="testdb",
            user="ro_user",
            password="${SOME_VAR}",
        )
        assert db.password == "${SOME_VAR}"

    def test_plaintext_password_rejected(self) -> None:
        with pytest.raises(ValueError, match="Plaintext password rejected"):
            DatabaseConfig(
                name="primary",
                hostname="test-postgres",
                database="testdb",
                user="ro_user",
                password="literal-secret",
            )

    def test_defaults(self) -> None:
        db = DatabaseConfig(
            name="primary",
            hostname="test-postgres",
            database="testdb",
            user="ro_user",
            password="trust",
        )
        assert db.type == "postgres"
        assert db.port == 5432
        assert db.env_prefix == "PG"


class TestLoadPolicy:
    """Tests for load_policy() and YAML parsing."""

    def test_valid_policy(
        self, tmp_path: Path, sample_policy_yaml: str, env_with_test_password: None
    ) -> None:
        policy_file = tmp_path / "reader.yaml"
        policy_file.write_text(sample_policy_yaml)

        policy = load_policy(policy_file)
        assert policy.name == "test-reader"
        assert len(policy.databases) == 1
        assert policy.databases[0].hostname == "test-postgres"
        assert len(policy.tools) == 4

    def test_multi_database_policy(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            name: multi-reader
            description: Multi-database policy

            databases:
              - name: bids
                type: postgres
                hostname: gov-bid-postgres
                port: 5432
                database: govbids
                user: auditor_ro
                password: ${BID_DB_PASS}
                env_prefix: PG

              - name: etl
                type: postgres
                hostname: ds-etl-postgres
                port: 5432
                database: defendershield
                user: auditor_ro
                password: trust
                env_prefix: ETL_DB

            tools:
              - "Read"
              - "Bash(psql*)"
        """)
        policy_file = tmp_path / "multi.yaml"
        policy_file.write_text(yaml_content)

        policy = load_policy(policy_file)
        assert len(policy.databases) == 2
        assert policy.databases[0].env_prefix == "PG"
        assert policy.databases[1].env_prefix == "ETL_DB"
        assert policy.databases[1].password == "trust"

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_policy("/nonexistent/policy.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "bad.yaml"
        policy_file.write_text("just a string")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_policy(policy_file)

    def test_empty_policy(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            name: empty-policy
        """)
        policy_file = tmp_path / "empty.yaml"
        policy_file.write_text(yaml_content)

        policy = load_policy(policy_file)
        assert policy.name == "empty-policy"
        assert policy.databases == []
        assert policy.tools == []


class TestResolvePolicyForService:
    """Tests for resolve_policy_for_service()."""

    def test_missing_service_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("workflow_agent.policy.AGENTS_DIR", Path("/nonexistent"))
        with pytest.raises(FileNotFoundError):
            resolve_policy_for_service("no-service", "reader")
