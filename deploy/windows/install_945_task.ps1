# Registers a Windows Task Scheduler job that runs the full 945 day automatically:
#   every Monday-Friday at 09:05 (PC local time -- must be IST), console window visible,
#   beep at 09:45, outcomes, daily report and full-session archive with no manual commands.
# If the PC was off/asleep at 09:05 the task starts as soon as it is available.
# Usage (from the repository folder, normal user PowerShell):
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_945_task.ps1
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = (Get-Command python).Source
if ((Get-TimeZone).Id -ne "India Standard Time") {
    Write-Warning "PC time zone is $((Get-TimeZone).Id), not India Standard Time -- 09:05 local is not 09:05 IST."
}
& $python (Join-Path $repo "945.py") --self-test *> $null
if ($LASTEXITCODE -ne 0) { throw "945 self-test failed -- not installing the task" }
$action   = New-ScheduledTaskAction -Execute $python -Argument "945.py --daemon" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:05am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 8) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
            -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName "PSYGRID-945" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "PSYGRID 945: 09:45 decision, outcomes, report, archive" -Force | Out-Null
Get-ScheduledTask -TaskName "PSYGRID-945" | Select-Object TaskName, State
Write-Host "Installed. Next runs: weekdays 09:05. Check any time with:  python 945.py --status"
