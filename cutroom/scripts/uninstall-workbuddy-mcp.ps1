[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE ".workbuddy\mcp.json"),
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$targetPath = [System.IO.Path]::GetFullPath($ConfigPath)
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Write-AtomicUtf8([string]$Path, [string]$Text) {
    $directory = Split-Path -Parent $Path
    $name = [System.IO.Path]::GetFileName($Path)
    $temporary = Join-Path $directory ("." + $name + "." + [Guid]::NewGuid().ToString("N") + ".tmp")
    $backup = Join-Path $directory ("." + $name + "." + [Guid]::NewGuid().ToString("N") + ".bak")
    try {
        [System.IO.File]::WriteAllText($temporary, $Text, $utf8NoBom)
        [System.IO.File]::Replace($temporary, $Path, $backup, $true)
        if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
    }
}

if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
    Write-Output "[SuoJian] WorkBuddy MCP config does not exist; nothing to remove."
    exit 0
}
$raw = [System.IO.File]::ReadAllText($targetPath)
try { $document = $raw | ConvertFrom-Json }
catch { throw "WorkBuddy MCP JSON could not be parsed: $targetPath" }
if ($null -eq $document) {
    throw "WorkBuddy MCP JSON could not be parsed: $targetPath"
}
if (
    -not $document.PSObject.Properties["mcpServers"] -or
    -not $document.mcpServers.PSObject.Properties["cutroom"]
) {
    Write-Output "[SuoJian] WorkBuddy MCP server 'cutroom' is not registered."
    exit 0
}

if ($DryRun) {
    Write-Output "[SuoJian] DryRun: mcpServers.cutroom would be removed from $targetPath"
    exit 0
}

$document.mcpServers.PSObject.Properties.Remove("cutroom")
$proposed = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
Write-AtomicUtf8 $targetPath $proposed
Write-Output "[SuoJian] Removed WorkBuddy MCP server 'cutroom' from $targetPath"
