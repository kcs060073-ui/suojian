@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 剪辑工作台 尚未安装。请先双击“安装CUTROOM.bat”。
  pause
  exit /b 1
)
if not exist "%~dp0node_modules" (
  echo 剪辑工作台 的 Node.js 依赖不完整。请重新双击“安装CUTROOM.bat”。
  pause
  exit /b 1
)
start "剪辑工作台 服务" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0workbench\start-local.ps1"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8765/"
echo 剪辑工作台 正在启动；浏览器将在服务准备后打开。
exit /b 0
