@echo off
REM Suojian main app - one-command regression test (CMD entry)
REM Real logic is in scripts\run_tests.py. Keep this file ASCII-only:
REM cmd.exe decodes .bat with the OEM code page.
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [FAIL] virtualenv not found: %PY%
    echo        create .venv and install requirements.txt first
    exit /b 1
)

"%PY%" "%ROOT%scripts\run_tests.py"
exit /b %ERRORLEVEL%
