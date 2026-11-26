"""Gmail API service for sending daily reports as replies in an existing thread."""

from __future__ import annotations

import base64
import logging
import socket
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import AppConfig, GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE
from services.google_auth import AuthenticationError, get_oauth_credentials

logger = logging.getLogger("ccms_automation")

GMAIL_SCOPES = [GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE]


class GmailServiceError(RuntimeError):
    """Raised when Gmail thread lookup, MIME building, or delivery fails."""


@dataclass(frozen=True)
class ThreadReplyContext:
    """Resolved headers and recipients for replying to an existing Gmail thread."""

    thread_id: str
    sender_email: str
    to_recipients: list[str]
    cc_recipients: list[str]
    thread_subject: str
    in_reply_to: str
    references: str


def _header_map(message: dict[str, Any]) -> dict[str, str]:
    """Build a case-insensitive header dictionary from a Gmail message payload."""
    headers = message.get("payload", {}).get("headers", [])
    result: dict[str, str] = {}
    for entry in headers:
        name = entry.get("name")
        value = entry.get("value")
        if name and value is not None:
            result[name.lower()] = str(value).strip()
    return result


def _parse_email_addresses(raw_headers: list[str]) -> list[tuple[str, str]]:
    """Parse RFC-2822 address headers into deduplicated (display_name, email_addr) pairs."""
    parsed = getaddresses([h for h in raw_headers if h])
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for display_name, email_addr in parsed:
        addr = email_addr.strip()
        if not addr:
            continue
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            unique.append((display_name.strip(), addr))
    return unique


def _format_addresses(pairs: list[tuple[str, str]]) -> list[str]:
    """Format (display_name, email_addr) pairs cleanly."""
    formatted: list[str] = []
    for name, addr in pairs:
        formatted.append(formataddr((name, addr)) if name else addr)
    return formatted


def _merge_references(existing_references: str, last_message_id: str) -> str:
    """Merge existing References header with the parent Message-ID without duplicates."""
    tokens = existing_references.split() if existing_references else []
    if last_message_id and last_message_id not in tokens:
        tokens.append(last_message_id)
    return " ".join(tokens)


def _execute_gmail_request(request: Any, operation: str, max_attempts: int = 3) -> Any:
    """Execute a Gmail API request with transient retry handling."""
    delay_seconds = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in {401, 403}:
                raise AuthenticationError(
                    f"Gmail API authorization failed during {operation} (HTTP {status}): {exc}"
                ) from exc
            if status == 404:
                raise GmailServiceError(
                    f"Gmail resource not found during {operation} (HTTP 404): {exc}"
                ) from exc
            if status in {429, 500, 502, 503, 504} and attempt < max_attempts:
                logger.warning(
                    "Transient Gmail HTTP %s during %s (attempt %d/%d); retrying in %.1fs",
                    status,
                    operation,
                    attempt,
                    max_attempts,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2
                continue
            raise GmailServiceError(f"Gmail API error during {operation}: {exc}") from exc
        except (OSError, TimeoutError, socket.timeout) as exc:
            if attempt < max_attempts:
                logger.warning(
                    "Network error during Gmail %s (%s, attempt %d/%d); retrying in %.1fs",
                    operation,
                    exc,
                    attempt,
                    max_attempts,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2
                continue
            raise GmailServiceError(
                f"Network failure during Gmail {operation} after {max_attempts} attempts: {exc}"
            ) from exc


def resolve_thread_context(
    service: Any,
    thread_id: str,
    configured_to: list[str] | None = None,
    configured_cc: list[str] | None = None,
) -> ThreadReplyContext:
    """Retrieve an existing Gmail thread and derive reply recipients and threading headers."""
    if not thread_id:
        raise GmailServiceError("GMAIL_THREAD_ID is not configured.")

    profile = _execute_gmail_request(
        service.users().getProfile(userId="me"),
        operation="profile lookup",
    )
    sender_email = str(profile.get("emailAddress", "")).strip()
    if not sender_email:
        raise GmailServiceError("Unable to determine authenticated Gmail account address.")

    thread = _execute_gmail_request(
        service.users().threads().get(
            userId="me",
            id=thread_id,
            format="metadata",
            metadataHeaders=["Subject", "From", "To", "Cc", "Reply-To", "Message-ID", "References"],
        ),
        operation=f"retrieving thread '{thread_id}'",
    )

    messages = thread.get("messages", [])
    if not messages:
        raise GmailServiceError(f"Gmail thread '{thread_id}' contains no messages.")

    first_headers = _header_map(messages[0])
    last_headers = _header_map(messages[-1])

    thread_subject = last_headers.get("subject") or first_headers.get("subject") or ""
    last_message_id = last_headers.get("message-id", "")
    existing_refs = last_headers.get("references", "")
    references = _merge_references(existing_refs, last_message_id)

    my_lower = sender_email.lower()

    if configured_to:
        to_recipients = list(configured_to)
    else:
        # Derive reply recipients from the thread while avoiding replying solely to ourselves
        # unless this is a self-addressed thread.
        candidate_headers: list[str] = []
        reply_target = last_headers.get("reply-to") or last_headers.get("from", "")
        if reply_target:
            candidate_headers.append(reply_target)
        if last_headers.get("to"):
            candidate_headers.append(last_headers["to"])

        parsed_candidates = _parse_email_addresses(candidate_headers)
        external_candidates = [(name, addr) for name, addr in parsed_candidates if addr.lower() != my_lower]

        if not external_candidates:
            # Scan earlier messages in the thread if the latest message only listed ourselves.
            earlier_headers: list[str] = []
            for msg in reversed(messages[:-1]):
                hdrs = _header_map(msg)
                for key in ("reply-to", "from", "to"):
                    if hdrs.get(key):
                        earlier_headers.append(hdrs[key])
            earlier_parsed = _parse_email_addresses(earlier_headers)
            external_candidates = [(name, addr) for name, addr in earlier_parsed if addr.lower() != my_lower]

        if external_candidates:
            to_recipients = _format_addresses(external_candidates)
        else:
            to_recipients = [sender_email]

    to_lower_set = {addr.lower() for _, addr in _parse_email_addresses(to_recipients)}

    if configured_cc:
        cc_recipients = [
            addr for addr in configured_cc if addr.lower() not in to_lower_set
        ]
    else:
        raw_cc = [last_headers.get("cc", "")]
        parsed_cc = _parse_email_addresses(raw_cc)
        cc_filtered = [
            (name, addr)
            for name, addr in parsed_cc
            if addr.lower() != my_lower and addr.lower() not in to_lower_set
        ]
        cc_recipients = _format_addresses(cc_filtered)

    return ThreadReplyContext(
        thread_id=thread_id,
        sender_email=sender_email,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
        thread_subject=thread_subject,
        in_reply_to=last_message_id,
        references=references,
    )


def build_reply_message(
    context: ThreadReplyContext,
    subject: str,
    body: str,
    attachment_path: Path,
) -> EmailMessage:
    """Build an RFC-2822 MIME EmailMessage with threading headers and Excel attachment."""
    if not attachment_path.exists() or not attachment_path.is_file():
        raise GmailServiceError(f"Attachment file not found: '{attachment_path}'")

    file_bytes = attachment_path.read_bytes()
    if not file_bytes:
        raise GmailServiceError(f"Attachment file is empty: '{attachment_path}'")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = context.sender_email
    msg["To"] = ", ".join(context.to_recipients)
    if context.cc_recipients:
        msg["Cc"] = ", ".join(context.cc_recipients)
    if context.in_reply_to:
        msg["In-Reply-To"] = context.in_reply_to
    if context.references:
        msg["References"] = context.references

    msg.set_content(body)
    msg.add_attachment(
        file_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=attachment_path.name,
    )
    return msg


class GmailThreadClient:
    """Gmail API client that sends daily reports inside an existing conversation thread."""

    SCOPES = GMAIL_SCOPES

    def __init__(self, config: AppConfig, service: Any | None = None) -> None:
        self._config = config
        self._service = service

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        creds = get_oauth_credentials(
            credentials_file=self._config.credentials_file,
            token_file=self._config.token_file,
            required_scopes=GMAIL_SCOPES,
            allow_interactive=False,
        )
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def list_recent_threads(self, query: str = "", max_results: int = 10) -> list[dict[str, str]]:
        """List recent Gmail threads (read-only) to help locate GMAIL_THREAD_ID."""
        service = self._get_service()
        list_kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_results}
        if query:
            list_kwargs["q"] = query

        listed = _execute_gmail_request(
            service.users().threads().list(**list_kwargs),
            operation="listing Gmail threads",
        )
        summaries: list[dict[str, str]] = []
        for item in listed.get("threads", []):
            tid = str(item.get("id", ""))
            if not tid:
                continue
            detail = _execute_gmail_request(
                service.users().threads().get(
                    userId="me",
                    id=tid,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ),
                operation=f"inspecting thread '{tid}'",
            )
            msgs = detail.get("messages", [])
            hdrs = _header_map(msgs[-1]) if msgs else {}
            summaries.append(
                {
                    "thread_id": tid,
                    "subject": hdrs.get("subject", "(no subject)"),
                    "from": hdrs.get("from", ""),
                    "date": hdrs.get("date", ""),
                }
            )
        return summaries

    def send_report_in_thread(self, attachment_path: Path, report_date: str) -> str:
        """Send the generated Excel report as a reply in the configured Gmail thread."""
        service = self._get_service()
        context = resolve_thread_context(
            service=service,
            thread_id=self._config.gmail_thread_id,
            configured_to=self._config.email_to,
            configured_cc=self._config.email_cc,
        )

        subject = self._config.email_subject_template.format(
            date=report_date,
            thread_subject=context.thread_subject,
        )
        body = self._config.email_body_template.format(
            date=report_date,
            thread_subject=context.thread_subject,
        )

        mime_message = build_reply_message(
            context=context,
            subject=subject,
            body=body,
            attachment_path=attachment_path,
        )

        raw_encoded = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("ascii")
        response = _execute_gmail_request(
            service.users().messages().send(
                userId="me",
                body={
                    "raw": raw_encoded,
                    "threadId": context.thread_id,
                },
            ),
            operation=f"sending report reply to thread '{context.thread_id}'",
            max_attempts=1,  # Avoid blind send retries that could risk duplicate emails.
        )

        message_id = str(response.get("id", ""))
        logger.info(
            "Sent report '%s' to Gmail thread '%s' (message_id=%s, to=%s).",
            attachment_path.name,
            context.thread_id,
            message_id,
            context.to_recipients,
        )
        return message_id
