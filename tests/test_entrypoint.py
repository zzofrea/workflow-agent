"""Tests for container entrypoint (container/entrypoint.py).

These test the entrypoint functions in isolation without Docker.
We add the container/ directory to sys.path to import the module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add container/ to path so we can import entrypoint
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))

from entrypoint import (
    build_markdown_report,
    build_output,
    build_prompt,
    dispatch_runtime,
    parse_json_output,
    read_role_config,
    read_spec,
    run_claude_cli,
    setup_claude_auth,
)


class TestSetupClaudeAuth:
    """Tests for setup_claude_auth()."""

    def test_copies_claude_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / ".claude.json").write_text('{"key": "value"}')

        home = tmp_path / "home"
        home.mkdir()

        monkeypatch.setattr("entrypoint.AUTH_STAGING_DIR", str(staging))
        monkeypatch.setattr("pathlib.Path.home", lambda: home)

        setup_claude_auth()
        assert (home / ".claude.json").exists()
        assert json.loads((home / ".claude.json").read_text()) == {"key": "value"}

    def test_copies_claude_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        claude_dir = staging / ".claude"
        claude_dir.mkdir()
        (claude_dir / "config.json").write_text("{}")

        home = tmp_path / "home"
        home.mkdir()

        monkeypatch.setattr("entrypoint.AUTH_STAGING_DIR", str(staging))
        monkeypatch.setattr("pathlib.Path.home", lambda: home)

        setup_claude_auth()
        assert (home / ".claude" / "config.json").exists()


class TestReadRoleConfig:
    """Tests for read_role_config()."""

    def test_reads_valid_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = {"name": "test", "system_prompt": "Be helpful"}
        (tmp_path / "role.json").write_text(json.dumps(config))
        monkeypatch.setattr("entrypoint.INPUT_DIR", str(tmp_path))

        result = read_role_config()
        assert result["name"] == "test"

    def test_missing_config_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("entrypoint.INPUT_DIR", str(tmp_path))
        result = read_role_config()
        assert result == {}


class TestReadSpec:
    """Tests for read_spec()."""

    def test_reads_spec(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "spec.md").write_text("# Test Spec")
        monkeypatch.setattr("entrypoint.INPUT_DIR", str(tmp_path))
        assert read_spec() == "# Test Spec"

    def test_missing_spec_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("entrypoint.INPUT_DIR", str(tmp_path))
        assert read_spec() == ""


class TestBuildPrompt:
    """Tests for build_prompt()."""

    def test_with_spec(self) -> None:
        prompt = build_prompt("# Scenarios\n## S1")
        assert "Specification" in prompt
        assert "Scenarios" in prompt

    def test_without_spec(self) -> None:
        prompt = build_prompt("")
        assert "Execute your role" in prompt


class TestParseJsonOutput:
    """Tests for parse_json_output()."""

    def test_direct_json(self) -> None:
        result = parse_json_output('{"status": "pass"}')
        assert result == {"status": "pass"}

    def test_markdown_fenced_json(self) -> None:
        raw = 'Here is the report:\n```json\n{"status": "pass"}\n```\nDone.'
        result = parse_json_output(raw)
        assert result == {"status": "pass"}

    def test_brace_extraction(self) -> None:
        raw = 'Some text {"status": "pass"} more text'
        result = parse_json_output(raw)
        assert result == {"status": "pass"}

    def test_unparseable_returns_none(self) -> None:
        assert parse_json_output("Just some text with no JSON") is None

    def test_empty_returns_none(self) -> None:
        assert parse_json_output("") is None


class TestBuildOutput:
    """Tests for build_output()."""

    def test_json_format_pass(self) -> None:
        parsed = {
            "scenarios": [{"id": 1, "status": "pass"}],
            "summary": "All good",
        }
        report = build_output(parsed, "{}", "json", "auditor", "svc", "sonnet", 10.0)
        assert report["overall"] == "pass"
        assert report["scenarios_total"] == 1
        assert report["scenarios_pass"] == 1

    def test_json_format_incomplete(self) -> None:
        report = build_output(None, "", "json", "auditor", "svc", "sonnet", 5.0)
        assert report["overall"] == "incomplete"
        assert report["incomplete"] is True

    def test_markdown_format(self) -> None:
        report = build_output(
            None, "# Report\nData here", "markdown", "analyst", "svc", "sonnet", 8.0
        )
        assert report["overall"] == "complete"
        assert report["content"] == "# Report\nData here"

    def test_text_format(self) -> None:
        report = build_output(None, "Some output", "text", "role", "svc", "sonnet", 3.0)
        assert report["overall"] == "complete"
        assert report["content"] == "Some output"


class TestBuildMarkdownReport:
    """Tests for build_markdown_report()."""

    def test_renders_scenarios(self) -> None:
        report = {
            "role": "auditor",
            "service": "svc",
            "date": "2026-01-01",
            "model": "sonnet",
            "overall": "pass",
            "duration_seconds": 10.0,
            "scenarios": [
                {
                    "id": 1,
                    "status": "pass",
                    "description": "DB check",
                    "expected": "Works",
                    "observation": "Works",
                },
            ],
        }
        md = build_markdown_report(report)
        assert "[PASS]" in md
        assert "DB check" in md

    def test_renders_content(self) -> None:
        report = {
            "role": "analyst",
            "service": "svc",
            "date": "2026-01-01",
            "model": "sonnet",
            "overall": "complete",
            "duration_seconds": 5.0,
            "content": "# Analysis\nData here",
            "summary": "analysis report",
        }
        md = build_markdown_report(report)
        assert "# Analysis" in md


class TestRunClaudeCliRetry:
    """Tests for run_claude_cli() retry logic."""

    @patch("entrypoint.subprocess.run")
    def test_succeeds_first_try(self, mock_run: object) -> None:
        import subprocess as _sp

        mock_run.return_value = _sp.CompletedProcess(  # type: ignore[attr-defined]
            args=[], returncode=0, stdout='{"scenarios":[]}', stderr=""
        )
        output, _ = run_claude_cli("p", "s", "Read", retries=2)
        assert output == '{"scenarios":[]}'
        assert mock_run.call_count == 1  # type: ignore[attr-defined]

    @patch("entrypoint.time.sleep")
    @patch("entrypoint.subprocess.run")
    def test_retries_on_empty_output(self, mock_run: object, mock_sleep: object) -> None:
        import subprocess as _sp

        fail = _sp.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        success = _sp.CompletedProcess(args=[], returncode=0, stdout='{"scenarios":[]}', stderr="")
        mock_run.side_effect = [fail, success]  # type: ignore[attr-defined]
        output, _ = run_claude_cli("p", "s", "Read", retries=2, retry_delay=0.0)
        assert output == '{"scenarios":[]}'
        assert mock_run.call_count == 2  # type: ignore[attr-defined]
        mock_sleep.assert_called_once_with(0.0)  # type: ignore[attr-defined]

    @patch("entrypoint.time.sleep")
    @patch("entrypoint.subprocess.run")
    def test_exhausts_retries(self, mock_run: object, mock_sleep: object) -> None:
        import subprocess as _sp

        fail = _sp.CompletedProcess(args=[], returncode=1, stdout="", stderr="api error")
        mock_run.return_value = fail  # type: ignore[attr-defined]
        output, _ = run_claude_cli("p", "s", "Read", retries=1, retry_delay=0.0)
        assert output == ""
        assert mock_run.call_count == 2  # type: ignore[attr-defined]

    @patch("entrypoint.subprocess.run")
    def test_nonzero_exit_with_output_returns_immediately(self, mock_run: object) -> None:
        import subprocess as _sp

        mock_run.return_value = _sp.CompletedProcess(  # type: ignore[attr-defined]
            args=[], returncode=1, stdout="partial output", stderr="warn"
        )
        output, _ = run_claude_cli("p", "s", "Read", retries=2)
        assert output == "partial output"
        assert mock_run.call_count == 1  # type: ignore[attr-defined]


class TestDispatchRuntime:
    """Tests for dispatch_runtime()."""

    def test_unsupported_runtime_exits(self) -> None:
        with pytest.raises(SystemExit):
            dispatch_runtime("litellm", {}, "")

    @patch("entrypoint._run_claude_cli_runtime")
    def test_claude_cli_dispatched(self, mock_run: object) -> None:
        dispatch_runtime("claude-cli", {"model": "sonnet"}, "spec")
