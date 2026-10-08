# Registers a Windows Task Scheduler job: every Monday-Friday at 08:50 (PC time must be IST)
# the SENSEX expiry engine starts DISARMED and opens its dashboard in your browser; it exits
# by itself at 15:40. No terminal interaction is needed during market hours.
# Credentials are read from the user environment variables DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN.
# Usage (from the repository folder, normal user PowerShell):
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_sensex_expiry_task.ps1 [-Mode paper|live]
param([ValidateSet("paper", "live")][string]$Mode = "paper")
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = (Get-Command python).Source
if ((Get-TimeZone).Id -ne "India Standard Time") {
    Write-Warning "PC time zone is $((Get-TimeZone).Id), not India Standard Time -- 08:50 local is not 08:50 IST."
}
foreach ($v in "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN") {
    if (-not [Environment]::GetEnvironmentVariable($v, "User")) { Write-Warning "$v is not set as a user environment variable." }
}
& $python -m sensex_expiry --self-test *> $null
if ($LASTEXITCODE -ne 0) { throw "sensex_expiry self-test failed -- not installing the task" }
$taskArgs = "-m sensex_expiry.session --feed dhan --mode $Mode --open-browser --exit-after 15:40"
$action   = New-ScheduledTaskAction -Execute $python -Argument $taskArgs -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 8:50am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 8) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
            -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName "SENSEX-Expiry-Engine" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "SENSEX expiry engine ($Mode) + dashboard at http://127.0.0.1:8765" -Force | Out-Null
Get-ScheduledTask -TaskName "SENSEX-Expiry-Engine" | Select-Object TaskName, State
Write-Host "Installed ($Mode). Weekdays 08:50 the dashboard opens at http://127.0.0.1:8765 . The engine starts DISARMED."
