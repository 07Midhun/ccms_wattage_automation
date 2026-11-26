"""SMTP + IMAP email delivery service maintaining an existing Gmail thread."""

from __future__ import annotations

import imaplib
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid
from pathlib import Path
from typing import Any

from config import AppConfig

logger = logging.getLogger("ccms_automation")

_PLACEHOLDER_THREAD_VALUES = {"", "your_existing_gmail_thread_id_here"}


class SMTPServiceError(RuntimeError):
    """Raised when SMTP/IMAP connection, thread resolution, or sending fails."""


@dataclass(frozen=True)
class SMTPThreadContext:
    """Resolved threading metadata for sending a reply over SMTP."""

    subject: str
    in_reply_to: str
    references: str
    thread_id: str = ""


def _merge_references(existing_refs: str, parent_message_id: str) -> str:
    """Append parent Message-ID to References without duplicates."""
    tokens = existing_refs.split() if existing_refs else []
    if parent_message_id and parent_message_id not in tokens:
        tokens.append(parent_message_id)
    return " ".join(tokens)


def _normalize_reply_subject(subject: str) -> str:
    """Ensure reply subject keeps the exact thread subject (with 'Re:' prefix)."""
    cleaned = subject.strip()
    if not cleaned:
        return cleaned
    if re.match(r"^re\s*:", cleaned, flags=re.IGNORECASE):
        return cleaned
    return f"Re: {cleaned}"


def _parse_gmail_thread_id_decimal(raw_thread_id: str) -> str | None:
    """Convert Gmail hex or decimal thread ID into the decimal X-GM-THRID string."""
    cleaned = raw_thread_id.strip()
    if not cleaned or cleaned in _PLACEHOLDER_THREAD_VALUES:
        return None
    if cleaned.isdigit():
        return cleaned
    if re.fullmatch(r"[0-9a-fA-F]{10,20}", cleaned):
        return str(int(cleaned, 16))
    return None


def _select_gmail_mailbox(imap: imaplib.IMAP4_SSL) -> None:
    """Select All Mail if available, otherwise fall back to INBOX."""
    for folder in ('"[Gmail]/All Mail"', "INBOX"):
        status, _ = imap.select(folder, readonly=True)
        if status == "OK":
            return
    raise SMTPServiceError("Unable to select '[Gmail]/All Mail' or 'INBOX' via IMAP.")


def _decode_header_bytes(raw_bytes: bytes) -> dict[str, str]:
    """Parse raw RFC-2822 header bytes into normalized lowercase header map."""
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    return {
        "subject": str(msg.get("Subject", "") or "").strip(),
        "message-id": str(msg.get("Message-ID", "") or "").strip(),
        "references": str(msg.get("References", "") or "").strip(),
        "from": str(msg.get("From", "") or "").strip(),
        "date": str(msg.get("Date", "") or "").strip(),
    }


def _extract_xgm_thrid(meta_bytes: bytes) -> str:
    """Extract X-GM-THRID numeric ID from an IMAP FETCH response tuple."""
    match = re.search(rb"X-GM-THRID\s+(\d+)", meta_bytes)
    return match.group(1).decode("ascii") if match else ""


def fetch_thread_headers_via_imap(
    config: AppConfig,
) -> SMTPThreadContext | None:
    """Look up the latest message headers in the target Gmail thread via IMAP."""
    thrid_dec = _parse_gmail_thread_id_decimal(config.gmail_thread_id)
    subject_query = config.smtp_thread_subject.strip()
    message_id_query = config.smtp_thread_message_id.strip()

    if not any([thrid_dec, subject_query, message_id_query]):
        return None

    if not config.smtp_username or not config.smtp_password:
        raise SMTPServiceError("SMTP_USERNAME and SMTP_PASSWORD are required for IMAP thread lookup.")

    logger.info(
        "Looking up existing thread via IMAP (%s:%s, thread_id=%r, subject=%r)...",
        config.imap_host,
        config.imap_port,
        config.gmail_thread_id,
        subject_query,
    )
    imap = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=20)
    try:
        imap.login(config.smtp_username, config.smtp_password)
        _select_gmail_mailbox(imap)

        uid_list: list[bytes] = []
        if thrid_dec:
            status, data = imap.uid("search", None, "X-GM-THRID", thrid_dec)
            if status == "OK" and data and data[0]:
                uid_list = data[0].split()
        elif message_id_query:
            escaped_mid = message_id_query.replace('"', "")
            status, data = imap.uid("search", None, "HEADER", "Message-ID", f'"{escaped_mid}"')
            if status == "OK" and data and data[0]:
                uid_list = data[0].split()
        elif subject_query:
            escaped_sub = subject_query.replace('"', "")
            # Use Gmail's indexed X-GM-RAW search first to avoid slow full-mailbox scans.
            try:
                status, data = imap.uid("search", None, "X-GM-RAW", f'subject:"{escaped_sub}"')
            except imaplib.IMAP4.error:
                since_str = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%d-%b-%Y")
                status, data = imap.uid("search", None, "SINCE", since_str, "SUBJECT", f'"{escaped_sub}"')
            if status == "OK" and data and data[0]:
                uid_list = data[0].split()

        if not uid_list:
            logger.warning(
                "No existing Gmail thread matched via IMAP (thread_id=%r, subject=%r, message_id=%r).",
                config.gmail_thread_id,
                subject_query,
                message_id_query,
            )
            return None

        latest_uid = uid_list[-1]
        status, fetch_data = imap.uid(
            "fetch",
            latest_uid,
            "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID REFERENCES)])",
        )
        if status != "OK" or not fetch_data:
            raise SMTPServiceError(f"Failed to fetch headers for IMAP UID {latest_uid!r}.")

        raw_headers = b""
        resolved_thrid = thrid_dec or ""
        for item in fetch_data:
            if isinstance(item, tuple):
                if not resolved_thrid:
                    resolved_thrid = _extract_xgm_thrid(item[0])
                raw_headers = item[1]
                break

        hdrs = _decode_header_bytes(raw_headers)
        parent_mid = hdrs.get("message-id", "")
        refs = _merge_references(hdrs.get("references", ""), parent_mid)
        base_subject = hdrs.get("subject", "") or subject_query
        reply_subject = _normalize_reply_subject(base_subject)

        logger.info(
            "Resolved existing thread via IMAP: X-GM-THRID=%s, subject=%r, parent_message_id=%s",
            resolved_thrid,
            reply_subject,
            parent_mid,
        )
        return SMTPThreadContext(
            subject=reply_subject,
            in_reply_to=parent_mid,
            references=refs,
            thread_id=resolved_thrid,
        )
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def list_imap_threads(
    config: AppConfig,
    query: str = "",
    max_results: int = 10,
) -> list[dict[str, str]]:
    """List recent Gmail threads via IMAP so the user can pick GMAIL_THREAD_ID / subject."""
    if not config.smtp_username or not config.smtp_password:
        raise SMTPServiceError("SMTP_USERNAME and SMTP_PASSWORD are required to list threads via IMAP.")

    imap = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=20)
    try:
        imap.login(config.smtp_username, config.smtp_password)
        _select_gmail_mailbox(imap)

        if query.strip():
            escaped_q = query.strip().replace('"', "")
            try:
                status, data = imap.uid("search", None, "X-GM-RAW", f'subject:"{escaped_q}"')
            except imaplib.IMAP4.error:
                since_str = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%d-%b-%Y")
                status, data = imap.uid("search", None, "SINCE", since_str, "SUBJECT", f'"{escaped_q}"')
        else:
            status, data = imap.uid("search", None, "ALL")

        if status != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()
        seen_threads: dict[str, dict[str, str]] = {}
        # Walk newest-first until we collect max_results unique threads.
        for uid in reversed(uids[-max(max_results * 5, 50):]):
            status, fetch_data = imap.uid(
                "fetch",
                uid,
                "(X-GM-THRID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])",
            )
            if status != "OK" or not fetch_data:
                continue
            raw_headers = b""
            thrid_dec = ""
            for item in fetch_data:
                if isinstance(item, tuple):
                    thrid_dec = _extract_xgm_thrid(item[0])
                    raw_headers = item[1]
                    break
            if not raw_headers:
                continue

            hdrs = _decode_header_bytes(raw_headers)
            thread_key = thrid_dec or hdrs.get("message-id") or uid.decode("ascii")
            if thread_key in seen_threads:
                continue

            thrid_hex = f"{int(thrid_dec):x}" if thrid_dec.isdigit() else ""
            seen_threads[thread_key] = {
                "thread_id": thrid_hex or thrid_dec,
                "thread_id_decimal": thrid_dec,
                "subject": hdrs.get("subject", "(no subject)"),
                "from": hdrs.get("from", ""),
                "date": hdrs.get("date", ""),
                "message_id": hdrs.get("message-id", ""),
            }
            if len(seen_threads) >= max_results:
                break

        return list(seen_threads.values())
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def resolve_smtp_thread_context(
    config: AppConfig,
    report_date: str,
    previous_thread_state: dict[str, str] | None = None,
) -> SMTPThreadContext:
    """Resolve Subject, In-Reply-To, and References for SMTP thread continuity."""
    # 1. Try live IMAP lookup against existing Gmail thread first (most reliable).
    try:
        imap_ctx = fetch_thread_headers_via_imap(config)
        if imap_ctx is not None and imap_ctx.in_reply_to:
            return imap_ctx
    except Exception as exc:
        if config.smtp_thread_message_id or (previous_thread_state and previous_thread_state.get("message_id")):
            logger.warning("IMAP thread lookup failed (%s); falling back to configured/cached Message-ID.", exc)
        else:
            raise SMTPServiceError(f"IMAP thread lookup failed: {exc}") from exc

    # 2. Fall back to explicit SMTP_THREAD_MESSAGE_ID or cached state from prior runs.
    prev = previous_thread_state or {}
    parent_mid = config.smtp_thread_message_id.strip() or prev.get("message_id", "").strip()
    prev_refs = prev.get("references", "").strip()
    references = _merge_references(prev_refs, parent_mid) if parent_mid else ""

    base_subject = (
        config.smtp_thread_subject.strip()
        or prev.get("subject", "").strip()
        or config.email_subject_template.format(date=report_date, thread_subject="")
    )
    subject = _normalize_reply_subject(base_subject) if parent_mid else base_subject

    return SMTPThreadContext(
        subject=subject,
        in_reply_to=parent_mid,
        references=references,
        thread_id=_parse_gmail_thread_id_decimal(config.gmail_thread_id) or prev.get("thread_id", ""),
    )


def build_smtp_reply_message(
    config: AppConfig,
    context: SMTPThreadContext,
    attachment_path: Path,
    report_date: str,
) -> EmailMessage:
    """Build an RFC-2822 EmailMessage with Message-ID, In-Reply-To, References, and Excel attachment."""
    if not attachment_path.exists() or not attachment_path.is_file():
        raise SMTPServiceError(f"Attachment file not found: '{attachment_path}'")

    file_bytes = attachment_path.read_bytes()
    if not file_bytes:
        raise SMTPServiceError(f"Attachment file is empty: '{attachment_path}'")

    recipients = config.recipient_emails or config.email_to
    if not recipients:
        raise SMTPServiceError("RECIPIENT_EMAILS (or EMAIL_TO) must be set when EMAIL_TRANSPORT=smtp.")

    sender = config.sender_email or config.smtp_username
    if not sender:
        raise SMTPServiceError("SENDER_EMAIL (or SMTP_USERNAME) must be configured when EMAIL_TRANSPORT=smtp.")

    domain = sender.split("@", 1)[1] if "@" in sender else "localhost"
    new_message_id = make_msgid(domain=domain)

    body = config.email_body_template.format(
        date=report_date,
        thread_subject=context.subject,
    )

    msg = EmailMessage()
    msg["Subject"] = context.subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    if config.email_cc:
        msg["Cc"] = ", ".join(config.email_cc)
    msg["Message-ID"] = new_message_id
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


class SMTPThreadClient:
    """SMTP + IMAP client that sends daily Excel reports inside an existing Gmail thread."""

    def __init__(self, config: AppConfig, smtp_factory: Any | None = None) -> None:
        self._config = config
        self._smtp_factory = smtp_factory

    def list_recent_threads(self, query: str = "", max_results: int = 10) -> list[dict[str, str]]:
        """List recent threads via IMAP."""
        return list_imap_threads(self._config, query=query, max_results=max_results)

    def send_report_in_thread(
        self,
        attachment_path: Path,
        report_date: str,
        previous_thread_state: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Send the Excel report over SMTP as a threaded reply.

        Returns (sent_message_id, thread_metadata_for_state).
        """
        if not self._config.smtp_username or not self._config.smtp_password:
            raise SMTPServiceError("SMTP_USERNAME and SMTP_PASSWORD are required when EMAIL_TRANSPORT=smtp.")

        context = resolve_smtp_thread_context(
            self._config,
            report_date=report_date,
            previous_thread_state=previous_thread_state,
        )
        msg = build_smtp_reply_message(
            config=self._config,
            context=context,
            attachment_path=attachment_path,
            report_date=report_date,
        )
        sent_message_id = str(msg["Message-ID"])
        logger.info(
            "Connecting to SMTP server %s:%s (tls=%s) to send '%s'...",
            self._config.smtp_host,
            self._config.smtp_port,
            self._config.smtp_use_tls,
            attachment_path.name,
        )

        try:
            if self._smtp_factory is not None:
                server = self._smtp_factory(self._config.smtp_host, self._config.smtp_port)
            elif not self._config.smtp_use_tls and self._config.smtp_port == 465:
                server = smtplib.SMTP_SSL(self._config.smtp_host, self._config.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(self._config.smtp_host, self._config.smtp_port, timeout=30)

            with server as smtp:
                if self._config.smtp_use_tls:
                    smtp.starttls()
                smtp.login(self._config.smtp_username, self._config.smtp_password)
                smtp.send_message(msg)
        except Exception as exc:
            raise SMTPServiceError(f"SMTP delivery failed: {exc}") from exc

        updated_refs = _merge_references(context.references, sent_message_id)
        logger.info(
            "Sent report '%s' via SMTP (%s:%s) to=%s, subject=%r, message_id=%s, in_reply_to=%s.",
            attachment_path.name,
            self._config.smtp_host,
            self._config.smtp_port,
            msg["To"],
            context.subject,
            sent_message_id,
            context.in_reply_to or "<NEW_THREAD>",
        )
        return sent_message_id, {
            "subject": context.subject,
            "references": updated_refs,
            "thread_id": context.thread_id or self._config.gmail_thread_id,
        }
