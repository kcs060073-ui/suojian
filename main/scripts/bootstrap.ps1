param(
    [switch]$SkipWhisper,
    [switch]$SkipFfmpegInstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-PythonModule([string]$Module) {
    & $VenvPython -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$Module') else 1)"
    return $LASTEXITCODE -eq 0
}

Write-Host "[SuoJian] Checking Python..."
if (-not (Test-Command "python")) {
    throw "Python was not found. Install Python 3.11 or newer first."
}
$VersionOk = & python -c "import sys; print(int(sys.version_info >= (3, 11)))"
if ($VersionOk -ne "1") {
    throw "SuoJian requires Python 3.11 or newer."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "[SuoJian] Creating isolated .venv..."
    & python -m venv (Join-Path $ProjectRoot ".venv")
}

$Packages = [ordered]@{
    "flask" = "Flask>=3.0"
    "cv2" = "opencv-python>=4.8"
    "scenedetect" = "scenedetect>=0.6"
    "librosa" = "librosa>=0.10"
    "pyJianYingDraft" = "pyJianYingDraft"
}
if (-not $SkipWhisper) {
    $Packages["whisper"] = "openai-whisper>=20231117"
}

foreach ($Entry in $Packages.GetEnumerator()) {
    if (Test-PythonModule $Entry.Key) {
        Write-Host "[present] $($Entry.Key)"
    } else {
        Write-Host "[install] $($Entry.Value)"
        & $VenvPython -m pip install --disable-pip-version-check --prefer-binary $Entry.Value
        if ($LASTEXITCODE -ne 0) { throw "Failed to install $($Entry.Value)." }
    }
}

if ((-not (Test-Command "ffmpeg")) -or (-not (Test-Command "ffprobe"))) {
    if ($SkipFfmpegInstall) {
        Write-Warning "FFmpeg/FFprobe is missing and automatic installation was skipped."
    } elseif (Test-Command "winget") {
        Write-Host "[install] FFmpeg via winget"
        & winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "FFmpeg installation failed. Install it manually and add it to PATH." }
    } else {
        throw "FFmpeg/FFprobe and winget were not found. Install FFmpeg manually and add it to PATH."
    }
}

Write-Host "[SuoJian] Running environment self-check..."
& $VenvPython (Join-Path $PSScriptRoot "selfcheck.py")
if ($LASTEXITCODE -ne 0) {
    if ($SkipWhisper) {
        Write-Warning "Core environment is ready; Whisper was skipped."
    } else {
        throw "Environment self-check did not fully pass."
    }
}
Write-Host "[done] Run .\.venv\Scripts\python.exe web.py to start the workbench."
