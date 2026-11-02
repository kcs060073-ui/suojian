param([switch]$CoreOnly)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Run-Step([string]$program, [string[]]$arguments) {
    & $program @arguments
    if ($LASTEXITCODE -ne 0) { throw "安装失败：$program（退出码 $LASTEXITCODE）" }
}

try {
    foreach ($name in @('py.exe','node.exe','npm.cmd','ffmpeg.exe','ffprobe.exe')) {
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
            throw "找不到 $name。请先安装 Python 3.11、Node.js LTS 和 FFmpeg，并重新打开终端。"
        }
    }
    & py.exe -3.11 -c "import sys; assert sys.version_info[:2] == (3, 11)"
    if ($LASTEXITCODE -ne 0) { throw '请先安装 Python 3.11。' }

    foreach ($part in @('main','cutroom')) {
        $directory = Join-Path $root $part
        $python = Join-Path $directory '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python)) {
            Run-Step 'py.exe' @('-3.11','-m','venv',(Join-Path $directory '.venv'))
        }
        Write-Host "安装 $part 的 Python 依赖..."
        Run-Step $python @('-m','pip','install','-r',(Join-Path $directory 'requirements.txt'))
        if ($part -eq 'cutroom') {
            Run-Step $python @('-m','pip','install','-r',(Join-Path $directory 'requirements-mcp.txt'))
        } elseif (-not $CoreOnly) {
            Write-Host '安装语音识别、字幕识别和音频处理依赖（下载较大）...'
            Run-Step $python @('-m','pip','install','-r',(Join-Path $directory 'requirements-optional.txt'))
        }
    }

    foreach ($directory in @((Join-Path $root 'cutroom'),(Join-Path $root 'vibe\work\runtime'))) {
        Push-Location $directory
        try { Run-Step 'npm.cmd' @('ci','--no-audit','--no-fund') } finally { Pop-Location }
    }
    $modules = Join-Path $root 'vibe\work\runtime\node_modules'
    $fusionModules = Join-Path $root 'vibe\work\park-fusion\node_modules'
    if (-not (Test-Path -LiteralPath $fusionModules)) {
        New-Item -ItemType Junction -Path $fusionModules -Target $modules | Out-Null
    }
    Push-Location (Join-Path $root 'vibe\work\runtime')
    try { Run-Step 'node.exe' @((Join-Path $modules '@remotion\cli\remotion-cli.js'),'browser','ensure') } finally { Pop-Location }
    $skillRoot = Join-Path $env:USERPROFILE '.codex\skills'
    New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
    $skills = [ordered]@{
        'park-vibe-producer' = 'vibe\work\skills\park-vibe-producer'
        'video-shotcraft' = 'vibe\work\vendor\video-shotcraft-main'
        'vibe-knowledge-film' = 'vibe\work\vendor\vibe-knowledge-film-main'
        'anything2explainer' = 'vibe\work\vendor\anything2explainer-main'
    }
    foreach ($name in $skills.Keys) {
        $link = Join-Path $skillRoot $name
        if (Test-Path -LiteralPath $link) {
            Write-Warning "已存在技能 $name，保留原版本。"
        } else {
            New-Item -ItemType Junction -Path $link -Target (Join-Path $root $skills[$name]) | Out-Null
        }
    }
    Write-Host '技能已登记到本机 Codex；请重新打开 Codex 以刷新技能列表。'
    Write-Host '安装完成。双击 启动溯剪.bat 打开工作台。' -ForegroundColor Green
    if ($CoreOnly) { Write-Warning '基础安装未包含语音识别等可选依赖。重新运行不带 -CoreOnly 的安装可补齐。' }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
