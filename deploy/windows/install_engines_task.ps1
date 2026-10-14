# One-time setup: run the ten strategy engines automatically every weekday, with nobody at the PC.
#
# Run ONCE in an ADMINISTRATOR PowerShell, from the repository folder:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_engines_task.ps1
#
# What it does:
#   - saves your Telegram token and chat id to telegram.env in this folder (git-ignored),
#   - registers the task "PSYGRID-Engines": Mon-Fri 09:05, runs whether or not you are logged
#     in, wakes the PC from sleep, retries 3 times if it fails, never runs twice at once,
#   - allows wake timers on mains power so the PC can wake itself at 09:05.
# Each morning the task pulls the latest code, runs the engines (they keep the PC awake until
# 15:16), and logs to data\engines\logs\. Telegram tells you it started; holidays exit at once.
# The PC must be switched ON or ASLEEP and plugged in. A shut-down PC cannot start itself.
$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Please run this in PowerShell opened with 'Run as administrator'." }

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = (Get-Command python).Source
if ((Get-TimeZone).Id -ne "India Standard Time") {
    Write-Warning "PC time zone is $((Get-TimeZone).Id), not India Standard Time -- 09:05 local is not 09:05 IST."
}

# --- Telegram credentials -> telegram.env (read by the engines when no environment variable is set)
$token = $env:TELEGRAM_BOT_TOKEN
$chat = $env:TELEGRAM_CHAT_ID
if (-not $token) { $token = [Environment]::GetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "User") }
if (-not $chat)  { $chat  = [Environment]::GetEnvironmentVariable("TELEGRAM_CHAT_ID", "User") }
if (-not $token) { $token = Read-Host "Telegram bot token" }
if (-not $chat)  { $chat  = Read-Host "Telegram chat id" }
$envFile = Join-Path $repo "telegram.env"
"TELEGRAM_BOT_TOKEN=$token`nTELEGRAM_CHAT_ID=$chat" | Out-File -Encoding ascii -NoNewline $envFile
Write-Host "Saved Telegram settings to $envFile (git-ignored)."

# --- checks before installing
& $python (Join-Path $repo "strategy_engines.py") --self-test *> $null
if ($LASTEXITCODE -ne 0) { throw "strategy engine self-test failed -- not installing the task" }
Push-Location $repo
& $python strategy_engines.py --telegram-test
$tg = $LASTEXITCODE
Pop-Location
if ($tg -ne 0) { Write-Warning "Telegram test failed: check the token / chat id (the task is installed anyway)." }

# --- the scheduled task
$wrapper = Join-Path $repo "deploy\windows\run_engines.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$wrapper`" -Python `"$python`"" `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:05am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
# S4U = run whether the user is logged on or not, without storing a password.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName "PSYGRID-Engines" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "PSYGRID ten strategy engines: weekday minute scans, Telegram 90+ alerts" `
    -Force | Out-Null

# --- let the PC wake itself (mains power) for the 09:05 start
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
powercfg /SETACTIVE SCHEME_CURRENT | Out-Null

Get-ScheduledTask -TaskName "PSYGRID-Engines" | Select-Object TaskName, State
Write-Host ""
Write-Host "Installed. From now on, every weekday at 09:05 the engines start by themselves."
Write-Host "Keep the PC ON or ASLEEP (not shut down) and plugged in."
Write-Host "Test it now without waiting:  Start-ScheduledTask -TaskName PSYGRID-Engines"
Write-Host "Today's log:                  data\engines\logs\$(Get-Date -Format yyyy-MM-dd).log"
