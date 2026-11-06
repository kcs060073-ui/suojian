$ErrorActionPreference = 'Stop'

function Refresh-SuoJianPath {
    $paths = @(
        $env:Path,
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\Scripts'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher'),
        (Join-Path $env:ProgramFiles 'nodejs')
    ) | Where-Object { $_ }
    $env:Path = $paths -join ';'
}

function Get-SuoJianPython([string]$Root) {
    $candidates = @()
    $launcher = Get-Command 'py.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) { $candidates += @{ Exe = $launcher.Source; Args = @('-3.11') } }
    $localPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    $candidates += @{ Exe = $localPython; Args = @() }
    foreach ($key in @('HKCU:\Software\Python\PythonCore\3.11\InstallPath', 'HKLM:\Software\Python\PythonCore\3.11\InstallPath')) {
        $registry = Get-Item -LiteralPath $key -ErrorAction SilentlyContinue
        if ($registry -and $registry.GetValue('')) { $candidates += @{ Exe = (Join-Path $registry.GetValue('') 'python.exe'); Args = @() } }
    }
    $systemPython = Get-Command 'python.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($systemPython -and $systemPython.Source -notmatch '\\WindowsApps\\') {
        $candidates += @{ Exe = $systemPython.Source; Args = @() }
    }
    $candidates += @{ Exe = (Join-Path $Root 'main\.venv\Scripts\python.exe'); Args = @() }
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Exe -PathType Leaf)) { continue }
        try {
            & $candidate.Exe @($candidate.Args) -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { }
    }
    return $null
}

function Test-SuoJianNode {
    $node = Get-Command 'node.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $npm = Get-Command 'npm.cmd' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $node -or -not $npm) { return $false }
    try {
        $version = & $node.Source --version 2>$null
        if ($LASTEXITCODE -ne 0 -or $version -notmatch '^v(\d+)\.') { return $false }
        $major = [int]$Matches[1]
        if ($major -ne 18 -and $major -ne 20 -and $major -lt 22) { return $false }
        & $npm.Source --version *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Test-SuoJianFfmpeg {
    foreach ($name in @('ffmpeg.exe','ffprobe.exe')) {
        $tool = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $tool) { return $false }
        try {
            & $tool.Source -version *> $null
            if ($LASTEXITCODE -ne 0) { return $false }
        } catch { return $false }
    }
    return $true
}

function Install-SuoJianTool([string]$Id, [string]$Name) {
    $winget = Get-Command 'winget.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) { throw "缺少 $Name，且 Windows 软件包管理器 winget 不可用。请先通过 Microsoft Store 安装或更新‘应用安装程序’，然后重新运行安装溯剪.bat。" }
    Write-Host "[安装] $Name（使用 Windows 软件包管理器）"
    & $winget.Source install --id $Id --exact --source winget --silent --accept-source-agreements --accept-package-agreements --disable-interactivity | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$Name 自动安装失败（退出码 $LASTEXITCODE）。请检查网络、安装权限和 winget 输出后重新运行安装溯剪.bat。" }
    Refresh-SuoJianPath
}

function Ensure-SuoJianSystemTools([string]$Root) {
    Refresh-SuoJianPath
    $python = Get-SuoJianPython $Root
    if (-not $python) {
        Install-SuoJianTool 'Python.Python.3.11' '64 位 Python 3.11'
        $python = Get-SuoJianPython $Root
        if (-not $python) { throw 'Python 已安装但当前进程未找到可用的 64 位 3.11。请关闭窗口并重新运行安装溯剪.bat。' }
    } else { Write-Host '[已就绪] Python 3.11' }
    if (-not (Test-SuoJianNode)) {
        Install-SuoJianTool 'OpenJS.NodeJS.LTS' 'Node.js LTS'
        if (-not (Test-SuoJianNode)) { throw 'Node.js 已安装但当前进程无法使用。请关闭窗口并重新运行安装溯剪.bat。' }
    } else { Write-Host '[已就绪] Node.js 和 npm' }
    if (-not (Test-SuoJianFfmpeg)) {
        Install-SuoJianTool 'Gyan.FFmpeg' 'FFmpeg 和 FFprobe'
        if (-not (Test-SuoJianFfmpeg)) { throw 'FFmpeg 已安装但当前进程无法使用。请关闭窗口并重新运行安装溯剪.bat。' }
    } else { Write-Host '[已就绪] FFmpeg 和 FFprobe' }
    return $python
}

function Test-SuoJianProjectReady([string]$Root) {
    Refresh-SuoJianPath
    if (-not (Get-SuoJianPython $Root) -or -not (Test-SuoJianNode) -or -not (Test-SuoJianFfmpeg)) { return $false }
    $mainPython = Join-Path $Root 'main\.venv\Scripts\python.exe'
    $cutPython = Join-Path $Root 'cutroom\.venv\Scripts\python.exe'
    foreach ($python in @($mainPython, $cutPython)) {
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    }
    try {
        & $mainPython -c 'import importlib.util,sys; sys.exit(0 if sys.version_info[:2] == (3,11) and all(importlib.util.find_spec(x) for x in sys.argv[1:]) else 1)' flask cv2 scenedetect numpy pyJianYingDraft rapidfuzz PIL playwright 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        & $cutPython -c 'import importlib.util,sys; sys.exit(0 if sys.version_info[:2] == (3,11) and all(importlib.util.find_spec(x) for x in sys.argv[1:]) else 1)' faster_whisper numpy PIL mcp 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
    } catch { return $false }
    foreach ($file in @('cutroom\node_modules\ts-node\package.json', 'vibe\work\runtime\node_modules\@remotion\cli\package.json', 'vibe\work\park-fusion\node_modules')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $file))) { return $false }
    }
    return $true
}
