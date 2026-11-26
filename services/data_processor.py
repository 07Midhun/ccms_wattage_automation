"""Business logic, validation, dynamic wattage detection, and Pandas consolidation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import pandas as pd

from services.google_sheets import RawSurveyRow

app_logger = logging.getLogger("ccms_automation")
validation_logger = logging.getLogger("ccms_validation")

_WATTAGE_PATTERN = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*[wW]?\s*$")
_INT_PATTERN = re.compile(r"^\s*([+-]?\d+)(?:\.0+)?\s*$")
_NATURAL_SPLIT_PATTERN = re.compile(r"(\d+)")

WattageNumber = int | float


class DataProcessingError(RuntimeError):
    """Raised when survey data cannot be processed into a valid report."""


@dataclass(frozen=True)
class ValidationIssue:
    """Structured representation of a validation issue logged for a source row."""

    row_number: int
    ward_number: str
    ccms_id: str
    problem: str
    reason: str


@dataclass
class ProcessedReportData:
    """Consolidated report data ready for Excel generation."""

    dataframe: pd.DataFrame
    wattage_values: list[WattageNumber]
    wattage_columns: list[str]
    total_input_rows: int
    valid_rows: int
    skipped_rows: int
    consolidated_rows: int
    grand_total_wattage: WattageNumber
    validation_issues: list[ValidationIssue] = field(default_factory=list)


def parse_wattage(raw_value: str) -> WattageNumber | None:
    """Parse a wattage string (e.g. '34W', '60 w', '45') into an int or float.

    Returns None when the cell is blank. Raises ValueError for invalid or non-positive values.
    """
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if text == "":
        return None

    match = _WATTAGE_PATTERN.match(text)
    if not match:
        raise ValueError(f"Unrecognized wattage format: {raw_value!r}")

    numeric = float(match.group(1))
    if numeric <= 0:
        raise ValueError(f"Wattage must be greater than zero, got: {raw_value!r}")

    if numeric.is_integer():
        return int(numeric)
    return round(numeric, 2)


def parse_arm_count(raw_value: str) -> int:
    """Parse an arm count string into a non-negative integer."""
    if raw_value is None or str(raw_value).strip() == "":
        raise ValueError("Arm count is blank")

    text = str(raw_value).strip()
    match = _INT_PATTERN.match(text)
    if not match:
        raise ValueError(f"Invalid non-integer arm count: {raw_value!r}")

    count = int(match.group(1))
    if count < 0:
        raise ValueError(f"Negative arm count is invalid: {raw_value!r}")
    return count


def format_wattage_label(wattage: WattageNumber) -> str:
    """Format numeric wattage as column label, e.g. 34 -> '34W'."""
    if isinstance(wattage, float) and wattage.is_integer():
        return f"{int(wattage)}W"
    return f"{wattage}W"


def _natural_sort_key(value: str) -> tuple[tuple[tuple[int, int | str], ...], str]:
    """Generate a natural sort key (numbers ordered numerically, text case-insensitively)."""
    parts = _NATURAL_SPLIT_PATTERN.split(value.strip())
    tokens: list[tuple[int, int | str]] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            tokens.append((0, int(part)))
        else:
            tokens.append((1, part.lower()))
    return (tuple(tokens), value.strip())


def _record_validation_issue(
    issues: list[ValidationIssue],
    row: RawSurveyRow,
    problem: str,
    reason: str,
) -> None:
    """Append a ValidationIssue and write a structured entry to validation.log."""
    issue = ValidationIssue(
        row_number=row.row_number,
        ward_number=row.ward_number or "<MISSING>",
        ccms_id=row.ccms_id or "<MISSING>",
        problem=problem,
        reason=reason,
    )
    issues.append(issue)
    validation_logger.warning(
        "row=%d | ward=%s | ccms_id=%s | problem=%s | action=%s",
        issue.row_number,
        issue.ward_number,
        issue.ccms_id,
        issue.problem,
        issue.reason,
    )


def _resolve_arm_count(
    row: RawSurveyRow,
    issues: list[ValidationIssue],
) -> int | None:
    """Resolve lamp count using Arm Count Status (N): WRONG -> O, CORRECT -> C."""
    arm_status = row.arm_count_status.strip().upper()
    if arm_status not in {"WRONG", "CORRECT"}:
        _record_validation_issue(
            issues,
            row,
            problem=f"Unexpected Arm Count Status in column N: {row.arm_count_status!r}",
            reason="Skipped row because Arm Count Status must be 'CORRECT' or 'WRONG'.",
        )
        return None

    if arm_status == "WRONG":
        count_source_label = "Actual Arm Count (column O)"
        raw_count = row.actual_arm_count
    else:
        count_source_label = "Existing Arm Count (column C)"
        raw_count = row.existing_arm_count

    try:
        return parse_arm_count(raw_count)
    except ValueError as exc:
        _record_validation_issue(
            issues,
            row,
            problem=f"Invalid {count_source_label}: {exc}",
            reason=f"Skipped row due to invalid {count_source_label}.",
        )
        return None


def _extract_row_contributions(
    row: RawSurveyRow,
    issues: list[ValidationIssue],
) -> list[tuple[str, str, WattageNumber, int]]:
    """Apply wattage fallback priority (T & U -> Y -> G) and arm-count rules to a row.

    Priority order (for all rows regardless of column S):
    1. Actual Lamp Wattage 1 (T) and Actual Lamp Wattage 2 (U): 1 lamp per non-blank field.
    2. If both T and U are blank, check Now_lamp_wattage (Y) × Arm Count (O or C via N).
    3. If T, U, and Y are all blank, fall back to Install Lamp Wattage (G) × Arm Count (O or C via N).
    """
    ward = row.ward_number.strip()
    ccms_id = row.ccms_id.strip()

    if not ward:
        _record_validation_issue(
            issues,
            row,
            problem="Missing Ward Number (column B)",
            reason="Skipped row because Ward Number is required for ward-wise grouping.",
        )
        return []

    if not ccms_id:
        _record_validation_issue(
            issues,
            row,
            problem="Missing CCMS ID (column AA)",
            reason="Skipped row because CCMS ID is required for CCMS-wise grouping.",
        )
        return []

    # Priority 1: Check Actual Lamp Wattage 1 (T) and Actual Lamp Wattage 2 (U).
    actual_contributions: list[tuple[str, str, WattageNumber, int]] = []
    for col_label, raw_w in (
        ("Actual Lamp Wattage 1 (column T)", row.actual_lamp_wattage_1),
        ("Actual Lamp Wattage 2 (column U)", row.actual_lamp_wattage_2),
    ):
        try:
            parsed = parse_wattage(raw_w)
        except ValueError as exc:
            _record_validation_issue(
                issues,
                row,
                problem=f"Invalid {col_label}: {exc}",
                reason="Skipped row due to invalid actual lamp wattage value.",
            )
            return []
        if parsed is not None:
            actual_contributions.append((ward, ccms_id, parsed, 1))

    if actual_contributions:
        return actual_contributions

    # Priority 2: Both T and U are blank -> check Now_lamp_wattage (column Y).
    try:
        now_wattage = parse_wattage(row.now_lamp_wattage)
    except ValueError as exc:
        _record_validation_issue(
            issues,
            row,
            problem=f"Invalid Now Lamp Wattage (column Y): {exc}",
            reason="Skipped row due to invalid Now_lamp_wattage value.",
        )
        return []

    if now_wattage is not None:
        arm_count = _resolve_arm_count(row, issues)
        if arm_count is None:
            return []
        return [(ward, ccms_id, now_wattage, arm_count)]

    # Priority 3: T, U, and Y are all blank -> fall back to Install Lamp Wattage (column G).
    try:
        install_wattage = parse_wattage(row.install_lamp_wattage)
    except ValueError as exc:
        _record_validation_issue(
            issues,
            row,
            problem=f"Invalid Install Lamp Wattage (column G): {exc}",
            reason="Skipped row due to invalid Install Lamp Wattage.",
        )
        return []

    if install_wattage is None:
        _record_validation_issue(
            issues,
            row,
            problem="No wattage found in columns T, U, Y (Now_lamp_wattage), or G (Install Lamp Wattage)",
            reason="Skipped row because no valid wattage value was present in T, U, Y, or G.",
        )
        return []

    arm_count = _resolve_arm_count(row, issues)
    if arm_count is None:
        return []

    return [(ward, ccms_id, install_wattage, arm_count)]


def process_survey_rows(rows: list[RawSurveyRow]) -> ProcessedReportData:
    """Validate, compute wattage counts, detect dynamic categories, and consolidate via Pandas."""
    if not rows:
        raise DataProcessingError("Cannot process empty survey row list.")

    issues: list[ValidationIssue] = []
    contributions: list[tuple[str, str, WattageNumber, int]] = []
    valid_row_count = 0
    skipped_row_count = 0

    for row in rows:
        row_items = _extract_row_contributions(row, issues)
        if row_items:
            contributions.extend(row_items)
            valid_row_count += 1
        else:
            skipped_row_count += 1

    if not contributions:
        raise DataProcessingError(
            f"All {len(rows)} input rows failed validation; no valid records to generate report. "
            "Check logs/validation.log for details."
        )

    raw_df = pd.DataFrame(
        contributions,
        columns=["ward_number", "ccms_id", "wattage", "lamp_count"],
    )
    raw_df["ward_number"] = raw_df["ward_number"].astype(str)
    raw_df["ccms_id"] = raw_df["ccms_id"].astype(str)

    # Dynamic wattage detection sorted in ascending numerical order.
    sorted_wattages: list[WattageNumber] = sorted(raw_df["wattage"].unique().tolist())
    wattage_columns = [format_wattage_label(w) for w in sorted_wattages]

    # Ward-wise + CCMS-wise consolidation (never combine different wards for the same CCMS ID).
    grouped = (
        raw_df.groupby(["ward_number", "ccms_id", "wattage"], as_index=False, sort=False)["lamp_count"]
        .sum()
    )
    pivoted = (
        grouped.pivot(index=["ward_number", "ccms_id"], columns="wattage", values="lamp_count")
        .reindex(columns=sorted_wattages, fill_value=0)
        .fillna(0)
    )

    for w in sorted_wattages:
        pivoted[w] = pivoted[w].astype(int)

    # Compute Total Wattage = Sum of (Wattage Category * Lamp Count).
    total_series = sum((pivoted[w] * w for w in sorted_wattages), start=pd.Series(0, index=pivoted.index))
    all_int_wattages = all(isinstance(w, int) or (isinstance(w, float) and w.is_integer()) for w in sorted_wattages)
    if all_int_wattages:
        pivoted["Total Wattage"] = total_series.astype(int)
    else:
        pivoted["Total Wattage"] = total_series.round(2)

    pivoted = pivoted.reset_index()
    pivoted = pivoted.rename(
        columns={
            "ward_number": "Ward No",
            "ccms_id": "CCMS ID",
            **{w: format_wattage_label(w) for w in sorted_wattages},
        }
    )

    # Sort consolidated records by Ward Number, then CCMS ID.
    sort_keys = pivoted.apply(
        lambda r: (_natural_sort_key(str(r["Ward No"])), _natural_sort_key(str(r["CCMS ID"]))),
        axis=1,
    )
    pivoted = pivoted.assign(_sort_key=sort_keys).sort_values("_sort_key", kind="mergesort").drop(columns=["_sort_key"])
    pivoted = pivoted.reset_index(drop=True)

    ordered_cols = ["Ward No", "CCMS ID", *wattage_columns, "Total Wattage"]
    pivoted = pivoted[ordered_cols]

    grand_total_raw = pivoted["Total Wattage"].sum()
    grand_total: WattageNumber = (
        int(grand_total_raw) if all_int_wattages else round(float(grand_total_raw), 2)
    )

    app_logger.info(
        "Processed %d input rows -> %d valid, %d skipped, %d consolidated (Ward+CCMS) rows, "
        "%d dynamic wattage categories (%s), grand total=%sW.",
        len(rows),
        valid_row_count,
        skipped_row_count,
        len(pivoted),
        len(wattage_columns),
        ", ".join(wattage_columns),
        grand_total,
    )

    return ProcessedReportData(
        dataframe=pivoted,
        wattage_values=sorted_wattages,
        wattage_columns=wattage_columns,
        total_input_rows=len(rows),
        valid_rows=valid_row_count,
        skipped_rows=skipped_row_count,
        consolidated_rows=len(pivoted),
        grand_total_wattage=grand_total,
        validation_issues=issues,
    )
