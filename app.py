"""Main entrypoint for Automated Ward-wise CCMS Wattage Report & Daily Email Automation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from config import AppConfig, DEFAULT_OAUTH_SCOPES, setup_logging
from services.data_processor import process_survey_rows
from services.excel_generator import generate_excel_report
from services.gmail_service import GmailThreadClient
from services.google_auth import get_oauth_credentials
from services.google_sheets import GoogleSheetsReadOnlyClient
from services.smtp_service import SMTPThreadClient


def _load_send_state(state_file: Path) -> dict[str, dict[str, str]]:
    """Load persisted send state JSON for duplicate-send protection and retries."""
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_send_state(state_file: Path, state: dict[str, dict[str, str]]) -> None:
    """Persist send state JSON atomically."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = state_file.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp_file.replace(state_file)


def _is_already_sent(state: dict[str, dict[str, str]], report_date: str, thread_key: str) -> dict[str, str] | None:
    """Return existing sent entry if report_date + thread_key was already delivered."""
    entry = state.get(report_date)
    if not isinstance(entry, dict):
        return None
    if entry.get("status") == "sent" and entry.get("thread_id", "") == thread_key:
        return entry
    return None


def _latest_sent_thread_state(state: dict[str, dict[str, str]]) -> dict[str, str] | None:
    """Return the most recent sent state entry for SMTP day-over-day thread chaining."""
    sent_dates = sorted(
        (d for d, entry in state.items() if isinstance(entry, dict) and entry.get("status") == "sent"),
    )
    if not sent_dates:
        return None
    return state[sent_dates[-1]]


def run_pipeline(
    config: AppConfig,
    report_date: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    retry_email: bool = False,
    sheets_client: GoogleSheetsReadOnlyClient | None = None,
    gmail_client: GmailThreadClient | None = None,
    smtp_client: SMTPThreadClient | None = None,
) -> int:
    """Execute the end-to-end daily automation pipeline.

    Returns 0 on success (including skipped duplicate sends), 1 on failure.
    """
    config.ensure_directories()
    app_logger, _ = setup_logging(config.logs_dir, config.log_level, config.timezone)
    effective_date = report_date or config.current_date_str()
    transport = config.email_transport.lower()
    thread_key = (
        config.gmail_thread_id
        or config.smtp_thread_subject
        or config.smtp_thread_message_id
        or "smtp-default-thread"
    ) if transport == "smtp" else config.gmail_thread_id

    app_logger.info(
        "Starting CCMS Wattage Report pipeline for date=%s (timezone=%s, transport=%s, dry_run=%s, retry_email=%s, force=%s).",
        effective_date,
        config.timezone,
        transport,
        dry_run,
        retry_email,
        force,
    )

    state = _load_send_state(config.send_state_file)
    if not dry_run and not force:
        existing_sent = _is_already_sent(state, effective_date, thread_key)
        if existing_sent:
            app_logger.info(
                "Duplicate-send protection triggered: report for %s was already sent (thread=%r, "
                "message_id=%s, report=%s). Skipping execution.",
                effective_date,
                thread_key,
                existing_sent.get("message_id", ""),
                existing_sent.get("report_path", ""),
            )
            return 0

    report_path: Path | None = None

    if retry_email:
        prev_entry = state.get(effective_date, {})
        candidate_raw = prev_entry.get("report_path") or str(
            config.reports_dir / config.report_filename_template.format(date=effective_date)
        )
        candidate_path = Path(candidate_raw)
        if not candidate_path.exists():
            app_logger.error(
                "Cannot retry email for %s: retained report file '%s' not found.",
                effective_date,
                candidate_path,
            )
            return 1
        report_path = candidate_path
        app_logger.info("Retrying email delivery using retained report '%s'.", report_path)
    else:
        try:
            sheets = sheets_client or GoogleSheetsReadOnlyClient(config)
            raw_rows = sheets.fetch_survey_data()
            processed = process_survey_rows(raw_rows)
            report_path = generate_excel_report(processed, config, effective_date)
        except Exception as exc:
            app_logger.exception("Pipeline aborted before email delivery: %s", exc)
            return 1

    if dry_run:
        app_logger.info(
            "Dry-run completed successfully. Generated report at '%s'; skipping Gmail delivery.",
            report_path,
        )
        return 0

    thread_meta: dict[str, str] = {}
    try:
        if transport == "smtp":
            smtp = smtp_client or SMTPThreadClient(config)
            prev_thread = _latest_sent_thread_state(state)
            message_id, thread_meta = smtp.send_report_in_thread(
                report_path,
                effective_date,
                previous_thread_state=prev_thread,
            )
        else:
            gmail = gmail_client or GmailThreadClient(config)
            message_id = gmail.send_report_in_thread(report_path, effective_date)
    except Exception as exc:
        # Retain generated report and record failed email state for safe retry.
        state[effective_date] = {
            "status": "email_failed",
            "report_path": str(report_path),
            "thread_id": thread_key,
            "error": str(exc),
            "updated_at": datetime.now(config.tz).isoformat(timespec="seconds"),
        }
        _save_send_state(config.send_state_file, state)
        app_logger.exception(
            "Email delivery failed for '%s' (report retained for retry via --retry-email): %s",
            report_path,
            exc,
        )
        return 1

    state[effective_date] = {
        "status": "sent",
        "report_path": str(report_path),
        "thread_id": thread_key,
        "message_id": message_id,
        "subject": thread_meta.get("subject", ""),
        "references": thread_meta.get("references", ""),
        "updated_at": datetime.now(config.tz).isoformat(timespec="seconds"),
    }
    _save_send_state(config.send_state_file, state)
    app_logger.info(
        "Pipeline completed successfully for %s (transport=%s, report='%s', message_id='%s').",
        effective_date,
        transport,
        report_path,
        message_id,
    )
    return 0


def run_interactive_auth(config: AppConfig) -> int:
    """Run one-time interactive OAuth 2.0 authorization (read-only Sheets + Gmail)."""
    config.ensure_directories()
    app_logger, _ = setup_logging(config.logs_dir, config.log_level, config.timezone)
    try:
        get_oauth_credentials(
            credentials_file=config.credentials_file,
            token_file=config.token_file,
            required_scopes=DEFAULT_OAUTH_SCOPES,
            allow_interactive=True,
        )
        app_logger.info(
            "OAuth 2.0 setup succeeded. Token stored at '%s' with scopes %s.",
            config.token_file,
            DEFAULT_OAUTH_SCOPES,
        )
        return 0
    except Exception as exc:
        app_logger.exception("OAuth 2.0 setup failed: %s", exc)
        return 1


def run_list_threads(config: AppConfig, query: str = "") -> int:
    """List recent Gmail threads so the user can find the exact GMAIL_THREAD_ID."""
    config.ensure_directories()
    setup_logging(config.logs_dir, config.log_level, config.timezone)
    try:
        if config.email_transport.lower() == "smtp":
            client = SMTPThreadClient(config)
        else:
            client = GmailThreadClient(config)
        threads = client.list_recent_threads(query=query, max_results=10)
        if not threads:
            print("No Gmail threads found matching that query.")
            return 0
        print("\nRecent Gmail Threads (copy 'thread_id' into GMAIL_THREAD_ID in .env):")
        for entry in threads:
            extra = f" | message_id={entry['message_id']}" if entry.get("message_id") else ""
            print(
                f"  thread_id={entry['thread_id']} | subject={entry['subject']} | "
                f"from={entry['from']} | date={entry['date']}{extra}"
            )
        return 0
    except Exception as exc:
        print(f"Failed to list Gmail threads: {exc}", file=sys.stderr)
        return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI flags."""
    parser = argparse.ArgumentParser(
        description="Automated Ward-wise CCMS Wattage Report & Daily Gmail Thread Automation",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run interactive OAuth 2.0 authorization flow and store token.json.",
    )
    parser.add_argument(
        "--list-threads",
        action="store_true",
        help="List recent Gmail threads to find the exact GMAIL_THREAD_ID.",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Optional Gmail search query used with --list-threads (e.g. 'subject:CCMS').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract, process, and generate/validate Excel without sending Gmail.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass duplicate-send protection for the target report date.",
    )
    parser.add_argument(
        "--retry-email",
        action="store_true",
        help="Retry sending the already-generated report for the target date.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override report date (YYYY-MM-DD). Defaults to today in configured timezone.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env configuration file (defaults to project .env).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    config = AppConfig.from_env(args.env_file)

    if args.auth:
        return run_interactive_auth(config)

    if args.list_threads:
        return run_list_threads(config, query=args.query)

    return run_pipeline(
        config=config,
        report_date=args.date,
        dry_run=args.dry_run,
        force=args.force,
        retry_email=args.retry_email,
    )


if __name__ == "__main__":
    sys.exit(main())
