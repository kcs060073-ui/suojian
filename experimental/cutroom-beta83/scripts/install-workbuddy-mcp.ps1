[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE ".workbuddy\mcp.json"),
    [string]$DatabasePath = "",
    [string]$WorkbenchOrigin = "http://127.0.0.1:8765",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
. (Join-Path $PSScriptRoot "mcp-runtime.ps1")
$runtimeBinding = Resolve-CutroomMcpRuntime -ProjectRoot $projectRoot -DatabasePath $DatabasePath -WorkbenchOrigin $WorkbenchOrigin
$pythonPath = $runtimeBinding.PythonPath
$serverPath = $runtimeBinding.ServerPath
$targetPath = [System.IO.Path]::GetFullPath($ConfigPath)
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Write-AtomicUtf8([string]$Path, [string]$Text) {
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        $null = New-Item -ItemType Directory -Force -Path $directory
    }
    $name = [System.IO.Path]::GetFileName($Path)
    $temporary = Join-Path $directory ("." + $name + "." + [Guid]::NewGuid().ToString("N") + ".tmp")
    $backup = Join-Path $directory ("." + $name + "." + [Guid]::NewGuid().ToString("N") + ".bak")
    try {
        [System.IO.File]::WriteAllText($temporary, $Text, $utf8NoBom)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Replace($temporary, $Path, $backup, $true)
            if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
        } else {
            [System.IO.File]::Move($temporary, $Path)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
    }
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "SuoJian Python environment was not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "SuoJian MCP entry point was not found: $serverPath"
}
& $pythonPath -c "import mcp"
if ($LASTEXITCODE -ne 0) {
    throw "SuoJian MCP dependencies are missing. Run: .venv\Scripts\python.exe -m pip install -r requirements-mcp.txt"
}

$entry = [ordered]@{
    type = "stdio"
    command = $pythonPath
    args = @($serverPath) + $runtimeBinding.Arguments + @("--client", "workbuddy")
    cwd = $projectRoot
}

if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
    $raw = [System.IO.File]::ReadAllText($targetPath)
    try { $document = $raw | ConvertFrom-Json }
    catch { throw "WorkBuddy MCP JSON could not be parsed: $targetPath" }
    if ($null -eq $document) {
        throw "WorkBuddy MCP JSON could not be parsed: $targetPath"
    }
} else {
    $document = [pscustomobject]@{}
}
if (-not $document.PSObject.Properties["mcpServers"]) {
    $document | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([pscustomobject]@{})
}
$document.mcpServers | Add-Member -MemberType NoteProperty -Name cutroom -Value ([pscustomobject]$entry) -Force
$proposed = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
$preview = ([ordered]@{ mcpServers = [ordered]@{ cutroom = $entry } } | ConvertTo-Json -Depth 8)

if ($DryRun) {
    Write-Output "[SuoJian] DryRun: no changes will be written to $targetPath"
    Write-Output $preview
    exit 0
}

Write-AtomicUtf8 $targetPath $proposed
Write-Output "[SuoJian] Registered WorkBuddy MCP server 'cutroom' in $targetPath"
Write-Output "Reload WorkBuddy connector discovery before use."
