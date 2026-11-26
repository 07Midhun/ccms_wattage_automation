"""Tests for SMTP + IMAP thread resolution, MIME headers, attachments, and pipeline integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import run_pipeline
from config import AppConfig
from services.google_sheets import GoogleSheetsReadOnlyClient, RawSurveyRow
from services.smtp_service import (
    SMTPThreadClient,
    _parse_gmail_thread_id_decimal,
    fetch_thread_headers_via_imap,
)


def _smtp_config(tmp_path: Path, **overrides: object) -> AppConfig:
    defaults: dict[str, object] = {
        "spreadsheet_id": "sheet-123",
        "email_transport": "smtp",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_use_tls": True,
        "smtp_username": "sender@example.com",
        "smtp_password": "app-password",
        "sender_email": "sender@example.com",
        "recipient_emails": ["recipient@example.com"],
        "email_cc": ["cc@example.com"],
        "reports_dir": tmp_path / "reports",
        "logs_dir": tmp_path / "logs",
        "send_state_file": tmp_path / "logs" / "send_state.json",
    }
    defaults.update(overrides)
    return AppConfig(**defaults)  # type: ignore[arg-type]


def test_parse_gmail_thread_id_decimal_converts_hex_and_decimal() -> None:
    assert _parse_gmail_thread_id_decimal("193a4b5c6d7e8f90") == str(int("193a4b5c6d7e8f90", 16))
    assert _parse_gmail_thread_id_decimal("1817691234567890") == "1817691234567890"
    assert _parse_gmail_thread_id_decimal("your_existing_gmail_thread_id_here") is None


def test_fetch_thread_headers_via_imap_reads_subject_message_id_and_references(tmp_path: Path) -> None:
    cfg = _smtp_config(tmp_path, gmail_thread_id="193a4b5c6d7e8f90")
    raw_hdrs = (
        b"Subject: Daily Ward-wise CCMS Wattage Report\r\n"
        b"Message-ID: <parent-msg@mail.gmail.com>\r\n"
        b"References: <root-msg@mail.gmail.com>\r\n\r\n"
    )
    mock_imap = MagicMock()
    mock_imap.select.return_value = ("OK", [b"1"])
    mock_imap.uid.side_effect = [
        ("OK", [b"42 43"]),
        ("OK", [(b"43 (X-GM-THRID 1817849154710933392 BODY[HEADER.FIELDS (SUBJECT MESSAGE-ID REFERENCES)] {120}", raw_hdrs), b")"]),
    ]

    with patch("services.smtp_service.imaplib.IMAP4_SSL", return_value=mock_imap):
        ctx = fetch_thread_headers_via_imap(cfg)

    assert ctx is not None
    assert ctx.subject == "Re: Daily Ward-wise CCMS Wattage Report"
    assert ctx.in_reply_to == "<parent-msg@mail.gmail.com>"
    assert ctx.references == "<root-msg@mail.gmail.com> <parent-msg@mail.gmail.com>"


def test_smtp_client_sends_threaded_reply_with_tls_and_attachment(tmp_path: Path) -> None:
    report_path = tmp_path / "Ward_Wise_CCMS_Report_2026-11-26.xlsx"
    report_path.write_bytes(b"PK\x03\x04dummy-xlsx")

    cfg = _smtp_config(
        tmp_path,
        smtp_thread_subject="CCMS Daily Wattage Report",
        smtp_thread_message_id="<orig@mail.gmail.com>",
    )

    mock_smtp_server = MagicMock()
    mock_smtp_server.__enter__.return_value = mock_smtp_server

    with patch("services.smtp_service.fetch_thread_headers_via_imap", return_value=None):
        client = SMTPThreadClient(cfg, smtp_factory=lambda host, port: mock_smtp_server)
        msg_id, meta = client.send_report_in_thread(report_path, "2026-11-26")

    mock_smtp_server.starttls.assert_called_once()
    mock_smtp_server.login.assert_called_once_with("sender@example.com", "app-password")
    mock_smtp_server.send_message.assert_called_once()

    sent_msg = mock_smtp_server.send_message.call_args.args[0]
    assert sent_msg["From"] == "sender@example.com"
    assert sent_msg["To"] == "recipient@example.com"
    assert sent_msg["Cc"] == "cc@example.com"
    assert sent_msg["Subject"] == "Re: CCMS Daily Wattage Report"
    assert sent_msg["In-Reply-To"] == "<orig@mail.gmail.com>"
    assert sent_msg["References"] == "<orig@mail.gmail.com>"
    assert sent_msg["Message-ID"] == msg_id
    assert meta["subject"] == "Re: CCMS Daily Wattage Report"
    assert msg_id in meta["references"]

    attachments = list(sent_msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Ward_Wise_CCMS_Report_2026-11-26.xlsx"


def test_pipeline_smtp_chains_thread_day_over_day_via_send_state(tmp_path: Path) -> None:
    cfg = _smtp_config(tmp_path, smtp_thread_subject="CCMS Daily Report")
    sheets_client = MagicMock(spec=GoogleSheetsReadOnlyClient)
    sheets_client.fetch_survey_data.return_value = [
        RawSurveyRow(2, "12", "2", "34W", "CORRECT", "", "CORRECT", "", "", "CCMS001")
    ]

    sent_messages = []

    def _fake_smtp_factory(host: str, port: int) -> MagicMock:
        srv = MagicMock()
        srv.__enter__.return_value = srv
        srv.send_message.side_effect = lambda msg: sent_messages.append(msg)
        return srv

    smtp_client = SMTPThreadClient(cfg, smtp_factory=_fake_smtp_factory)

    with patch("services.smtp_service.fetch_thread_headers_via_imap", return_value=None):
        assert run_pipeline(cfg, report_date="2026-11-26", sheets_client=sheets_client, smtp_client=smtp_client) == 0
        assert run_pipeline(cfg, report_date="2026-11-27", sheets_client=sheets_client, smtp_client=smtp_client) == 0

    assert len(sent_messages) == 2
    day1_msg, day2_msg = sent_messages
    assert day2_msg["In-Reply-To"] == day1_msg["Message-ID"]
    assert day1_msg["Message-ID"] in day2_msg["References"]

    saved_state = json.loads(cfg.send_state_file.read_text(encoding="utf-8"))
    assert saved_state["2026-11-27"]["status"] == "sent"
