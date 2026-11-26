"""Shared Google Sheets test fixture builders."""

from __future__ import annotations


def make_sheet_row(
    ward: str = "",
    existing_arm: str = "",
    install_wattage: str = "",
    arm_status: str = "",
    actual_arm: str = "",
    survey_status: str = "",
    actual_w1: str = "",
    actual_w2: str = "",
    ccms_id: str = "",
    now_wattage: str = "",
) -> list[str]:
    """Build a 27-column (A..AA) row matching the AppSheet_Survey column positions."""
    row = [""] * 27
    row[1] = ward              # B
    row[2] = existing_arm      # C
    row[6] = install_wattage   # G
    row[13] = arm_status       # N
    row[14] = actual_arm       # O
    row[18] = survey_status    # S
    row[19] = actual_w1        # T
    row[20] = actual_w2        # U
    row[24] = now_wattage      # Y
    row[26] = ccms_id          # AA
    return row


def make_header_row() -> list[str]:
    """Build the 27-column header row for AppSheet_Survey."""
    return make_sheet_row(
        ward="Ward Number",
        existing_arm="Existing Arm Count",
        install_wattage="Install Lamp Wattage",
        arm_status="Arm Count Status",
        actual_arm="Actual Arm Count",
        survey_status="Survey Lamp Wattage Status",
        actual_w1="Actual Lamp Wattage 1",
        actual_w2="Actual Lamp Wattage 2",
        now_wattage="Now_lamp_wattage",
        ccms_id="CCMS ID",
    )
