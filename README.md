# Automated Ward-wise CCMS Wattage Report & Daily Email Automation

Automated Python pipeline that reads survey records **strictly read-only** from the `AppSheet_Survey` tab of a Google Spreadsheet, applies wattage and arm-count business rules, consolidates lamp counts by `Ward Number + CCMS ID` with dynamic wattage columns, generates a formatted Excel workbook (`Ward_Wise_CCMS_Report_YYYY-MM-DD.xlsx`), and sends it daily as a reply inside an existing Gmail thread.

---

## Project Structure

```text
ccms_wattage_automation/
├── app.py                          # CLI & end-to-end daily orchestrator
├── config.py                       # Environment configuration & timezone-aware logging
├── requirements.txt                # Python dependencies
├── .env.example                    # Template for secrets & configuration
├── services/
│   ├── google_auth.py              # OAuth 2.0 / Service Account auth (read-only Sheets)
│   ├── google_sheets.py            # Strictly read-only extractor for AppSheet_Survey
│   ├── data_processor.py           # Business rules, dynamic wattage detection & Pandas grouping
│   ├── excel_generator.py          # OpenPyXL formatting & workbook validation
│   └── gmail_service.py            # Threaded Gmail reply & attachment delivery
├── scheduler/
│   ├── cron_setup.sh               # Linux/macOS cron installer (Asia/Kolkata)
│   └── windows_task_setup.ps1      # Windows Task Scheduler helper
├── credentials/                    # OAuth credentials.json & token.json (gitignored)
├── reports/                        # Generated daily Excel reports
├── logs/                           # application.log, validation.log, send_state.json
└── tests/                          # Automated unit & integration tests
```

---

## Strict Read-Only Google Sheets Guarantee

- **OAuth scope**: `https://www.googleapis.com/auth/spreadsheets.readonly`
- Write scopes (`spreadsheets`, `drive`, `drive.file`) are explicitly blocked in `services/google_auth.py`.
- Only `AppSheet_Survey` is read (`spreadsheets().get(...)` metadata check + `spreadsheets().values().get(...)` on `'AppSheet_Survey'!A:AA`).
- All calculations and aggregations happen locally in Python/Pandas; the source Google Spreadsheet is never modified.

---

## Setup Guide

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Google Cloud & OAuth 2.0 Credentials

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project.
2. Enable **Google Sheets API** and **Gmail API**.
3. Configure the OAuth consent screen and add your Google account as a test user (if in testing mode).
4. Create **OAuth 2.0 Client ID** credentials (Application type: **Desktop app**).
5. Download the JSON file and save it as:
   `credentials/credentials.json`

### 3. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required settings in `.env`:

- `GOOGLE_SPREADSHEET_ID`: The spreadsheet ID from the Google Sheets URL (`/spreadsheets/d/<ID>/edit`).
- `GMAIL_THREAD_ID`: Target Gmail thread ID (open the thread in Gmail or inspect via Gmail API; the URL or API `threadId` hex string).
- `EMAIL_TO`: Leave blank/`auto` to automatically reply to the participants in the existing thread, or specify comma-separated addresses.
- `TIMEZONE`: `Asia/Kolkata`
- `CRON_SCHEDULE`: e.g. `0 9 * * *` (9:00 AM IST daily).

### 4. Authorize OAuth 2.0 Once

Run the interactive authorization flow once:

```bash
python app.py --auth
```

This opens a browser window to grant:
- `https://www.googleapis.com/auth/spreadsheets.readonly`
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.send`

The refreshable token is saved to `credentials/token.json`.

---

## Usage & Verification

### Dry Run (Extract + Process + Generate & Validate Excel, No Email)

```bash
python app.py --dry-run
```

Inspect:
- Generated Excel file in `reports/Ward_Wise_CCMS_Report_YYYY-MM-DD.xlsx`
- Execution log in `logs/application.log`
- Validation log for any skipped/flagged rows in `logs/validation.log`

### Full Run (Generate + Send in Gmail Thread)

```bash
python app.py
```

### Retry Email Delivery After a Network/Gmail Outage

If Excel generation succeeded and Gmail failed, the workbook is preserved and `logs/send_state.json` records `email_failed`. Retry sending without regenerating:

```bash
python app.py --retry-email
```

### Duplicate-Send Protection

Running `python app.py` multiple times on the same date (`Asia/Kolkata`) skips sending a duplicate email if `logs/send_state.json` shows `sent` for that date and thread ID. Pass `--force` only when intentionally overriding.

---

## Scheduling Daily Execution

### Option A: GitHub Actions Cloud Schedule (Runs Even When Your Laptop Is Off)

The workflow in `.github/workflows/daily_report.yml` runs on GitHub's cloud runners every day at **09:00 AM IST** (`30 3 * * *` UTC) and supports manual runs via **Run workflow**.

1. **Keep Google OAuth refresh token long-lived**:
   In Google Cloud Console → **OAuth consent screen**, set Publishing Status to **In Production** (otherwise refresh tokens expire every 7 days in Testing mode).
2. **Generate `credentials/token.json` locally once**:
   Run `python app.py --auth` and complete browser login.
3. **Push `ccms_wattage_automation/` as your GitHub repository** (`.gitignore` automatically excludes `.env`, `credentials/*.json`, `reports/*.xlsx`, and `logs/`).
4. **Add GitHub Repository Secrets** (`Settings → Secrets and variables → Actions → Secrets`):
   - `GOOGLE_SPREADSHEET_ID`: Target spreadsheet ID.
   - `GMAIL_THREAD_ID`: Target existing Gmail thread ID.
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: Full JSON contents of `credentials/service_account.json` (read-only Sheets access).
   - `GOOGLE_CREDENTIALS_JSON`: Full JSON contents of `credentials/credentials.json`.
   - `GOOGLE_TOKEN_JSON`: Full JSON contents of `credentials/token.json`.
   - Optional: `EMAIL_TO`, `EMAIL_CC`.
5. **Test from GitHub UI**:
   Open **Actions → Daily CCMS Wattage Report → Run workflow**, run once with `dry_run = true`, inspect the uploaded report/log artifact, then run with `dry_run = false`.

> Note: GitHub Actions `schedule` crons run in UTC and can experience 5–15 minutes of queue delay at peak hours. Also, GitHub pauses scheduled workflows if a repository has no commits for 60 days.

### Option B: Linux / macOS Server (Cron Job)

```bash
chmod +x scheduler/cron_setup.sh
./scheduler/cron_setup.sh
```

This configures `CRON_TZ=Asia/Kolkata` and uses `CRON_SCHEDULE` from `.env` (default `0 9 * * *`).

### Option C: Windows (Task Scheduler — Requires PC Powered On)

```powershell
powershell -ExecutionPolicy Bypass -File .\scheduler\windows_task_setup.ps1
```

---

## Running Tests

```bash
pytest -v
```
