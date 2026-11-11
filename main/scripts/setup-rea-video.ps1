# Optional isolated rea-video setup. Run explicitly; normal startup never installs it.
$ErrorActionPreference = 'Stop'
$projectMain = Split-Path -Parent $PSScriptRoot
$bootstrapPython = Join-Path $projectMain '.venv\Scripts\python.exe'
$reaPython = Join-Path $projectMain '.rea-venv\Scripts\python.exe'
$pin = '3522250b4d3e667610a175c0f188ac3d725b41cd'
function Run-ReaStep([string]$program, [string[]]$arguments) {
    & $program @arguments
    if ($LASTEXITCODE -ne 0) { throw "rea-video setup failed (exit $LASTEXITCODE)" }
}
if (-not (Test-Path -LiteralPath $bootstrapPython)) { throw 'Run the main installer first.' }
if (-not (Test-Path -LiteralPath $reaPython)) {
    Run-ReaStep $bootstrapPython @('-m','venv',(Join-Path $projectMain '.rea-venv'))
}
Run-ReaStep $reaPython @('-c','import sys; sys.exit(0 if sys.version_info[:2] == (3,11) else 1)')
Run-ReaStep $reaPython @('-m','pip','install','-r',(Join-Path $projectMain 'requirements-rea.txt'))
Run-ReaStep $reaPython @('-m','pip','install','--no-deps',"https://github.com/JuneYaooo/rea-video/archive/$pin.zip")
Run-ReaStep (Join-Path $projectMain '.rea-venv\Scripts\rea-video.exe') @('--help')
Write-Host 'rea-video ready. Main workbench uses offline analysis with VLM disabled.'
