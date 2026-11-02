[CmdletBinding()]
param(
    [switch]$Describe,
    [switch]$Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$supervisor = Join-Path $projectRoot "workbench\start-local.ps1"
$taskExecutable = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$database = Join-Path $projectRoot "runtime\workbench\catalog.sqlite"
$taskName = "CutroomLocal"
$port = 8765
$fixedUrl = "http://127.0.0.1:$port/"
$config = [ordered]@{
    taskName = $taskName
    fixedUrl = $fixedUrl
    module = "workbench.server"
    projectRoot = $projectRoot
    childExecutable = $python
    supervisorScript = $supervisor
    taskExecutable = $taskExecutable
    database = $database
    port = $port
}

if ($Describe) {
    [pscustomobject]$config | ConvertTo-Json -Compress
    exit 0
}

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($Remove) {
    if ($existingTask) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "CUTROOM autostart task removed."
    } else {
        Write-Output "CUTROOM autostart task does not exist."
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found: $python"
}
if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
    throw "CUTROOM supervisor not found: $supervisor"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "workbench\server.py") -PathType Leaf)) {
    throw "Not a CUTROOM project root: $projectRoot"
}

if ($existingTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 200
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    } while ($existingTask -and $existingTask.State -eq "Running" -and (Get-Date) -lt $deadline)
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $commandLine = [string]$process.CommandLine
    $isCutroomModule = (
        $process.Name -eq "python.exe" -and
        $commandLine.IndexOf("-m workbench.server", [System.StringComparison]::Ordinal) -ge 0
    )
    $isCutroomPage = $false
    if ($isCutroomModule) {
        try {
            $response = Invoke-WebRequest -Uri $fixedUrl -UseBasicParsing -TimeoutSec 3
            $isCutroomPage = (
                $response.StatusCode -eq 200 -and
                $response.Content.IndexOf("<title>CUTROOM", [System.StringComparison]::Ordinal) -ge 0
            )
        } catch {
            $isCutroomPage = $false
        }
    }
    $isThisCutroom = $isCutroomModule -and $isCutroomPage
    if (-not $isThisCutroom) {
        throw "Port $port is occupied by another process (PID $($listener.OwningProcess))."
    }
    Stop-Process -Id $listener.OwningProcess -Force
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 200
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    } while ($listener -and (Get-Date) -lt $deadline)
    if ($listener) {
        throw "Existing CUTROOM process did not release port $port."
    }
}

$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $supervisor + '"'
$action = New-ScheduledTaskAction -Execute $taskExecutable -Argument $arguments -WorkingDirectory $projectRoot
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Start CUTROOM locally at 127.0.0.1:8765 after logon" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 300
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
} while (-not $listener -and (Get-Date) -lt $deadline)
if (-not $listener) {
    throw "CUTROOM task was created, but the service did not start within 30 seconds."
}

Write-Output "CUTROOM autostart installed. Fixed URL: $fixedUrl"
