@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release\install.ps1"
if errorlevel 1 (
  echo.
  echo 安装未完成，请查看上方错误信息。
  pause
  exit /b 1
)
echo.
echo CUTROOM 已安装完成。
pause
