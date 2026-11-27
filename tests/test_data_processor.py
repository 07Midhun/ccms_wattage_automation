"""Tests for wattage business logic, dynamic columns, consolidation, and validation logging."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import setup_logging
from services.data_processor import DataProcessingError, process_survey_rows
from services.google_sheets import RawSurveyRow


def _row(
    row_number: int = 2,
    ward: str = "12",
    existing_arm: str = "",
    install_w: str = "",
    arm_status: str = "",
    actual_arm: str = "",
    survey_status: str = "",
    actual_w1: str = "",
    actual_w2: str = "",
    ccms_id: str = "CCMS001",
    now_w: str = "",
    pole_id: str = "",
    actual_sp_no: str = "",
) -> RawSurveyRow:
    return RawSurveyRow(
        row_number=row_number,
        ward_number=ward,
        existing_arm_count=existing_arm,
        install_lamp_wattage=install_w,
        arm_count_status=arm_status,
        actual_arm_count=actual_arm,
        survey_lamp_wattage_status=survey_status,
        actual_lamp_wattage_1=actual_w1,
        actual_lamp_wattage_2=actual_w2,
        now_lamp_wattage=now_w,
        ccms_id=ccms_id,
        pole_id=pole_id,
        actual_sp_no=actual_sp_no,
    )


def test_survey_wrong_two_different_wattages_ignores_install_and_arm_fields() -> None:
    rows = [
        _row(
            row_number=2,
            ward="12",
            existing_arm="99",       # must be ignored
            install_w="120W",        # must be ignored
            arm_status="INVALID",    # must be ignored
            actual_arm="88",         # must be ignored
            survey_status=" wrong ",
            actual_w1="34W",
            actual_w2="60W",
            ccms_id="CCMS001",
        )
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    assert list(df.columns) == ["Ward No", "Switch Point (SP) Number", "CCMS ID", "34W", "60W", "Total SP", "Total Wattage"]
    assert df.loc[0, "34W"] == 1
    assert df.loc[0, "60W"] == 1
    assert df.loc[0, "Total Wattage"] == 94


def test_survey_wrong_identical_wattages_counts_both_lamps() -> None:
    rows = [
        _row(
            row_number=2,
            ward="12",
            survey_status="WRONG",
            actual_w1="34",
            actual_w2="34W",
            ccms_id="CCMS001",
        )
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    assert list(df.columns) == ["Ward No", "Switch Point (SP) Number", "CCMS ID", "34W", "Total SP", "Total Wattage"]
    assert df.loc[0, "34W"] == 2
    assert df.loc[0, "Total Wattage"] == 68


def test_survey_wrong_one_blank_actual_wattage() -> None:
    rows = [
        _row(
            row_number=2,
            ward="5",
            survey_status="WRONG",
            actual_w1="45W",
            actual_w2="",
            ccms_id="CCMS010",
        )
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    assert list(df.columns) == ["Ward No", "Switch Point (SP) Number", "CCMS ID", "45W", "Total SP", "Total Wattage"]
    assert df.loc[0, "45W"] == 1
    assert df.loc[0, "Total Wattage"] == 45


def test_survey_correct_and_arm_status_wrong_uses_actual_arm_count_only() -> None:
    rows = [
        _row(
            row_number=2,
            ward="12",
            existing_arm="10",   # must NOT be used
            install_w="34W",
            arm_status=" wrong ",
            actual_arm="3",
            survey_status="Correct",
            ccms_id="CCMS001",
        )
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    assert df.loc[0, "34W"] == 3
    assert df.loc[0, "Total Wattage"] == 102


def test_survey_correct_and_arm_status_correct_uses_existing_arm_count_only() -> None:
    rows = [
        _row(
            row_number=2,
            ward="12",
            existing_arm="1",
            install_w="34W",
            arm_status="CORRECT",
            actual_arm="99",     # must NOT be used
            survey_status="CORRECT",
            ccms_id="CCMS001",
        )
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    assert df.loc[0, "34W"] == 1
    assert df.loc[0, "Total Wattage"] == 34


def test_consolidation_same_ward_and_separate_wards_and_dynamic_sorting() -> None:
    rows = [
        # Ward 12, CCMS001: 2x34W (CORRECT/CORRECT) + 1x60W & 1x34W (WRONG) -> 34W=3, 60W=1 -> 162W
        _row(2, "12", existing_arm="2", install_w="34W", arm_status="CORRECT", survey_status="CORRECT", ccms_id="CCMS001"),
        _row(3, "12", survey_status="WRONG", actual_w1="60W", actual_w2="34W", ccms_id="CCMS001"),
        # Ward 13, CCMS001: must NOT combine with Ward 12! 2x34W -> 68W
        _row(4, "13", install_w="34W", arm_status="WRONG", actual_arm="2", survey_status="CORRECT", ccms_id="CCMS001"),
        # Ward 13, CCMS002: 1x45W -> 45W
        _row(5, "13", existing_arm="1", install_w="45W", arm_status="CORRECT", survey_status="CORRECT", ccms_id="CCMS002"),
        # Ward 2, CCMS009: dynamic new wattages 18W and 120W -> 138W (natural sort puts Ward 2 before 12)
        _row(6, "2", survey_status="WRONG", actual_w1="120W", actual_w2="18W", ccms_id="CCMS009"),
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    assert list(df.columns) == [
        "Ward No",
        "Switch Point (SP) Number",
        "CCMS ID",
        "18W",
        "34W",
        "45W",
        "60W",
        "120W",
        "Total SP",
        "Total Wattage",
    ]
    assert list(zip(df["Ward No"], df["CCMS ID"])) == [
        ("2", "CCMS009"),
        ("12", "CCMS001"),
        ("13", "CCMS001"),
        ("13", "CCMS002"),
    ]

    # Verify manual calculations row-by-row
    row_w2 = df.iloc[0]
    assert (row_w2["18W"], row_w2["120W"], row_w2["Total Wattage"]) == (1, 1, 138)

    row_w12 = df.iloc[1]
    assert (row_w12["34W"], row_w12["45W"], row_w12["60W"], row_w12["Total Wattage"]) == (3, 0, 1, 162)

    row_w13_1 = df.iloc[2]
    assert (row_w13_1["34W"], row_w13_1["Total Wattage"]) == (2, 68)

    row_w13_2 = df.iloc[3]
    assert (row_w13_2["45W"], row_w13_2["Total Wattage"]) == (1, 45)

    assert result.grand_total_wattage == 138 + 162 + 68 + 45


def test_invalid_rows_logged_to_validation_log_and_skipped(tmp_path: Path) -> None:
    setup_logging(tmp_path, "INFO", "Asia/Kolkata")
    rows = [
        _row(2, ward="", survey_status="WRONG", actual_w1="34W", ccms_id="CCMS001"),           # missing ward
        _row(3, ward="12", survey_status="WRONG", actual_w1="34W", ccms_id=""),               # missing CCMS ID
        _row(4, ward="12", install_w="", now_w="", actual_w1="", actual_w2="", ccms_id="C2"), # all wattages blank
        _row(5, ward="12", survey_status="WRONG", actual_w1="-", actual_w2="", ccms_id="C3"), # '-' kept invalid
        _row(6, ward="12", survey_status="WRONG", actual_w1="BADW", ccms_id="C4"),            # invalid wattage in T
        _row(7, ward="12", survey_status="CORRECT", arm_status="UNKNOWN", install_w="34W", ccms_id="C5"),
        _row(8, ward="12", survey_status="CORRECT", arm_status="WRONG", install_w="34W", actual_arm="-2", ccms_id="C6"),
        _row(9, ward="12", existing_arm="2", install_w="34W", arm_status="CORRECT", survey_status="CORRECT", ccms_id="VALID01"),
    ]
    result = process_survey_rows(rows)

    assert result.valid_rows == 1
    assert result.skipped_rows == 7
    assert len(result.validation_issues) == 7
    assert result.dataframe.loc[0, "CCMS ID"] == "VALID01"
    assert result.dataframe.loc[0, "Total Wattage"] == 68

    validation_log_text = (tmp_path / "validation.log").read_text(encoding="utf-8")
    for expected_row in ("row=2", "row=3", "row=4", "row=5", "row=6", "row=7", "row=8"):
        assert expected_row in validation_log_text


def test_wattage_priority_t_u_then_now_lamp_y_then_install_g_regardless_of_s() -> None:
    rows = [
        # T & U populated even with S blank/CORRECT -> T & U win over Y and G
        _row(2, ward="1", existing_arm="5", install_w="90W", arm_status="CORRECT", survey_status="CORRECT", actual_w1="34W", actual_w2="45W", now_w="60W", ccms_id="C1"),
        # T & U blank, Y populated -> Y wins over G, using arm count from Condition B (N=WRONG -> O=3)
        _row(3, ward="1", existing_arm="1", install_w="90W", arm_status="WRONG", actual_arm="3", survey_status="", actual_w1="", actual_w2="", now_w="60W", ccms_id="C2"),
        # T & U blank, Y populated, N=CORRECT -> uses C=2
        _row(4, ward="1", existing_arm="2", install_w="90W", arm_status="CORRECT", actual_arm="9", survey_status="WRONG", actual_w1="", actual_w2="", now_w="40W", ccms_id="C3"),
        # T, U, Y all blank -> falls back to G with N=WRONG -> O=2
        _row(5, ward="1", existing_arm="1", install_w="78W", arm_status="WRONG", actual_arm="2", survey_status="WRONG", actual_w1="", actual_w2="", now_w="", ccms_id="C4"),
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    by_ccms = {r["CCMS ID"]: r for _, r in df.iterrows()}
    assert (by_ccms["C1"]["34W"], by_ccms["C1"]["45W"], by_ccms["C1"]["Total Wattage"]) == (1, 1, 79)
    assert (by_ccms["C2"]["60W"], by_ccms["C2"]["Total Wattage"]) == (3, 180)
    assert (by_ccms["C3"]["40W"], by_ccms["C3"]["Total Wattage"]) == (2, 80)
    assert (by_ccms["C4"]["78W"], by_ccms["C4"]["Total Wattage"]) == (2, 156)


def test_sp_number_extraction_priority_and_deduplication_without_changing_wattages() -> None:
    rows = [
        # Row 2 & 3: Same Ward+CCMS, '0082' in AM vs 'SP082' in A -> both normalize to '82' and deduplicate
        _row(2, ward="W75", existing_arm="1", install_w="34W", arm_status="CORRECT", ccms_id="CCMS001", pole_id="GBA-SNL-W75-P2015-SP082", actual_sp_no=""),
        _row(3, ward="W75", existing_arm="2", install_w="34W", arm_status="CORRECT", ccms_id="CCMS001", pole_id="GBA-SNL-W75-P2016-SP082", actual_sp_no="0082"),
        # Row 4: AM actual_sp_no takes priority over col A pole_id and strips leading zeros ('003' -> '3')
        _row(4, ward="W75", actual_w1="60W", ccms_id="CCMS002", pole_id="GBA-SNL-W75-P2020-SP117", actual_sp_no="003"),
        # Row 5: Missing SP in both AM and A -> still keeps wattage row intact with empty SP
        _row(5, ward="W75", actual_w1="40W", ccms_id="CCMS003", pole_id="GBA-SNL-W75-P2099", actual_sp_no=""),
        # Row 6: Same Ward+CCMS as CCMS001, skipped for wattage (blank N & '-'), must still contribute SP129!
        _row(6, ward="W75", existing_arm="", install_w="-", arm_status="", ccms_id="CCMS001", pole_id="GBA-SNL-W75-P2189-SP129", actual_sp_no=""),
    ]
    result = process_survey_rows(rows)
    df = result.dataframe

    by_ccms = {r["CCMS ID"]: r for _, r in df.iterrows()}
    assert by_ccms["CCMS001"]["Switch Point (SP) Number"] == "82, 129"
    assert by_ccms["CCMS001"]["Total SP"] == 2
    assert by_ccms["CCMS001"]["Total Wattage"] == 102
    assert by_ccms["CCMS002"]["Switch Point (SP) Number"] == "3"
    assert by_ccms["CCMS002"]["Total SP"] == 1
    assert by_ccms["CCMS002"]["Total Wattage"] == 60
    assert by_ccms["CCMS003"]["Switch Point (SP) Number"] == ""
    assert by_ccms["CCMS003"]["Total SP"] == 0
    assert by_ccms["CCMS003"]["Total Wattage"] == 40
    assert result.grand_total_sp == 3
    assert result.grand_total_wattage == 202


def test_all_invalid_rows_raises_processing_error(tmp_path: Path) -> None:
    setup_logging(tmp_path, "INFO", "Asia/Kolkata")
    rows = [
        _row(2, ward="", survey_status="WRONG", actual_w1="34W", ccms_id="CCMS001"),
    ]
    with pytest.raises(DataProcessingError, match="failed validation"):
        process_survey_rows(rows)
