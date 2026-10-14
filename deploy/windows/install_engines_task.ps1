# Registers a Windows Task Scheduler job that runs the ten strategy engines automatically:
#   every Monday-Friday at 09:05 (PC local time -- must be IST); the engines wait for the
#   09:15 open, scan every minute until 15:16 and send Telegram alerts for 90+ signals.
# If the PC was off/asleep at 09:05 the task starts as soon as it is available; a restart
# resumes the day's ledger, so an alert already sent is never sent twice.
# Usage (from the repository folder, normal user PowerShell):
#   $env:TELEGRAM_BOT_TOKEN = "123:ABC"; $env:TELEGRAM_CHAT_ID = "-100123"   # optional, once
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_engines_task.ps1
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = (Get-Command python).Source
if ((Get-TimeZone).Id -ne "India Standard Time") {
    Write-Warning "PC time zone is $((Get-TimeZone).Id), not India Standard Time -- 09:05 local is not 09:05 IST."
}
if ($env:TELEGRAM_BOT_TOKEN -and $env:TELEGRAM_CHAT_ID) {
    # stored as user environment variables, never in the task definition
    [Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", $env:TELEGRAM_BOT_TOKEN, "User")
    [Environment]::SetEnvironmentVariable("TELEGRAM_CHAT_ID", $env:TELEGRAM_CHAT_ID, "User")
} elseif (-not [Environment]::GetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "User")) {
    Write-Warning "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: alerts will only be printed."
}
& $python (Join-Path $repo "strategy_engines.py") --self-test *> $null
if ($LASTEXITCODE -ne 0) { throw "strategy engine self-test failed -- not installing the task" }
$action   = New-ScheduledTaskAction -Execute $python -Argument "strategy_engines.py" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:05am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 8) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
            -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName "PSYGRID-Engines" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "PSYGRID ten strategy engines: minute scans, Telegram 90+ alerts" -Force | Out-Null
Get-ScheduledTask -TaskName "PSYGRID-Engines" | Select-Object TaskName, State
Write-Host "Installed. Next runs: weekdays 09:05. Test Telegram with:  python strategy_engines.py --telegram-test"
