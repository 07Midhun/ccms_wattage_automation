"""Google OAuth 2.0 and Service Account authentication helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import DEFAULT_OAUTH_SCOPES, SHEETS_READONLY_SCOPE

logger = logging.getLogger("ccms_automation")

# Write scopes that must NEVER be granted for Sheets operations.
PROHIBITED_SHEETS_WRITE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
    }
)


class AuthenticationError(RuntimeError):
    """Raised when Google API authentication fails or tokens are missing/invalid."""


def _validate_no_sheets_write_scopes(scopes: list[str] | set[str] | tuple[str, ...] | None) -> None:
    """Ensure no Sheets write scopes are present in the credentials."""
    if not scopes:
        return
    forbidden = PROHIBITED_SHEETS_WRITE_SCOPES.intersection(set(scopes))
    if forbidden:
        raise AuthenticationError(
            f"Prohibited write scope(s) detected: {sorted(forbidden)}. "
            f"Only '{SHEETS_READONLY_SCOPE}' is permitted for Google Sheets."
        )


def get_oauth_credentials(
    credentials_file: Path,
    token_file: Path,
    required_scopes: list[str],
    allow_interactive: bool = False,
) -> Credentials:
    """Load, refresh, or interactively obtain OAuth 2.0 credentials.

    Never requests Google Sheets write scopes.
    """
    _validate_no_sheets_write_scopes(required_scopes)
    creds: Credentials | None = None

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file))
        except Exception as exc:
            raise AuthenticationError(
                f"Failed to load OAuth token file '{token_file}': {exc}"
            ) from exc

        _validate_no_sheets_write_scopes(creds.scopes)

        if creds.scopes:
            missing = [scope for scope in required_scopes if scope not in creds.scopes]
            if missing:
                raise AuthenticationError(
                    f"Token '{token_file}' is missing required scope(s): {missing}. "
                    "Re-run OAuth authorization with 'python app.py --auth'."
                )

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(creds.to_json(), encoding="utf-8")
            logger.info("Refreshed Google OAuth access token and saved to %s", token_file)
            return creds
        except GoogleAuthError as exc:
            if not allow_interactive:
                raise AuthenticationError(
                    f"Failed to refresh Google OAuth token: {exc}. "
                    "Re-run interactive authorization with 'python app.py --auth'."
                ) from exc
            logger.warning("Token refresh failed (%s); falling back to interactive OAuth flow.", exc)

    if not allow_interactive:
        raise AuthenticationError(
            f"Valid OAuth token not found at '{token_file}'. "
            "Run 'python app.py --auth' once interactively to authorize Google Sheets (read-only) and Gmail."
        )

    if not credentials_file.exists():
        raise AuthenticationError(
            f"Google OAuth client secrets file not found at '{credentials_file}'. "
            "Download it from Google Cloud Console and place it at that path."
        )

    flow_scopes = list(dict.fromkeys([*DEFAULT_OAUTH_SCOPES, *required_scopes]))
    _validate_no_sheets_write_scopes(flow_scopes)

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), flow_scopes)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    except Exception as exc:
        raise AuthenticationError(f"Interactive OAuth authorization failed: {exc}") from exc

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Saved new Google OAuth token to %s", token_file)
    return creds


def get_sheets_readonly_credentials(
    credentials_file: Path,
    token_file: Path,
    service_account_file: Path | None = None,
    allow_interactive: bool = False,
) -> Credentials | ServiceAccountCredentials:
    """Return strictly read-only credentials for the Google Sheets API."""
    if service_account_file is not None:
        if not service_account_file.exists():
            raise AuthenticationError(
                f"Google Sheets service account file not found at '{service_account_file}'."
            )
        try:
            return ServiceAccountCredentials.from_service_account_file(
                str(service_account_file),
                scopes=[SHEETS_READONLY_SCOPE],
            )
        except Exception as exc:
            raise AuthenticationError(
                f"Failed to load Sheets service account credentials: {exc}"
            ) from exc

    return get_oauth_credentials(
        credentials_file=credentials_file,
        token_file=token_file,
        required_scopes=[SHEETS_READONLY_SCOPE],
        allow_interactive=allow_interactive,
    )
