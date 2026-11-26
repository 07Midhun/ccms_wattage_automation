"""Configuration and logging setup for CCMS Wattage Automation."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# Strictly read-only scope for Google Sheets API — never request write permissions.
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

DEFAULT_OAUTH_SCOPES = [
    SHEETS_READONLY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
]

DEFAULT_EMAIL_BODY = (
    "Please find attached the latest Ward-wise CCMS Wattage Report generated from the "
    "AppSheet_Survey Google Sheet.\n\n"
    "The report contains consolidated CCMS-wise lamp counts for every ward, including "
    "dynamically detected wattage categories and total wattage calculations.\n\n"
    "Regards."
)


def _resolve_path(raw_path: str, base_dir: Path = PROJECT_ROOT) -> Path:
    """Resolve a relative path against project root or return an absolute path."""
    path = Path(raw_path)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _decode_multiline(raw_value: str) -> str:
    """Convert escaped newlines (\\n) from .env strings into actual newlines."""
    return raw_value.replace("\\n", "\n")


def _parse_bool(raw_value: str, default: bool = False) -> bool:
    """Parse boolean strings from environment variables."""
    cleaned = raw_value.strip().lower()
    if not cleaned:
        return default
    return cleaned in {"1", "true", "yes", "on"}


def _parse_csv_emails(raw_value: str) -> list[str]:
    """Parse comma-separated email addresses, ignoring 'auto'."""
    cleaned = raw_value.strip()
    if not cleaned or cleaned.lower() == "auto":
        return []
    return [addr.strip() for addr in cleaned.split(",") if addr.strip()]


def normalize_spreadsheet_id(raw_value: str) -> str:
    """Extract the bare Google Spreadsheet ID if a full URL or '/edit...' suffix was pasted."""
    cleaned = raw_value.strip()
    if not cleaned:
        return ""
    url_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", cleaned)
    if url_match:
        return url_match.group(1)
    # Handle '<id>/edit?gid=...' or '<id>#gid=...'
    return re.split(r"[/?#]", cleaned, maxsplit=1)[0].strip()


@dataclass(frozen=True)
class AppConfig:
    """Centralized configuration loaded from environment variables."""

    spreadsheet_id: str
    sheet_tab_name: str = "AppSheet_Survey"

    credentials_file: Path = field(default_factory=lambda: PROJECT_ROOT / "credentials" / "credentials.json")
    token_file: Path = field(default_factory=lambda: PROJECT_ROOT / "credentials" / "token.json")
    sheets_service_account_file: Path | None = None

    gmail_thread_id: str = ""
    email_transport: str = "gmail_api"  # "gmail_api" or "smtp"
    email_to: list[str] = field(default_factory=list)
    email_cc: list[str] = field(default_factory=list)
    email_subject_template: str = "Daily Ward-wise CCMS Wattage Report – {date}"
    email_body_template: str = DEFAULT_EMAIL_BODY

    # SMTP + IMAP thread configuration (used when email_transport == "smtp")
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    sender_email: str = ""
    recipient_emails: list[str] = field(default_factory=list)
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_thread_subject: str = ""
    smtp_thread_message_id: str = ""

    report_worksheet_name: str = "CCMS_Wattage_Report"
    report_filename_template: str = "Ward_Wise_CCMS_Report_{date}.xlsx"
    reports_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "reports")
    logs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    send_state_file: Path = field(default_factory=lambda: PROJECT_ROOT / "logs" / "send_state.json")

    log_level: str = "INFO"
    timezone: str = "Asia/Kolkata"
    cron_schedule: str = "0 9 * * *"

    @property
    def tz(self) -> ZoneInfo:
        """Return ZoneInfo for the configured timezone."""
        return ZoneInfo(self.timezone)

    def current_date_str(self) -> str:
        """Return today's date string (YYYY-MM-DD) in the configured timezone."""
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    def ensure_directories(self) -> None:
        """Ensure runtime directories exist."""
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.credentials_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.send_state_file.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, env_file: Path | str | None = None) -> "AppConfig":
        """Load configuration from environment variables and optional .env file."""
        dotenv_path = Path(env_file) if env_file else (PROJECT_ROOT / ".env")
        if dotenv_path.exists():
            load_dotenv(dotenv_path=dotenv_path, override=False)

        raw_sa = os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE", "").strip()
        sa_path = _resolve_path(raw_sa) if raw_sa else None

        email_to = _parse_csv_emails(os.getenv("EMAIL_TO", ""))
        recipient_emails = _parse_csv_emails(os.getenv("RECIPIENT_EMAILS", ""))
        if not recipient_emails and email_to:
            recipient_emails = list(email_to)
        if not email_to and recipient_emails:
            email_to = list(recipient_emails)

        email_cc = _parse_csv_emails(os.getenv("EMAIL_CC", ""))

        smtp_username = os.getenv("SMTP_USERNAME", "").strip()
        sender_email = os.getenv("SENDER_EMAIL", "").strip() or smtp_username
        email_transport = (os.getenv("EMAIL_TRANSPORT", "gmail_api").strip().lower() or "gmail_api")

        raw_smtp_port = os.getenv("SMTP_PORT", "587").strip()
        smtp_port = int(raw_smtp_port) if raw_smtp_port else 587

        raw_imap_port = os.getenv("IMAP_PORT", "993").strip()
        imap_port = int(raw_imap_port) if raw_imap_port else 993

        body_raw = os.getenv("EMAIL_BODY_TEMPLATE", "").strip()
        email_body = _decode_multiline(body_raw) if body_raw else DEFAULT_EMAIL_BODY

        return cls(
            spreadsheet_id=normalize_spreadsheet_id(os.getenv("GOOGLE_SPREADSHEET_ID", "")),
            sheet_tab_name=os.getenv("GOOGLE_SHEET_TAB_NAME", "AppSheet_Survey").strip() or "AppSheet_Survey",
            credentials_file=_resolve_path(
                os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials/credentials.json").strip()
                or "credentials/credentials.json"
            ),
            token_file=_resolve_path(
                os.getenv("GOOGLE_TOKEN_FILE", "credentials/token.json").strip()
                or "credentials/token.json"
            ),
            sheets_service_account_file=sa_path,
            gmail_thread_id=os.getenv("GMAIL_THREAD_ID", "").strip(),
            email_transport=email_transport,
            email_to=email_to,
            email_cc=email_cc,
            email_subject_template=(
                os.getenv("EMAIL_SUBJECT_TEMPLATE", "Daily Ward-wise CCMS Wattage Report – {date}").strip()
                or "Daily Ward-wise CCMS Wattage Report – {date}"
            ),
            email_body_template=email_body,
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
            smtp_port=smtp_port,
            smtp_use_tls=_parse_bool(os.getenv("SMTP_USE_TLS", "True"), default=True),
            smtp_username=smtp_username,
            smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
            sender_email=sender_email,
            recipient_emails=recipient_emails,
            imap_host=os.getenv("IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com",
            imap_port=imap_port,
            smtp_thread_subject=os.getenv("SMTP_THREAD_SUBJECT", "").strip(),
            smtp_thread_message_id=os.getenv("SMTP_THREAD_MESSAGE_ID", "").strip(),
            report_worksheet_name=(
                os.getenv("REPORT_WORKSHEET_NAME", "CCMS_Wattage_Report").strip()
                or "CCMS_Wattage_Report"
            ),
            report_filename_template=(
                os.getenv("REPORT_FILENAME_TEMPLATE", "Ward_Wise_CCMS_Report_{date}.xlsx").strip()
                or "Ward_Wise_CCMS_Report_{date}.xlsx"
            ),
            reports_dir=_resolve_path(os.getenv("REPORTS_DIR", "reports").strip() or "reports"),
            logs_dir=_resolve_path(os.getenv("LOGS_DIR", "logs").strip() or "logs"),
            send_state_file=_resolve_path(
                os.getenv("SEND_STATE_FILE", "logs/send_state.json").strip() or "logs/send_state.json"
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            timezone=os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata",
            cron_schedule=os.getenv("CRON_SCHEDULE", "0 9 * * *").strip() or "0 9 * * *",
        )


class _TimezoneFormatter(logging.Formatter):
    """Logging formatter that stamps records in the configured timezone."""

    def __init__(self, fmt: str, datefmt: str | None = None, tz_name: str = "Asia/Kolkata") -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._tz = ZoneInfo(tz_name)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self._tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def setup_logging(logs_dir: Path, log_level: str = "INFO", timezone: str = "Asia/Kolkata") -> tuple[logging.Logger, logging.Logger]:
    """Configure application and validation loggers with timezone-aware formatting."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    app_formatter = _TimezoneFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
        tz_name=timezone,
    )
    val_formatter = _TimezoneFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
        tz_name=timezone,
    )

    app_logger = logging.getLogger("ccms_automation")
    app_logger.setLevel(numeric_level)
    app_logger.handlers.clear()
    app_logger.propagate = False

    app_file_handler = RotatingFileHandler(
        logs_dir / "application.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    app_file_handler.setFormatter(app_formatter)
    app_logger.addHandler(app_file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(app_formatter)
    app_logger.addHandler(console_handler)

    validation_logger = logging.getLogger("ccms_validation")
    validation_logger.setLevel(logging.INFO)
    validation_logger.handlers.clear()
    validation_logger.propagate = False

    val_file_handler = RotatingFileHandler(
        logs_dir / "validation.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    val_file_handler.setFormatter(val_formatter)
    validation_logger.addHandler(val_file_handler)

    return app_logger, validation_logger
