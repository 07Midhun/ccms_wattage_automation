<#
.SYNOPSIS
Registers a daily Windows Scheduled Task to run the CCMS Wattage Report automation.
.DESCRIPTION
Reads CRON_SCHEDULE (HH:MM extracted when using standard 'M H * * *' format) from .env
or accepts -Time (default 09:00) in Asia/Kolkata (India Standard Time).
#>
param(
    [string]$TaskName = "CCMS_Wattage_Daily_Report",
    [string]$Time = "",
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvFile = Join-Path $ProjectDir ".env"

if (-not $Time) {
    $Time = "09:00"
    if (Test-Path $EnvFile) {
        $cronLine = Get-Content $EnvFile | Where-Object { $_ -match "^CRON_SCHEDULE=" } | Select-Object -Last 1
        if ($cronLine) {
            $expr = ($cronLine -split "=", 2)[1].Trim().Trim('"')
            $parts = $expr -split "\s+"
            if ($parts.Count -ge 2 -and $parts[0] -match "^\d+$" -and $parts[1] -match "^\d+$") {
                $Time = "{0:D2}:{1:D2}" -f [int]$parts[1], [int]$parts[0]
            }
        }
    }
}

if (-not $PythonPath) {
    $PythonPath = (Get-Command python -ErrorAction Stop).Source
}

$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "app.py" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Write-Host "Registered Windows Scheduled Task '$TaskName' daily at $Time using '$PythonPath'."
