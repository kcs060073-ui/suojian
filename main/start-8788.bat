@echo off
REM Start the Suojian main app (8788) detached from any parent shell.
REM Keep this file ASCII-only. Logs go to service-8788.log in the repo root.
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo [FAIL] venv not found: %ROOT%.venv\Scripts\python.exe
    exit /b 1
)

REM pythonw has no console; stdout/stderr are redirected to files explicitly.
start "" /b "%ROOT%.venv\Scripts\pythonw.exe" -u web.py >> "%ROOT%service-8788.log" 2>> "%ROOT%service-8788-error.log"
exit /b 0
