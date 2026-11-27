$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'setup-tools.ps1')

$script:installed = @()
$script:pythonReady = $true
$script:nodeReady = $true
$script:ffmpegReady = $true
function Refresh-SuoJianPath { }
function Get-SuoJianPython([string]$Root) { if ($script:pythonReady) { return @{ Exe='python.exe'; Args=@('-3.11') } } }
function Test-SuoJianNode { return $script:nodeReady }
function Test-SuoJianFfmpeg { return $script:ffmpegReady }
function Install-SuoJianTool([string]$Id, [string]$Name) {
    $script:installed += $Id
    switch ($Id) {
        'Python.Python.3.11' { $script:pythonReady = $true }
        'OpenJS.NodeJS.LTS' { $script:nodeReady = $true }
        'Gyan.FFmpeg' { $script:ffmpegReady = $true }
        default { throw "unexpected package: $Id" }
    }
}

$python = Ensure-SuoJianSystemTools 'C:\test'
if ($python.Exe -ne 'python.exe' -or $script:installed.Count -ne 0) { throw 'Existing components should be reused.' }
$script:pythonReady = $false
$python = Ensure-SuoJianSystemTools 'C:\test'
if (($script:installed -join ',') -ne 'Python.Python.3.11') { throw 'Only missing Python should be installed.' }
$script:installed = @()
$script:pythonReady = $false; $script:nodeReady = $false; $script:ffmpegReady = $false
$python = Ensure-SuoJianSystemTools 'C:\test'
if ($python.Exe -ne 'python.exe' -or ($script:installed -join ',') -ne 'Python.Python.3.11,OpenJS.NodeJS.LTS,Gyan.FFmpeg') { throw 'Missing components should be installed once.' }
$script:installed = @()
$null = Ensure-SuoJianSystemTools 'C:\test'
if ($script:installed.Count -ne 0) { throw 'Second run should reuse installed components.' }
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'setup-tools.ps1')
function Get-Command { param($Name, $CommandType, $ErrorAction) return $null }
try { Install-SuoJianTool 'Python.Python.3.11' 'Python'; throw 'Missing winget should be reported.' }
catch { if ($_.Exception.Message -notmatch 'winget') { throw } }
Write-Host 'System component detection tests passed.'

$fixture = Join-Path ([IO.Path]::GetTempPath()) ('suojian-font-' + [guid]::NewGuid().ToString('N'))
$font = Join-Path $fixture 'vibe/work/park-fusion/public/fonts/NotoSansSC.ttf'
New-Item -ItemType Directory -Path (Split-Path -Parent $font) -Force | Out-Null
[IO.File]::WriteAllBytes($font, [byte[]](0,1,2,3))
Initialize-SuoJianSharedFonts $fixture
Initialize-SuoJianSharedFonts $fixture
foreach ($alias in @('vendor/anything2explainer-main/template/public/fonts/NotoSansSC.ttf','vendor/vibe-knowledge-film-main/engine/fonts/NotoSansSC-VF.ttf')) {
    $destination = Join-Path $fixture "vibe/work/$alias"
    if ((Get-FileHash -LiteralPath $destination).Hash -ne (Get-FileHash -LiteralPath $font).Hash) {
        throw 'A renderer did not receive the bundled font.'
    }
    Remove-Item -LiteralPath $destination
}
Remove-Item -LiteralPath $font
Write-Host 'Shared font compatibility paths passed.'
