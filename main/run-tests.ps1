# Suojian main app - one-command regression test (PowerShell entry)
# Logic lives in scripts\run_tests.py; this wrapper only locates the interpreter.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 using the
#       system ANSI code page, so non-ASCII comments can break quote parsing.
# NOTE: no here-string here on purpose - PS 5.1 misparses here-string
#       terminators in files with LF-only line endings.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "[FAIL] virtualenv not found: $py" -ForegroundColor Red
    Write-Host "       create .venv and install requirements.txt first" -ForegroundColor Yellow
    exit 1
}

& $py (Join-Path $root "scripts\run_tests.py")
exit $LASTEXITCODE
