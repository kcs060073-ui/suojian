[CmdletBinding()]
param(
    [string]$TokenFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ([string]::IsNullOrWhiteSpace($TokenFile)) {
    $TokenFile = Join-Path $projectRoot "runtime\mcp\doubao-work.token"
}
$resolvedTokenFile = [System.IO.Path]::GetFullPath($TokenFile)
if (-not (Test-Path -LiteralPath $resolvedTokenFile -PathType Leaf)) {
    throw "Start the Doubao Work MCP first; token file was not found: $resolvedTokenFile"
}
$token = [System.IO.File]::ReadAllText($resolvedTokenFile).Trim()
if ($token.Length -lt 32) {
    throw "Doubao Work MCP token is invalid: $resolvedTokenFile"
}

$descriptor = [ordered]@{
    name = "cutroom"
    transport = "http"
    url = "http://127.0.0.1:8766/mcp"
    headers = [ordered]@{
        Authorization = "Bearer $token"
    }
}
$descriptor | ConvertTo-Json -Depth 8
