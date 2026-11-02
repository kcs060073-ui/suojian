[CmdletBinding()]
param(
    [switch]$Describe,
    [int]$RestartDelaySeconds = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$database = Join-Path $projectRoot "runtime\workbench\catalog.sqlite"
$arguments = '-u -m workbench.server --db "' + $database + '" --port 8765 --no-open'
$config = [ordered]@{
    module = "workbench.server"
    projectRoot = $projectRoot
    childExecutable = $python
    childArguments = $arguments
    database = $database
    port = 8765
    restartDelaySeconds = $RestartDelaySeconds
}

if ($Describe) {
    [pscustomobject]$config | ConvertTo-Json -Compress
    exit 0
}
if ($RestartDelaySeconds -lt 1 -or $RestartDelaySeconds -gt 60) {
    throw "RestartDelaySeconds must be between 1 and 60."
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found: $python"
}

function Test-CutroomPort {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.ConnectAsync("127.0.0.1", 8765)
        if (-not $pending.Wait(500)) {
            return $false
        }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

while ($true) {
    if (-not (Test-CutroomPort)) {
        try {
            $null = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
            $deadline = (Get-Date).AddSeconds(30)
            do {
                Start-Sleep -Milliseconds 250
            } while (-not (Test-CutroomPort) -and (Get-Date) -lt $deadline)
        } catch {
            Write-Error $_ -ErrorAction Continue
        }
    }
    Start-Sleep -Seconds $RestartDelaySeconds
}
