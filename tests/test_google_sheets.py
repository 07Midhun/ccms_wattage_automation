"""Tests for strict read-only Google Sheets extraction and security checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config import AppConfig, SHEETS_READONLY_SCOPE, normalize_spreadsheet_id
from services.google_auth import AuthenticationError, _validate_no_sheets_write_scopes
from services.google_sheets import (
    EmptySpreadsheetError,
    GoogleSheetsReadOnlyClient,
    MissingRequiredColumnsError,
    MissingWorksheetError,
    REQUIRED_COLUMNS,
    parse_sheet_values,
)
from sheet_helpers import make_header_row, make_sheet_row


def test_prohibited_sheets_write_scopes_rejected() -> None:
    _validate_no_sheets_write_scopes([SHEETS_READONLY_SCOPE])

    for forbidden in (
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
    ):
        with pytest.raises(AuthenticationError, match="Prohibited write scope"):
            _validate_no_sheets_write_scopes([forbidden])


def test_parse_sheet_values_preserves_ccms_string_and_skips_blank_rows() -> None:
    values = [
        make_header_row(),
        make_sheet_row(
            ward="12",
            existing_arm="2",
            install_wattage="34W",
            arm_status="CORRECT",
            actual_arm="99",
            survey_status="CORRECT",
            ccms_id="00123",
        ),
        [""] * 27,  # blank separator row
        make_sheet_row(
            ward="13",
            survey_status="WRONG",
            actual_w1="45W",
            actual_w2="60W",
            ccms_id="CCMS-009",
        ),
    ]

    rows = parse_sheet_values(values)
    assert len(rows) == 2
    assert rows[0].row_number == 2
    assert rows[0].ccms_id == "00123"
    assert isinstance(rows[0].ccms_id, str)
    assert rows[1].row_number == 4
    assert rows[1].ccms_id == "CCMS-009"


def test_parse_sheet_values_missing_required_columns_raises() -> None:
    truncated_header = make_header_row()[:26]  # Drops column AA (CCMS ID)
    with pytest.raises(MissingRequiredColumnsError, match="AA \\(CCMS ID\\)"):
        parse_sheet_values([truncated_header, [""] * 26])


def test_parse_sheet_values_empty_sheet_raises() -> None:
    with pytest.raises(EmptySpreadsheetError):
        parse_sheet_values([])

    with pytest.raises(EmptySpreadsheetError):
        parse_sheet_values([make_header_row()])

    with pytest.raises(EmptySpreadsheetError):
        parse_sheet_values([make_header_row(), [""] * 27])


def test_fetch_survey_data_reads_only_appsheet_survey_tab(tmp_path: Path) -> None:
    mock_service = MagicMock()
    mock_spreadsheets = mock_service.spreadsheets.return_value

    # Simulate a 6-tab spreadsheet; only AppSheet_Survey should be read.
    mock_spreadsheets.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"title": "Summary"}},
            {"properties": {"title": "AppSheet_Survey"}},
            {"properties": {"title": "Ward_Master"}},
            {"properties": {"title": "Backup"}},
            {"properties": {"title": "Pivot"}},
            {"properties": {"title": "Logs"}},
        ]
    }
    mock_spreadsheets.values.return_value.get.return_value.execute.return_value = {
        "values": [
            make_header_row(),
            make_sheet_row(
                ward="12",
                existing_arm="1",
                install_wattage="34W",
                arm_status="CORRECT",
                survey_status="CORRECT",
                ccms_id="CCMS001",
            ),
        ]
    }

    cfg = AppConfig(spreadsheet_id="sheet-123", reports_dir=tmp_path, logs_dir=tmp_path)
    client = GoogleSheetsReadOnlyClient(cfg, service=mock_service)
    rows = client.fetch_survey_data()

    assert len(rows) == 1
    assert rows[0].ccms_id == "CCMS001"

    # Verify only read calls occurred and only on AppSheet_Survey
    mock_spreadsheets.values.return_value.get.assert_called_once_with(
        spreadsheetId="sheet-123",
        range="'AppSheet_Survey'!A:AA",
        valueRenderOption="FORMATTED_VALUE",
    )
    mock_spreadsheets.values.return_value.update.assert_not_called()
    mock_spreadsheets.values.return_value.append.assert_not_called()
    mock_spreadsheets.values.return_value.clear.assert_not_called()
    mock_spreadsheets.batchUpdate.assert_not_called()


def test_fetch_survey_data_missing_tab_raises(tmp_path: Path) -> None:
    mock_service = MagicMock()
    mock_service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "OtherTab"}}]
    }
    cfg = AppConfig(spreadsheet_id="sheet-123", reports_dir=tmp_path, logs_dir=tmp_path)
    client = GoogleSheetsReadOnlyClient(cfg, service=mock_service)

    with pytest.raises(MissingWorksheetError, match="AppSheet_Survey"):
        client.fetch_survey_data()


def test_fixed_column_indices_match_spec() -> None:
    assert REQUIRED_COLUMNS["ward_number"][1] == 1       # B
    assert REQUIRED_COLUMNS["existing_arm_count"][1] == 2  # C
    assert REQUIRED_COLUMNS["install_lamp_wattage"][1] == 6  # G
    assert REQUIRED_COLUMNS["arm_count_status"][1] == 13  # N
    assert REQUIRED_COLUMNS["actual_arm_count"][1] == 14  # O
    assert REQUIRED_COLUMNS["survey_lamp_wattage_status"][1] == 18  # S
    assert REQUIRED_COLUMNS["actual_lamp_wattage_1"][1] == 19  # T
    assert REQUIRED_COLUMNS["actual_lamp_wattage_2"][1] == 20  # U
    assert REQUIRED_COLUMNS["now_lamp_wattage"][1] == 24       # Y
    assert REQUIRED_COLUMNS["ccms_id"][1] == 26                # AA


def test_normalize_spreadsheet_id_strips_url_and_edit_suffix() -> None:
    expected = "1X0Df1G43KbG4F1NVd2QJzZEIj8jQQOfUME8S_2rFIQU"
    assert normalize_spreadsheet_id(expected) == expected
    assert normalize_spreadsheet_id(f"{expected}/edit?gid=1686278980#gid=1686278980") == expected
    assert normalize_spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{expected}/edit#gid=0") == expected
