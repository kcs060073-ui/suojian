param([switch]$CoreOnly)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
. (Join-Path $root 'setup-tools.ps1')

function Run-Step([string]$program, [string[]]$arguments) {
    & $program @arguments
    if ($LASTEXITCODE -ne 0) { throw "安装失败：$program（退出码 $LASTEXITCODE）" }
}

try {
    $bootstrapPython = Ensure-SuoJianSystemTools $root

    foreach ($part in @('main','cutroom')) {
        $directory = Join-Path $root $part
        $python = Join-Path $directory '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python)) {
            Run-Step $bootstrapPython.Exe (@($bootstrapPython.Args) + @('-m','venv',(Join-Path $directory '.venv')))
        }
        & $python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else 1)'
        if ($LASTEXITCODE -ne 0) { throw "$directory 的 Python 环境不可用。请保留该目录并检查后重试。" }
        Write-Host "安装 $part 的 Python 依赖..."
        Run-Step $python @('-m','pip','install','-r',(Join-Path $directory 'requirements.txt'))
        if ($part -eq 'cutroom') {
            Run-Step $python @('-m','pip','install','-r',(Join-Path $directory 'requirements-mcp.txt'))
        } elseif (-not $CoreOnly) {
            Write-Host '安装语音识别、字幕识别和音频处理依赖（下载较大）...'
            Run-Step $python @('-m','pip','install','-r',(Join-Path $directory 'requirements-optional.txt'))
        }
    }
    $mainPython = Join-Path $root 'main\.venv\Scripts\python.exe'
    Run-Step $mainPython @('-m','pip','install','-r',(Join-Path $root 'vibe\work\vendor\vibe-knowledge-film-main\requirements.txt'))
    Run-Step $mainPython @('-m','playwright','install','chromium')

    foreach ($directory in @((Join-Path $root 'cutroom'),(Join-Path $root 'vibe\work\runtime'))) {
        Push-Location $directory
        try {
            $ready = $false
            if (Test-Path -LiteralPath (Join-Path $directory 'node_modules')) {
                try {
                    & npm.cmd ls --depth=0 --omit=optional *> $null
                    $ready = $LASTEXITCODE -eq 0
                } catch { $ready = $false }
            }
            if ($ready) { Write-Host "[已就绪] $directory 的 Node 依赖" }
            elseif (Test-Path -LiteralPath (Join-Path $directory 'node_modules')) {
                Write-Host "[修复] $directory 的 Node 依赖"
                Run-Step 'npm.cmd' @('install','--no-save','--no-package-lock','--no-audit','--no-fund','--legacy-peer-deps')
            } else {
                Write-Host "[安装] $directory 的 Node 依赖"
                Run-Step 'npm.cmd' @('ci','--no-audit','--no-fund','--legacy-peer-deps')
            }
        } finally { Pop-Location }
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
        'rea-video' = 'vibe\work\skills\rea-video'
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
