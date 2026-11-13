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

$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $supervisor + '"'
function Test-ThisCutroomTask($task) {
    $actions = @($task.Actions)
    if ($actions.Count -ne 1) { return $false }
    $action = $actions[0]
    return (
        [string]$action.Execute -ieq $taskExecutable -and
        [string]$action.Arguments -ceq $arguments -and
        [string]$action.WorkingDirectory -ieq $projectRoot
    )
}
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and -not (Test-ThisCutroomTask $existingTask)) {
    throw "CutroomLocal belongs to another installation; it was not changed."
}
if ($Remove) {
    if ($existingTask) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "SuoJian autostart task removed."
    } else {
        Write-Output "SuoJian autostart task does not exist."
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found: $python"
}
if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
    throw "SuoJian supervisor not found: $supervisor"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "workbench\server.py") -PathType Leaf)) {
    throw "Not a SuoJian project root: $projectRoot"
}

# Existing listeners are never killed by this installer, even when their page says SuoJian.
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    Push-Location $projectRoot
    try {
        & $python -m workbench.local_entrypoint --db $database --port $port | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Port $port is occupied by an unverified service; no process was stopped."
        }
    } finally { Pop-Location }
}
if ($existingTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}

$action = New-ScheduledTaskAction -Execute $taskExecutable -Argument $arguments -WorkingDirectory $projectRoot
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Start SuoJian locally at 127.0.0.1:8765 after logon" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Push-Location $projectRoot
try {
    & $python -m workbench.local_entrypoint --db $database --port $port --wait 30 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "SuoJian task was created, but the correct service did not become ready within 30 seconds."
    }
} finally { Pop-Location }

Write-Output "SuoJian autostart installed. Double-click 启动CUTROOM.bat to open the authorized workbench."
