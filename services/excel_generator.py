"""Excel report generation, formatting, and validation using OpenPyXL."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from config import AppConfig
from services.data_processor import ProcessedReportData

logger = logging.getLogger("ccms_automation")


class ExcelGenerationError(RuntimeError):
    """Raised when Excel generation or workbook validation fails."""


def _is_plain_integer_string(value: str) -> bool:
    """Return True for non-empty digits without leading zeros (except '0' itself)."""
    return value.isdigit() and (value == "0" or not value.startswith("0"))


def generate_excel_report(
    report_data: ProcessedReportData,
    config: AppConfig,
    report_date: str,
) -> Path:
    """Generate and format the consolidated Ward-wise CCMS Wattage Excel report."""
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    filename = config.report_filename_template.format(date=report_date)
    output_path = config.reports_dir / filename
    temp_path = config.reports_dir / f".{filename}.tmp"

    df = report_data.dataframe
    headers = list(df.columns)
    worksheet_name = config.report_worksheet_name[:31]  # Excel sheet title max length is 31 chars.

    try:
        wb = Workbook()
        ws = wb.active
        ws.title = worksheet_name

        header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        zebra_fill = PatternFill(start_color="F5F8FC", end_color="F5F8FC", fill_type="solid")
        data_font = Font(name="Calibri", size=11, color="111827")
        total_font = Font(name="Calibri", size=11, bold=True, color="111827")

        thin_side = Side(style="thin", color="D1D5DB")
        header_bottom_side = Side(style="medium", color="163A59")
        data_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
        header_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=header_bottom_side)

        # Write styled header row (row 1).
        ws.append(headers)
        ws.row_dimensions[1].height = 24
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.border = header_border
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        # Write consolidated rows starting at row 2.
        all_int_wattages = all(
            isinstance(w, int) or (isinstance(w, float) and w.is_integer())
            for w in report_data.wattage_values
        )
        total_wattage_fmt = '#,##0"W"' if all_int_wattages else '#,##0.##"W"'
        last_col_idx = len(headers)
        total_sp_col_idx = headers.index("Total SP") + 1 if "Total SP" in headers else None

        for row_offset, row_values in enumerate(df.itertuples(index=False, name=None), start=2):
            ws.row_dimensions[row_offset].height = 20
            use_zebra = (row_offset % 2) == 1

            for col_idx, raw_val in enumerate(row_values, start=1):
                cell = ws.cell(row=row_offset, column=col_idx)
                cell.border = data_border
                if use_zebra:
                    cell.fill = zebra_fill

                if col_idx == 1:
                    # Ward No: preserve leading-zero or alphanumeric wards as text; plain numbers as int.
                    ward_str = str(raw_val).strip()
                    if _is_plain_integer_string(ward_str):
                        cell.value = int(ward_str)
                        cell.number_format = "0"
                    else:
                        cell.value = ward_str
                        cell.number_format = "@"
                    cell.font = data_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif col_idx == 2:
                    # Switch Point (SP) Number: plain integer if single number, else text.
                    sp_str = "" if raw_val is None else str(raw_val).strip()
                    if _is_plain_integer_string(sp_str):
                        cell.value = int(sp_str)
                        cell.number_format = "0"
                    else:
                        cell.value = sp_str
                        cell.number_format = "@"
                    cell.font = data_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif col_idx == 3:
                    # CCMS ID: always preserve as string.
                    cell.value = str(raw_val)
                    cell.number_format = "@"
                    cell.font = data_font
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                elif total_sp_col_idx is not None and col_idx == total_sp_col_idx:
                    # Total SP: count of unique SP numbers in this row.
                    cell.value = int(raw_val)
                    cell.number_format = "#,##0"
                    cell.font = total_font
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif col_idx == last_col_idx:
                    # Total Wattage: numeric value formatted with 'W' suffix.
                    cell.value = int(raw_val) if all_int_wattages else float(raw_val)
                    cell.number_format = total_wattage_fmt
                    cell.font = total_font
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                else:
                    # Dynamic wattage category lamp count.
                    cell.value = int(raw_val)
                    cell.number_format = "#,##0"
                    cell.font = data_font
                    cell.alignment = Alignment(horizontal="right", vertical="center")

        # Freeze header row and enable AutoFilter across full table.
        ws.freeze_panes = "A2"
        last_row = len(df) + 1
        last_col_letter = get_column_letter(last_col_idx)
        ws.auto_filter.ref = f"A1:{last_col_letter}{last_row}"

        # Auto-fit column widths with generous padding for filter arrows and W suffix.
        for col_idx, header in enumerate(headers, start=1):
            max_len = len(str(header)) + 3  # space for AutoFilter dropdown
            for row_idx in range(2, last_row + 1):
                val = ws.cell(row=row_idx, column=col_idx).value
                display_len = len(f"{val}W") if col_idx == last_col_idx else len(str(val))
                if display_len > max_len:
                    max_len = display_len
            ws.column_dimensions[get_column_letter(col_idx)].width = max(max_len + 4, 12)

        wb.save(temp_path)
        os.replace(temp_path, output_path)
        logger.info("Generated Excel report at %s (%d consolidated rows).", output_path, len(df))
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise ExcelGenerationError(f"Failed to generate Excel report '{output_path}': {exc}") from exc

    validate_workbook(output_path, report_data, worksheet_name)
    return output_path


def validate_workbook(
    workbook_path: Path,
    expected_data: ProcessedReportData,
    worksheet_name: str,
) -> None:
    """Validate the generated Excel workbook structure, headers, rows, and totals."""
    if not workbook_path.exists() or workbook_path.stat().st_size == 0:
        raise ExcelGenerationError(f"Generated workbook '{workbook_path}' is missing or empty.")

    wb = None
    try:
        wb = load_workbook(workbook_path, read_only=True, data_only=True)
        if worksheet_name not in wb.sheetnames:
            raise ExcelGenerationError(
                f"Expected worksheet '{worksheet_name}' missing in '{workbook_path}'."
            )

        ws = wb[worksheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            actual_headers = [str(cell) if cell is not None else "" for cell in next(rows_iter)]
        except StopIteration as exc:
            raise ExcelGenerationError(f"Worksheet '{worksheet_name}' has no header row.") from exc

        expected_headers = list(expected_data.dataframe.columns)
        if actual_headers != expected_headers:
            raise ExcelGenerationError(
                f"Workbook header mismatch. Expected {expected_headers}, got {actual_headers}."
            )

        total_wattage_idx = expected_headers.index("Total Wattage")
        total_sp_idx = expected_headers.index("Total SP") if "Total SP" in expected_headers else None

        actual_row_count = 0
        actual_total_wattage = 0.0
        actual_total_sp = 0
        for row in rows_iter:
            actual_row_count += 1
            total_cell = row[total_wattage_idx]
            if total_cell is None:
                raise ExcelGenerationError(
                    f"Missing Total Wattage value on workbook row {actual_row_count + 1}."
                )
            actual_total_wattage += float(total_cell)
            if total_sp_idx is not None:
                sp_cell = row[total_sp_idx]
                if sp_cell is None:
                    raise ExcelGenerationError(
                        f"Missing Total SP value on workbook row {actual_row_count + 1}."
                    )
                actual_total_sp += int(sp_cell)

        if actual_row_count != expected_data.consolidated_rows:
            raise ExcelGenerationError(
                f"Workbook row count mismatch. Expected {expected_data.consolidated_rows}, got {actual_row_count}."
            )

        if abs(actual_total_wattage - float(expected_data.grand_total_wattage)) > 1e-6:
            raise ExcelGenerationError(
                f"Workbook Total Wattage sum mismatch. Expected {expected_data.grand_total_wattage}, "
                f"got {actual_total_wattage}."
            )

        if total_sp_idx is not None and actual_total_sp != expected_data.grand_total_sp:
            raise ExcelGenerationError(
                f"Workbook Total SP sum mismatch. Expected {expected_data.grand_total_sp}, "
                f"got {actual_total_sp}."
            )

        logger.info(
            "Validated workbook '%s': worksheet='%s', rows=%d, total_wattage=%sW.",
            workbook_path.name,
            worksheet_name,
            actual_row_count,
            expected_data.grand_total_wattage,
        )
    except ExcelGenerationError:
        raise
    except Exception as exc:
        raise ExcelGenerationError(f"Failed to validate Excel workbook '{workbook_path}': {exc}") from exc
    finally:
        if wb is not None:
            wb.close()
