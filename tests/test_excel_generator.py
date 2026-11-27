"""Tests for OpenPyXL report generation, formatting, and validation."""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from config import AppConfig
from services.data_processor import process_survey_rows
from services.excel_generator import generate_excel_report
from services.google_sheets import RawSurveyRow


def test_generate_excel_report_formatting_and_values(tmp_path: Path) -> None:
    cfg = AppConfig(
        spreadsheet_id="sheet-123",
        reports_dir=tmp_path / "reports",
        logs_dir=tmp_path / "logs",
    )
    rows = [
        RawSurveyRow(2, "12", "2", "34W", "CORRECT", "", "CORRECT", "", "", "00123", pole_id="GBA-SNL-W12-P1-SP117"),
        RawSurveyRow(3, "12", "", "", "", "", "WRONG", "60W", "34W", "00123", pole_id="GBA-SNL-W12-P2-SP117"),
        RawSurveyRow(4, "13", "", "45W", "WRONG", "2", "CORRECT", "", "", "CCMS002", actual_sp_no="209"),
    ]
    processed = process_survey_rows(rows)
    output_path = generate_excel_report(processed, cfg, "2026-11-25")

    assert output_path.name == "Ward_Wise_CCMS_Report_2026-11-25.xlsx"
    assert output_path.exists()

    wb = load_workbook(output_path)
    assert "CCMS_Wattage_Report" in wb.sheetnames
    ws = wb["CCMS_Wattage_Report"]

    headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    assert headers == ["Ward No", "Switch Point (SP) Number", "CCMS ID", "34W", "45W", "60W", "Total SP", "Total Wattage"]

    # Formatting checks
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:H3"
    assert ws.cell(row=1, column=1).font.bold is True
    assert ws.cell(row=1, column=1).fill.fill_type == "solid"
    assert ws.cell(row=2, column=1).border.left.style == "thin"

    # Switch Point (SP) Number at col B
    assert ws.cell(row=2, column=2).value == 117
    assert ws.cell(row=3, column=2).value == 209

    # CCMS ID at col C preserved as string with leading zeros
    assert ws.cell(row=2, column=3).value == "00123"
    assert isinstance(ws.cell(row=2, column=3).value, str)
    assert ws.cell(row=2, column=3).number_format == "@"

    # Lamp counts, Total SP, and Total Wattage
    assert ws.cell(row=2, column=4).value == 3   # 34W count
    assert ws.cell(row=2, column=5).value == 0   # 45W count
    assert ws.cell(row=2, column=6).value == 1   # 60W count
    assert ws.cell(row=2, column=7).value == 1   # Total SP
    assert ws.cell(row=2, column=7).font.bold is True
    assert ws.cell(row=2, column=8).value == 162 # 3*34 + 1*60
    assert ws.cell(row=2, column=8).number_format == '#,##0"W"'

    assert ws.cell(row=3, column=1).value == 13
    assert ws.cell(row=3, column=3).value == "CCMS002"
    assert ws.cell(row=3, column=5).value == 2
    assert ws.cell(row=3, column=7).value == 1
    assert ws.cell(row=3, column=8).value == 90
    wb.close()
