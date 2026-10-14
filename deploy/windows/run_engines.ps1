# Unattended daily run of the ten strategy engines (called by the "PSYGRID-Engines" task).
#   1. git pull --ff-only   (gets fixes automatically; if it fails, today runs on the current code)
#   2. python strategy_engines.py  (exits by itself on holidays and after 15:16)
# Everything is written to data\engines\logs\YYYY-MM-DD.log, because the task runs with no window.
param([string]$Python = "")
$ErrorActionPreference = "Continue"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repo
$logDir = Join-Path $repo "data\engines\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:GIT_TERMINAL_PROMPT = "0"          # never hang waiting for a password in the background
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8   # read python's UTF-8 (rupee sign, emoji) correctly
function Log { process { "$_" | Out-File -Append -Encoding utf8 $log } }   # one encoding for the whole log

"==== {0:yyyy-MM-dd HH:mm:ss} task start ====" -f (Get-Date) | Out-File -Append -Encoding utf8 $log
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) {
    & git pull --ff-only 2>&1 | Log
    if ($LASTEXITCODE -ne 0) { "git pull failed (exit $LASTEXITCODE): running the existing code" | Out-File -Append -Encoding utf8 $log }
} else {
    "git not found: running the existing code" | Out-File -Append -Encoding utf8 $log
}

# the installer passes the exact python.exe, so a different PATH in the background cannot break it
$python = if ($Python) { $Python } else { (Get-Command python).Source }
& $python (Join-Path $repo "strategy_engines.py") 2>&1 | Log
$code = $LASTEXITCODE
"==== {0:yyyy-MM-dd HH:mm:ss} engines exited with code $code ====" -f (Get-Date) | Out-File -Append -Encoding utf8 $log
# 0 = day finished, 20 = holiday/weekend, 23 = session already over: all normal.
if ($code -in 0, 20, 23) { exit 0 } else { exit $code }
