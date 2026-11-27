"""Strictly read-only Google Sheets extractor for the AppSheet_Survey tab."""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import AppConfig, SHEETS_READONLY_SCOPE
from services.google_auth import AuthenticationError, get_sheets_readonly_credentials

logger = logging.getLogger("ccms_automation")


class GoogleSheetsError(RuntimeError):
    """Base error for Google Sheets extraction failures."""


class SpreadsheetNotFoundError(GoogleSheetsError):
    """Raised when the configured Google Spreadsheet cannot be found."""


class MissingWorksheetError(GoogleSheetsError):
    """Raised when the required AppSheet_Survey worksheet is missing."""


class MissingRequiredColumnsError(GoogleSheetsError):
    """Raised when required column positions are missing from the worksheet."""


class EmptySpreadsheetError(GoogleSheetsError):
    """Raised when the AppSheet_Survey tab has no data rows."""


def col_letter_to_index(col: str) -> int:
    """Convert an Excel/Sheets column letter (e.g. 'B', 'AA') to a 0-based index."""
    result = 0
    for ch in col.strip().upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"Invalid column letter: {col!r}")
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


# Fixed column positions required by specification (Section 3 + SP Number columns).
REQUIRED_COLUMNS: dict[str, tuple[str, int]] = {
    "pole_id": ("A", col_letter_to_index("A")),
    "ward_number": ("B", col_letter_to_index("B")),
    "existing_arm_count": ("C", col_letter_to_index("C")),
    "install_lamp_wattage": ("G", col_letter_to_index("G")),
    "arm_count_status": ("N", col_letter_to_index("N")),
    "actual_arm_count": ("O", col_letter_to_index("O")),
    "survey_lamp_wattage_status": ("S", col_letter_to_index("S")),
    "actual_lamp_wattage_1": ("T", col_letter_to_index("T")),
    "actual_lamp_wattage_2": ("U", col_letter_to_index("U")),
    "now_lamp_wattage": ("Y", col_letter_to_index("Y")),
    "ccms_id": ("AA", col_letter_to_index("AA")),
    "actual_sp_no": ("AM", col_letter_to_index("AM")),
}

MAX_REQUIRED_COL_INDEX = max(idx for _, idx in REQUIRED_COLUMNS.values())

FIELD_DISPLAY_NAMES: dict[str, str] = {
    "pole_id": "Pole ID",
    "ward_number": "Ward Number",
    "existing_arm_count": "Existing Arm Count",
    "install_lamp_wattage": "Install Lamp Wattage",
    "arm_count_status": "Arm Count Status",
    "actual_arm_count": "Actual Arm Count",
    "survey_lamp_wattage_status": "Survey Lamp Wattage Status",
    "actual_lamp_wattage_1": "Actual Lamp Wattage 1",
    "actual_lamp_wattage_2": "Actual Lamp Wattage 2",
    "now_lamp_wattage": "Now Lamp Wattage",
    "ccms_id": "CCMS ID",
    "actual_sp_no": "Actual SP No",
}


@dataclass(frozen=True)
class RawSurveyRow:
    """Single extracted row from the AppSheet_Survey tab."""

    row_number: int
    ward_number: str
    existing_arm_count: str
    install_lamp_wattage: str
    arm_count_status: str
    actual_arm_count: str
    survey_lamp_wattage_status: str
    actual_lamp_wattage_1: str
    actual_lamp_wattage_2: str
    ccms_id: str
    now_lamp_wattage: str = ""
    pole_id: str = ""
    actual_sp_no: str = ""


def _execute_with_retry(request: Any, operation: str, max_attempts: int = 3) -> Any:
    """Execute a read-only Google API request with transient network/5xx retries."""
    delay_seconds = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in {401, 403}:
                raise AuthenticationError(
                    f"Google Sheets authorization failed during {operation} (HTTP {status}): {exc}"
                ) from exc
            if status == 404:
                raise SpreadsheetNotFoundError(
                    f"Google Spreadsheet not found during {operation} (HTTP 404): {exc}"
                ) from exc
            if status in {429, 500, 502, 503, 504} and attempt < max_attempts:
                logger.warning(
                    "Transient Google Sheets HTTP %s during %s (attempt %d/%d); retrying in %.1fs",
                    status,
                    operation,
                    attempt,
                    max_attempts,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2
                continue
            raise GoogleSheetsError(f"Google Sheets API error during {operation}: {exc}") from exc
        except (OSError, TimeoutError, socket.timeout) as exc:
            if attempt < max_attempts:
                logger.warning(
                    "Network error during %s (%s, attempt %d/%d); retrying in %.1fs",
                    operation,
                    exc,
                    attempt,
                    max_attempts,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2
                continue
            raise GoogleSheetsError(
                f"Network failure during Google Sheets {operation} after {max_attempts} attempts: {exc}"
            ) from exc


def parse_sheet_values(values: list[list[Any]], tab_name: str = "AppSheet_Survey") -> list[RawSurveyRow]:
    """Validate header row and extract fixed-position columns from raw sheet values."""
    if not values:
        raise EmptySpreadsheetError(f"The '{tab_name}' worksheet is empty.")

    header_row = values[0]
    missing_cols: list[str] = []
    for field_key, (col_letter, col_idx) in REQUIRED_COLUMNS.items():
        header_val = str(header_row[col_idx]).strip() if col_idx < len(header_row) and header_row[col_idx] is not None else ""
        if not header_val:
            missing_cols.append(f"{col_letter} ({FIELD_DISPLAY_NAMES[field_key]})")

    if missing_cols:
        raise MissingRequiredColumnsError(
            f"Missing required column header(s) in '{tab_name}': {', '.join(missing_cols)}"
        )

    if len(values) <= 1:
        raise EmptySpreadsheetError(
            f"The '{tab_name}' worksheet contains headers only and no data rows."
        )

    extracted: list[RawSurveyRow] = []
    for idx, row in enumerate(values[1:], start=2):
        # Skip completely blank rows (all cells empty/whitespace).
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        def _cell(col_index: int) -> str:
            if col_index >= len(row) or row[col_index] is None:
                return ""
            return str(row[col_index]).strip()

        extracted.append(
            RawSurveyRow(
                row_number=idx,
                ward_number=_cell(REQUIRED_COLUMNS["ward_number"][1]),
                existing_arm_count=_cell(REQUIRED_COLUMNS["existing_arm_count"][1]),
                install_lamp_wattage=_cell(REQUIRED_COLUMNS["install_lamp_wattage"][1]),
                arm_count_status=_cell(REQUIRED_COLUMNS["arm_count_status"][1]),
                actual_arm_count=_cell(REQUIRED_COLUMNS["actual_arm_count"][1]),
                survey_lamp_wattage_status=_cell(REQUIRED_COLUMNS["survey_lamp_wattage_status"][1]),
                actual_lamp_wattage_1=_cell(REQUIRED_COLUMNS["actual_lamp_wattage_1"][1]),
                actual_lamp_wattage_2=_cell(REQUIRED_COLUMNS["actual_lamp_wattage_2"][1]),
                now_lamp_wattage=_cell(REQUIRED_COLUMNS["now_lamp_wattage"][1]),
                ccms_id=_cell(REQUIRED_COLUMNS["ccms_id"][1]),
                pole_id=_cell(REQUIRED_COLUMNS["pole_id"][1]),
                actual_sp_no=_cell(REQUIRED_COLUMNS["actual_sp_no"][1]),
            )
        )

    if not extracted:
        raise EmptySpreadsheetError(
            f"The '{tab_name}' worksheet has no non-blank data rows."
        )

    return extracted


class GoogleSheetsReadOnlyClient:
    """Strictly read-only Google Sheets client for retrieving AppSheet_Survey data."""

    READ_ONLY_SCOPE = SHEETS_READONLY_SCOPE

    def __init__(self, config: AppConfig, service: Any | None = None) -> None:
        self._config = config
        self._service = service

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        creds = get_sheets_readonly_credentials(
            credentials_file=self._config.credentials_file,
            token_file=self._config.token_file,
            service_account_file=self._config.sheets_service_account_file,
            allow_interactive=False,
        )
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    def fetch_survey_data(self) -> list[RawSurveyRow]:
        """Fetch raw rows exclusively from the configured AppSheet_Survey tab."""
        spreadsheet_id = self._config.spreadsheet_id
        tab_name = self._config.sheet_tab_name

        if not spreadsheet_id:
            raise GoogleSheetsError("GOOGLE_SPREADSHEET_ID is not configured.")

        service = self._get_service()

        logger.info(
            "Connecting to Google Sheets (read-only) for spreadsheet=%s, target_tab=%s",
            spreadsheet_id,
            tab_name,
        )

        # Read-only metadata check to confirm the exact target tab exists.
        metadata = _execute_with_retry(
            service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties(title))",
            ),
            operation="spreadsheet metadata lookup",
        )
        sheet_titles = [
            sheet.get("properties", {}).get("title", "")
            for sheet in metadata.get("sheets", [])
        ]
        if tab_name not in sheet_titles:
            raise MissingWorksheetError(
                f"Required tab '{tab_name}' not found in spreadsheet '{spreadsheet_id}'. "
                f"Available tabs: {sheet_titles}"
            )

        # Read strictly from AppSheet_Survey columns A:AM using FORMATTED_VALUE to preserve string CCMS IDs.
        escaped_tab = tab_name.replace("'", "''")
        range_name = f"'{escaped_tab}'!A:AM"
        response = _execute_with_retry(
            service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueRenderOption="FORMATTED_VALUE",
            ),
            operation=f"reading range {range_name}",
        )

        values = response.get("values", [])
        rows = parse_sheet_values(values, tab_name=tab_name)
        logger.info("Extracted %d non-blank rows exclusively from '%s'.", len(rows), tab_name)
        return rows
