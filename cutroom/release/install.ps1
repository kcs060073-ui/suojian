[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

function Write-Stage([string]$Message) {
    Write-Host "`n[SuoJian] $Message" -ForegroundColor Cyan
}

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Assert-LastExitCode([string]$Action) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Action 失败（退出码 $LASTEXITCODE）。"
    }
}

function Install-Package([string]$Id, [string]$DisplayName) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "缺少 $DisplayName，且未找到 Windows 程序包管理器 winget。请先从 Microsoft Store 安装‘应用安装程序’，再重试。"
    }
    Write-Stage "正在安装 $DisplayName"
    & winget.exe install --exact --id $Id --accept-package-agreements --accept-source-agreements --silent
    Assert-LastExitCode "安装 $DisplayName"
    Refresh-ProcessPath
}

function Resolve-Python311 {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $probeOutput = @(& $launcher.Source -3.11 -c "import sys; ok=sys.version_info[:2] == (3, 11); print(sys.executable) if ok else None; raise SystemExit(0 if ok else 1)" 2>$null)
            $probeExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($probeExitCode -eq 0 -and $probeOutput.Count -gt 0) {
            return ([string]$probeOutput[-1]).Trim()
        }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $python.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" 2>$null
            $probeExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($probeExitCode -eq 0) { return $python.Source }
    }
    return $null
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "SuoJian Beta 安装器仅支持 64 位 Windows。"
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "SuoJian Beta 安装器仅支持 64 位 Windows。"
    }
    if ($PSVersionTable.PSVersion -lt [Version]"5.1") {
        throw "需要 PowerShell 5.1 或更高版本。"
    }

    # Keep dependency staging inside the extracted SuoJian directory. Shared
    # system temp folders can be cleaned by Windows or endpoint security during
    # the long first install, which leaves pip referring to vanished wheels.
    $installTemp = Join-Path $projectRoot "runtime\install-temp"
    $null = New-Item -ItemType Directory -Force -Path $installTemp
    $env:TEMP = $installTemp
    $env:TMP = $installTemp

    Write-Stage "检查运行环境"
    $python = Resolve-Python311
    if (-not $python) {
        Install-Package "Python.Python.3.11" "Python 3.11"
        $python = Resolve-Python311
    }
    if (-not $python) { throw "Python 3.11 安装完成后仍无法定位，请重启电脑后再次运行安装器。" }

    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue) -or -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        Install-Package "OpenJS.NodeJS.LTS" "Node.js LTS"
    }
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue) -or -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw "Node.js LTS 安装完成后仍无法定位，请重启电脑后再次运行安装器。"
    }

    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)) {
        Install-Package "Gyan.FFmpeg" "FFmpeg"
    }
    if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)) {
        throw "FFmpeg 安装完成后仍无法定位，请重启电脑后再次运行安装器。"
    }

    Set-Location -LiteralPath $projectRoot
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Stage "创建 SuoJian 专用 Python 环境"
        & $python -m venv (Join-Path $projectRoot ".venv")
        Assert-LastExitCode "创建 Python 环境"
    }

    Write-Stage "安装 Python 依赖"
    & $venvPython -m pip install --upgrade pip
    Assert-LastExitCode "升级 pip"
    & $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
    Assert-LastExitCode "安装 Python 依赖"

    Write-Stage "安装 Node.js 依赖"
    & npm.cmd ci --no-audit --no-fund
    Assert-LastExitCode "npm ci"

    $directories = @(
        "runtime\workbench", "out", "assets-local",
        "assets-local\subtitle-presets\inbox", "assets-local\voice-profiles\inbox",
        "assets-local\bgm\inbox", "assets-local\sfx\inbox",
        "assets-local\visual-cards\inbox", "assets-local\stickers\inbox",
        "assets-local\fancy-text-presets\inbox", "assets-local\brand\inbox",
        "public\digital_human", "public\scripts", "public\inserts",
        "public\ad_audio", "public\library"
    )
    foreach ($relative in $directories) {
        $null = New-Item -ItemType Directory -Force (Join-Path $projectRoot $relative)
    }

    Write-Stage "执行安装自检"
    & $venvPython -c "import faster_whisper, numpy, PIL, media_catalog, workbench; print('Python OK')"
    Assert-LastExitCode "Python 自检"
    & node.exe --version
    Assert-LastExitCode "Node.js 自检"
    & ffmpeg.exe -version
    Assert-LastExitCode "FFmpeg 自检"
    & ffprobe.exe -version
    Assert-LastExitCode "ffprobe 自检"

    Write-Host "`n安装完成。以后双击‘启动CUTROOM.bat’即可使用。" -ForegroundColor Green
    Write-Host "如需让 Codex 控制 SuoJian，可选安装 MCP 依赖后先预览注册：" -ForegroundColor Yellow
    Write-Host "powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-codex-mcp.ps1 -DryRun"
    Write-Host "普通安装不会自动注册 MCP，也不会修改 Codex 配置。"
    exit 0
} catch {
    Write-Host "`nCUTROOM 安装未完成：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "请保留本窗口中的错误信息后重试；若仍失败，请交给维护人员排查。" -ForegroundColor Yellow
    exit 1
}
