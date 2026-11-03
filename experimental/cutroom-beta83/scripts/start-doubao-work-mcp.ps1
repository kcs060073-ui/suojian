[CmdletBinding()]
param(
    [string]$DatabasePath = "",
    [string]$WorkbenchOrigin = "http://127.0.0.1:8765"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
. (Join-Path $PSScriptRoot "mcp-runtime.ps1")
$runtimeBinding = Resolve-CutroomMcpRuntime -ProjectRoot $projectRoot -DatabasePath $DatabasePath -WorkbenchOrigin $WorkbenchOrigin
$pythonPath = $runtimeBinding.PythonPath
$serverPath = $runtimeBinding.ServerPath

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "CUTROOM Python environment was not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "CUTROOM MCP entry point was not found: $serverPath"
}

$tokenDirectory = Join-Path $projectRoot "runtime\mcp"
$tokenFile = Join-Path $tokenDirectory "doubao-work.token"
[System.IO.Directory]::CreateDirectory($tokenDirectory) | Out-Null
if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    [System.IO.File]::WriteAllText(
        $tokenFile,
        $token,
        [System.Text.UTF8Encoding]::new($false)
    )
}
$token = [System.IO.File]::ReadAllText($tokenFile).Trim()
if ($token.Length -lt 32) {
    throw "Doubao Work MCP token is invalid: $tokenFile"
}
$env:CUTROOM_MCP_BEARER_TOKEN = $token

$arguments = @(
    $serverPath,
    "--client", "doubao_work",
    "--transport", "streamable-http",
    "--port", "8766"
) + $runtimeBinding.Arguments

Write-Host "[CUTROOM] Doubao Work MCP: http://127.0.0.1:8766/mcp"
Write-Host "Run scripts\show-doubao-work-mcp-config.ps1 in another window to view the connector fields."
Write-Host "Keep this window open while Doubao Work uses CUTROOM. Press Ctrl+C to stop."
& $pythonPath @arguments
exit $LASTEXITCODE
