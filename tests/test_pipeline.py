"""End-to-end pipeline tests for duplicate-send protection, failure handling, and retries."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from app import run_pipeline
from config import AppConfig
from services.gmail_service import GmailServiceError
from services.google_sheets import GoogleSheetsReadOnlyClient, RawSurveyRow


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        spreadsheet_id="sheet-123",
        gmail_thread_id="thread-xyz",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
        send_state_file=tmp_path / "logs" / "send_state.json",
    )


def test_pipeline_aborts_without_sending_when_processing_fails(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    sheets_client = MagicMock(spec=GoogleSheetsReadOnlyClient)
    sheets_client.fetch_survey_data.return_value = [
        RawSurveyRow(2, "", "1", "34W", "CORRECT", "", "CORRECT", "", "", "CCMS1")  # invalid: missing ward
    ]
    gmail_client = MagicMock()

    exit_code = run_pipeline(
        cfg,
        report_date="2026-11-25",
        sheets_client=sheets_client,
        gmail_client=gmail_client,
    )

    assert exit_code == 1
    gmail_client.send_report_in_thread.assert_not_called()
    assert list((tmp_path / "reports").glob("*.xlsx")) == []


def test_pipeline_retains_report_on_email_failure_and_recovers_on_retry(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    sheets_client = MagicMock(spec=GoogleSheetsReadOnlyClient)
    sheets_client.fetch_survey_data.return_value = [
        RawSurveyRow(2, "12", "2", "34W", "CORRECT", "", "CORRECT", "", "", "CCMS001")
    ]
    gmail_client = MagicMock()
    gmail_client.send_report_in_thread.side_effect = GmailServiceError("Temporary Gmail outage")

    first_code = run_pipeline(
        cfg,
        report_date="2026-11-25",
        sheets_client=sheets_client,
        gmail_client=gmail_client,
    )
    assert first_code == 1

    expected_report = tmp_path / "reports" / "Ward_Wise_CCMS_Report_2026-11-25.xlsx"
    assert expected_report.exists()

    state = json.loads(cfg.send_state_file.read_text(encoding="utf-8"))
    assert state["2026-11-25"]["status"] == "email_failed"

    # Retry email using retained workbook
    gmail_client.send_report_in_thread.side_effect = None
    gmail_client.send_report_in_thread.return_value = "msg-recovered-1"

    retry_code = run_pipeline(
        cfg,
        report_date="2026-11-25",
        retry_email=True,
        gmail_client=gmail_client,
    )
    assert retry_code == 0
    state_after = json.loads(cfg.send_state_file.read_text(encoding="utf-8"))
    assert state_after["2026-11-25"]["status"] == "sent"
    assert state_after["2026-11-25"]["message_id"] == "msg-recovered-1"


def test_pipeline_duplicate_send_protection_and_force_override(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    sheets_client = MagicMock(spec=GoogleSheetsReadOnlyClient)
    sheets_client.fetch_survey_data.return_value = [
        RawSurveyRow(2, "12", "2", "34W", "CORRECT", "", "CORRECT", "", "", "CCMS001")
    ]
    gmail_client = MagicMock()
    gmail_client.send_report_in_thread.return_value = "msg-1"

    assert run_pipeline(cfg, report_date="2026-11-25", sheets_client=sheets_client, gmail_client=gmail_client) == 0
    assert gmail_client.send_report_in_thread.call_count == 1

    # Second run on same date is skipped automatically
    assert run_pipeline(cfg, report_date="2026-11-25", sheets_client=sheets_client, gmail_client=gmail_client) == 0
    assert gmail_client.send_report_in_thread.call_count == 1

    # --force overrides duplicate protection
    assert run_pipeline(cfg, report_date="2026-11-25", force=True, sheets_client=sheets_client, gmail_client=gmail_client) == 0
    assert gmail_client.send_report_in_thread.call_count == 2
