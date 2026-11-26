#!/usr/bin/env bash
# Configure daily Cron Job for CCMS Wattage Report Automation in Asia/Kolkata timezone.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"

# Defaults (overridden by .env if present)
CRON_SCHEDULE="0 9 * * *"
TIMEZONE="Asia/Kolkata"

if [[ -f "${ENV_FILE}" ]]; then
    # Read CRON_SCHEDULE and TIMEZONE safely without executing arbitrary code
    ENV_CRON="$(grep -E '^CRON_SCHEDULE=' "${ENV_FILE}" | tail -n 1 | cut -d'=' -f2- | tr -d '\r"' | xargs || true)"
    ENV_TZ="$(grep -E '^TIMEZONE=' "${ENV_FILE}" | tail -n 1 | cut -d'=' -f2- | tr -d '\r"' | xargs || true)"
    if [[ -n "${ENV_CRON}" ]]; then
        CRON_SCHEDULE="${ENV_CRON}"
    fi
    if [[ -n "${ENV_TZ}" ]]; then
        TIMEZONE="${ENV_TZ}"
    fi
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python)}"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "Error: Python interpreter not found." >&2
    exit 1
fi

mkdir -p "${PROJECT_DIR}/logs"
MARKER="# ccms_wattage_automation"
CRON_CMD="cd \"${PROJECT_DIR}\" && TZ=\"${TIMEZONE}\" \"${PYTHON_BIN}\" app.py >> \"${PROJECT_DIR}/logs/cron.log\" 2>&1"
CRON_LINE="${CRON_SCHEDULE} ${CRON_CMD} ${MARKER}"

EXISTING_CRONTAB="$(crontab -l 2>/dev/null || true)"
FILTERED_CRONTAB="$(printf '%s\n' "${EXISTING_CRONTAB}" | grep -v "${MARKER}" | grep -v '^CRON_TZ=' || true)"

{
    echo "CRON_TZ=${TIMEZONE}"
    if [[ -n "${FILTERED_CRONTAB}" ]]; then
        printf '%s\n' "${FILTERED_CRONTAB}"
    fi
    echo "${CRON_LINE}"
} | crontab -

echo "Installed cron job (${TIMEZONE}):"
echo "  ${CRON_LINE}"
