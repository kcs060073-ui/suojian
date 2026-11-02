@echo off
REM Suojian terminal (park Vibe workbench) - one-command regression test
REM Real logic is in tools\run_tests.py. This repo has no own venv; it
REM borrows the main app venv by default. Keep this file ASCII-only.
REM 2026-10-23: main app moved to C:\Users\Tanikawa\suojian (was under a
REM dated Codex temp dir). New path wins, legacy path kept as last resort.
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PY=%ROOT%.venv\Scripts\python.exe"
) else if exist "C:\Users\Tanikawa\suojian\.venv\Scripts\python.exe" (
    set "PY=C:\Users\Tanikawa\suojian\.venv\Scripts\python.exe"
) else (
    set "PY=C:\Users\Tanikawa\Documents\Codex\2026-09-20\continuing-from-chatgpt-conversation-6aaf4eec-667c\suojian\.venv\Scripts\python.exe"
)

if not exist "%PY%" (
    echo [FAIL] no usable Python interpreter found
    echo        expected: %PY%
    exit /b 1
)

"%PY%" "%ROOT%tools\run_tests.py"
exit /b %ERRORLEVEL%
