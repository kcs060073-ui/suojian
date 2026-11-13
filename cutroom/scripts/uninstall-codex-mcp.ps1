[CmdletBinding()]
param(
    [string]$ConfigPath = $(
        if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
            Join-Path $env:USERPROFILE ".codex\config.toml"
        } else {
            Join-Path $env:CODEX_HOME "config.toml"
        }
    ),
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$targetPath = [System.IO.Path]::GetFullPath($ConfigPath)
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

. (Join-Path $PSScriptRoot "codex-toml.ps1")

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
    Write-Output "[SuoJian] Codex config was not found; nothing to remove: $targetPath"
    exit 0
}

$extension = [System.IO.Path]::GetExtension($targetPath).ToLowerInvariant()
if ($extension -eq ".json") {
    $document = ([System.IO.File]::ReadAllText($targetPath) | ConvertFrom-Json)
    $changed = $false
    if ($document.PSObject.Properties["mcpServers"] -and $document.mcpServers.PSObject.Properties["cutroom"]) {
        $document.mcpServers.PSObject.Properties.Remove("cutroom")
        $changed = $true
    }
    $proposed = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
} elseif ($extension -eq ".toml") {
    $raw = [System.IO.File]::ReadAllText($targetPath)
    $proposed = Update-CutroomToml -Text $raw
    $changed = $proposed -cne $raw
} else {
    throw "Codex config must use .toml or .json: $targetPath"
}

if (-not $changed) {
    Write-Output "[SuoJian] No 'cutroom' MCP server entry was found."
    exit 0
}
if ($DryRun) {
    Write-Output "[SuoJian] DryRun: only the 'cutroom' MCP entry would be removed; SuoJian data and credentials stay untouched."
    Write-Output "Target config: $targetPath"
    exit 0
}

Write-AtomicUtf8 $targetPath $proposed
Write-Output "[SuoJian] Removed only the Codex MCP server entry 'cutroom'."
Write-Output "SuoJian, tasks, model profiles, and credentials were not changed."
