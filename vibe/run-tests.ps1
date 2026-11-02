# Suojian terminal (park Vibe workbench) - one-command regression test
# Logic lives in tools\run_tests.py. This repo has no own venv; it borrows
# the main app's venv by default.
# NOTE: keep this file ASCII-only - Windows PowerShell 5.1 decodes .ps1 with
#       the system ANSI code page and non-ASCII comments can break parsing.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$candidates = @(
    (Join-Path $root ".venv\Scripts\python.exe"),
    "C:\Users\Tanikawa\suojian\.venv\Scripts\python.exe",
    "C:\Users\Tanikawa\Documents\Codex\2026-09-20\continuing-from-chatgpt-conversation-6aaf4eec-667c\suojian\.venv\Scripts\python.exe"
)

$py = $null
foreach ($c in $candidates) {
    if (Test-Path $c) { $py = $c; break }
}

if (-not $py) {
    Write-Host "[FAIL] no usable Python interpreter found" -ForegroundColor Red
    Write-Host "       expected one of: $($candidates -join ' | ')" -ForegroundColor Yellow
    exit 1
}

& $py (Join-Path $root "tools\run_tests.py")
exit $LASTEXITCODE
