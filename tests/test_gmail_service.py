"""Tests for Gmail thread resolution, MIME headers, attachments, and sending."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import MagicMock

from config import AppConfig
from services.gmail_service import GmailThreadClient, resolve_thread_context


def test_resolve_thread_context_from_external_and_self_messages() -> None:
    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "bot@example.com"
    }
    # Simulate yesterday's automated reply by bot@example.com as the last message in the thread.
    mock_service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "id": "thread-abc",
        "messages": [
            {
                "id": "msg-1",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "CCMS Daily Thread"},
                        {"name": "From", "value": "Manager <manager@example.com>"},
                        {"name": "To", "value": "bot@example.com, ops@example.com"},
                        {"name": "Cc", "value": "audit@example.com"},
                        {"name": "Message-ID", "value": "<msg1@mail.example.com>"},
                    ]
                },
            },
            {
                "id": "msg-2",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Daily Ward-wise CCMS Wattage Report – 2026-11-24"},
                        {"name": "From", "value": "bot@example.com"},
                        {"name": "To", "value": "Manager <manager@example.com>, ops@example.com"},
                        {"name": "Cc", "value": "audit@example.com"},
                        {"name": "Message-ID", "value": "<msg2@mail.example.com>"},
                        {"name": "References", "value": "<msg1@mail.example.com>"},
                    ]
                },
            },
        ],
    }

    ctx = resolve_thread_context(mock_service, "thread-abc")
    assert ctx.sender_email == "bot@example.com"
    assert ctx.to_recipients == ["Manager <manager@example.com>", "ops@example.com"]
    assert ctx.cc_recipients == ["audit@example.com"]
    assert ctx.in_reply_to == "<msg2@mail.example.com>"
    assert ctx.references == "<msg1@mail.example.com> <msg2@mail.example.com>"


def test_send_report_in_thread_sets_threading_headers_and_attachment(tmp_path: Path) -> None:
    report_path = tmp_path / "Ward_Wise_CCMS_Report_2026-11-25.xlsx"
    report_path.write_bytes(b"PK\x03\x04dummy-xlsx-content")

    mock_service = MagicMock()
    mock_service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "bot@example.com"
    }
    mock_service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "id": "thread-xyz",
        "messages": [
            {
                "id": "msg-10",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Initial Report Thread"},
                        {"name": "From", "value": "lead@example.com"},
                        {"name": "To", "value": "bot@example.com"},
                        {"name": "Message-ID", "value": "<msg10@mail.example.com>"},
                    ]
                },
            }
        ],
    }
    mock_service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "sent-msg-99"
    }

    cfg = AppConfig(
        spreadsheet_id="sheet-123",
        gmail_thread_id="thread-xyz",
        reports_dir=tmp_path,
        logs_dir=tmp_path,
    )
    client = GmailThreadClient(cfg, service=mock_service)
    msg_id = client.send_report_in_thread(report_path, "2026-11-25")

    assert msg_id == "sent-msg-99"
    send_call = mock_service.users.return_value.messages.return_value.send.call_args
    assert send_call.kwargs["userId"] == "me"
    body_payload = send_call.kwargs["body"]
    assert body_payload["threadId"] == "thread-xyz"

    raw_bytes = base64.urlsafe_b64decode(body_payload["raw"])
    parsed_msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    assert parsed_msg["To"] == "lead@example.com"
    assert parsed_msg["In-Reply-To"] == "<msg10@mail.example.com>"
    assert parsed_msg["References"] == "<msg10@mail.example.com>"
    assert "2026-11-25" in parsed_msg["Subject"]

    attachments = list(parsed_msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Ward_Wise_CCMS_Report_2026-11-25.xlsx"
    assert attachments[0].get_payload(decode=True) == b"PK\x03\x04dummy-xlsx-content"
