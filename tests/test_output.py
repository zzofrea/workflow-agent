"""Tests for output archival and notification routing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workflow_agent.output import (
    _is_critical,
    archive_dir,
    archive_output,
    route_notifications,
)


class TestArchiveDir:
    """Tests for archive_dir()."""

    def test_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("workflow_agent.output.OUTPUT_BASE", "/tmp/agent-output")
        path = archive_dir("bid-scraper", "auditor")
        assert path.startswith("/tmp/agent-output/bid-scraper/auditor_")
        # Should contain date stamp
        assert "202" in path


class TestArchiveOutput:
    """Tests for archive_output()."""

    def test_archives_report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive_base = tmp_path / "archive"
        monkeypatch.setattr("workflow_agent.output.OUTPUT_BASE", str(archive_base))

        report = {"overall": "pass", "summary": "All good"}
        dest = archive_output("svc", "auditor", report)

        assert Path(dest).exists()
        assert (Path(dest) / "report.json").exists()

        saved = json.loads((Path(dest) / "report.json").read_text())
        assert saved["overall"] == "pass"

    def test_creates_directories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive_base = tmp_path / "deep" / "nested" / "archive"
        monkeypatch.setattr("workflow_agent.output.OUTPUT_BASE", str(archive_base))

        report = {"overall": "pass"}
        dest = archive_output("svc", "role", report)
        assert Path(dest).exists()


class TestIsCritical:
    """Tests for _is_critical()."""

    def test_connection_refused_is_critical(self) -> None:
        failures = [{"observation": "Connection refused to database"}]
        assert _is_critical(failures) == "critical"

    def test_zero_rows_is_critical(self) -> None:
        failures = [{"evidence": "Found 0 rows in table"}]
        assert _is_critical(failures) == "critical"

    def test_data_quality_is_warning(self) -> None:
        failures = [{"observation": "95% completeness below threshold"}]
        assert _is_critical(failures) == "warning"

    def test_empty_failures_is_warning(self) -> None:
        assert _is_critical([]) == "warning"


class TestRouteNotifications:
    """Tests for route_notifications()."""

    @patch("workflow_agent.output.fanout")
    @patch("workflow_agent.output.NotifyConfig")
    def test_pass_sends_success(self, mock_config: MagicMock, mock_fanout: MagicMock) -> None:
        report = {"overall": "pass", "summary": "All scenarios passed"}
        route_notifications(report, "svc", "auditor")
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args[1]
        assert call_kwargs["severity"] == "success"

    @patch("workflow_agent.output.fanout")
    @patch("workflow_agent.output.NotifyConfig")
    def test_fail_sends_warning(self, mock_config: MagicMock, mock_fanout: MagicMock) -> None:
        report = {
            "overall": "fail",
            "summary": "1 failure",
            "scenarios": [
                {"id": 1, "status": "fail", "observation": "Data stale"},
            ],
        }
        route_notifications(report, "svc", "auditor")
        mock_fanout.assert_called_once()
        call_kwargs = mock_fanout.call_args[1]
        assert call_kwargs["severity"] == "warning"

    @patch("workflow_agent.output.fanout", None)
    @patch("workflow_agent.output.NotifyConfig", None)
    def test_missing_notify_does_not_raise(self) -> None:
        route_notifications({"overall": "pass"}, "svc", "role")
