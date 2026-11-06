$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
. (Join-Path $root 'setup-tools.ps1')
if (-not (Test-SuoJianProjectReady $root)) {
    Write-Host '正在检查并安装缺少的运行环境，首次使用可能需要较长时间。' -ForegroundColor Cyan
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'install.ps1')
    if ($LASTEXITCODE -ne 0) { throw '环境准备未完成，请查看上方提示并重新启动。' }
    Refresh-SuoJianPath
    if (-not (Test-SuoJianProjectReady $root)) { throw '环境安装完成后仍未通过检查，请关闭窗口后重新启动。' }
}
$main = Join-Path $root 'main'
$cutroom = Join-Path $root 'cutroom'
$env:SUOJIAN_ROOT = $main
$env:SUOJIAN_VIBE_ROOT = Join-Path $root 'vibe'
$env:SUOJIAN_CUTROOM_ROOT = $cutroom
$env:SUOJIAN_PYTHON = Join-Path $main '.venv\Scripts\python.exe'
$env:SUOJIAN_LEGACY_MODULES = Join-Path $root 'vibe\work\runtime\node_modules'
$cutPython = Join-Path $cutroom '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $env:SUOJIAN_PYTHON) -or -not (Test-Path -LiteralPath $cutPython)) {
    throw '请先运行 安装溯剪.bat。'
}
$dbFolder = Join-Path $cutroom 'runtime\workbench'
New-Item -ItemType Directory -Force -Path $dbFolder | Out-Null
$client = [System.Net.Sockets.TcpClient]::new()
try { $cutroomRunning = $client.ConnectAsync('127.0.0.1',8765).Wait(500) -and $client.Connected } catch { $cutroomRunning = $false } finally { $client.Dispose() }
if (-not $cutroomRunning) {
    $db = Join-Path $dbFolder 'catalog.sqlite'
    Start-Process -FilePath $cutPython -ArgumentList @('-u','-m','workbench.server','--db',('"' + $db + '"'),'--port','8765','--no-open') -WorkingDirectory $cutroom -WindowStyle Hidden
}
Write-Host '正在打开溯剪：http://127.0.0.1:8788/ 。关闭此窗口将停止主工作台。'
Start-Process 'http://127.0.0.1:8788/'
Push-Location $main
try { & $env:SUOJIAN_PYTHON 'web.py' } finally { Pop-Location }
