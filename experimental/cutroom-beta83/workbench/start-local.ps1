[CmdletBinding()]
param(
    [switch]$Describe,
    [switch]$Stop,
    [ValidateRange(5, 60)][int]$StopWaitSeconds = 40,
    [int]$RestartDelaySeconds = 2,
    [string]$DatabasePath,
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$MaintenanceReadOnly,
    [string]$SessionStateDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
. (Join-Path $projectRoot "workbench\diagnostics.ps1")
trap {
    if (-not $MaintenanceReadOnly) { Write-CutroomFailure -Root $projectRoot -Source startup -Code LAUNCHER_FAILED -Line $_.InvocationInfo.ScriptLineNumber }
    throw $_
}
if (Test-Path -LiteralPath (Join-Path $projectRoot "runtime\software-updates\pending.json")) { throw "存在未完成的升级，请运行产品更新升级.bat恢复后再启动。" }
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$database = Join-Path $projectRoot "runtime\workbench\catalog.sqlite"
if ($DatabasePath) { $database = [System.IO.Path]::GetFullPath($DatabasePath) }
$entryArguments = @('-m', 'workbench.local_entrypoint', '--db', $database, '--port', $Port)
$arguments = '-u -m workbench.server --db "' + $database + '" --port ' + $Port + ' --no-open'
if ($MaintenanceReadOnly) {
    if (-not $SessionStateDirectory) { throw "MaintenanceReadOnly requires SessionStateDirectory outside the installation." }
    $sessionState = [System.IO.Path]::GetFullPath($SessionStateDirectory)
    $arguments += ' --maintenance-read-only --session-state-dir "' + $sessionState + '"'
    $entryArguments += @('--maintenance-read-only', '--session-state-dir', $sessionState)
} elseif ($SessionStateDirectory) {
    throw "SessionStateDirectory is only supported with MaintenanceReadOnly."
}
$config = [ordered]@{
    module = "workbench.server"
    projectRoot = $projectRoot
    childExecutable = $python
    childArguments = $arguments
    database = $database
    port = $Port
    maintenanceReadOnly = [bool]$MaintenanceReadOnly
    restartDelaySeconds = $RestartDelaySeconds
    stopRequested = [bool]$Stop
    stopWaitSeconds = $StopWaitSeconds
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
        $pending = $client.ConnectAsync("127.0.0.1", $Port)
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

# Readiness failures must never be mistaken for termination of an owned process.
# The injected operations also let lifecycle tests exercise this exact decision path.
function Invoke-CutroomSupervisorTick {
    param([ref]$Child, [scriptblock]$ProbePort, [scriptblock]$StartChild,
          [scriptblock]$VerifyService, [scriptblock]$ReportIssue)
    if ($null -ne $Child.Value) {
        if (-not $Child.Value.HasExited) { return 'owned-running' }
        # Only a signalled process handle authorizes reclaiming an old job tree.
        $Child.Value.Dispose()
        $Child.Value = $null
    }
    if (& $ProbePort) {
        if ((& $VerifyService $false) -ne 0) {
            throw "Occupied port belongs to an unverified service; no process was changed."
        }
        return 'external-verified'
    }
    $Child.Value = & $StartChild
    $verificationFailure = $null
    try {
        if ((& $VerifyService $true) -ne 0) {
            $verificationFailure = 'CUTROOM startup identity/readiness verification did not succeed.'
        }
    } catch {
        $verificationFailure = 'CUTROOM startup identity/readiness verification could not finish.'
    }
    if ($null -ne $verificationFailure) {
        # Do not unwind into finally/Dispose while the owned process may be doing
        # paid work. It remains unverified; only actual exit permits a restart.
        try { & $ReportIssue ($verificationFailure + ' Keeping the owned process; retry opening after it is ready.') | Out-Null } catch { }
        return 'owned-unverified'
    }
    return 'owned-started'
}

# One supervisor per installation prevents repeated double-clicks from spawning monitor loops.
$hash = [System.Security.Cryptography.SHA256]::Create()
try {
    $scopeBytes = [System.Text.Encoding]::UTF8.GetBytes(($projectRoot.ToLowerInvariant() + "|" + $database.ToLowerInvariant() + "|" + $Port + "|" + [bool]$MaintenanceReadOnly))
    $scope = [BitConverter]::ToString($hash.ComputeHash($scopeBytes)).Replace("-", "")
} finally { $hash.Dispose() }
$controlDirectory = Join-Path (Split-Path -Parent $database) "launcher-state"
if ($MaintenanceReadOnly) {
    $rootPrefix = $projectRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($sessionState.Equals($projectRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $sessionState.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SessionStateDirectory must be outside the installation."
    }
    $controlDirectory = $sessionState
}
if ($Stop -and -not $MaintenanceReadOnly) {
    throw "Controlled Stop requires MaintenanceReadOnly. Writable jobs need coordinated shutdown; no process was changed."
}
$stateFile = Join-Path $controlDirectory ("supervisor-" + $scope + ".json")
$stopFile = Join-Path $controlDirectory ("supervisor-" + $scope + ".stop.json")
function Assert-ControlPath([string]$Path) {
    # Check existing ancestors too: a not-yet-created child of a junction is unsafe.
    $current = [System.IO.Path]::GetFullPath($Path)
    while ($current) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "Reparse control path is not supported; no control file or service was changed."
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
}
Assert-ControlPath $controlDirectory
Assert-ControlPath $stateFile
Assert-ControlPath $stopFile
$mutex = [System.Threading.Mutex]::new($false, "Local\CUTROOM-Supervisor-" + $scope)
$owned = $false
$child = $null
$generation = [Guid]::NewGuid().ToString('N')
function Write-ControlFile([string]$Path, $Value) {
    Assert-ControlPath $Path
    $temporary = $Path + "." + [Guid]::NewGuid().ToString('N') + ".tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}
function Test-StopRequest {
    if (-not (Test-Path -LiteralPath $stopFile -PathType Leaf)) { return $false }
    Assert-ControlPath $stopFile
    $request = Get-Content -LiteralPath $stopFile -Raw | ConvertFrom-Json
    return $request.generation -eq $generation -and $request.scope -eq $scope -and $request.supervisorPid -eq $PID
}
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if ($Stop) {
        if ($owned) { exit 0 } # No monitor: never stop a service merely occupying the port.
        $deadline = [DateTime]::UtcNow.AddSeconds($StopWaitSeconds)
        while (-not (Test-Path -LiteralPath $stateFile -PathType Leaf)) {
            try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
            if ($owned) { exit 0 }
            if ([DateTime]::UtcNow -ge $deadline) { throw "Supervisor has no matching control state; no process was changed." }
            Start-Sleep -Milliseconds 100
        }
        $running = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        if ($running.scope -ne $scope -or $running.generation -notmatch '^[0-9a-f]{32}$' -or $running.supervisorPid -lt 1) {
            throw "Supervisor control identity is invalid; no process was changed."
        }
        Write-ControlFile $stopFile @{ scope = $scope; generation = $running.generation; supervisorPid = $running.supervisorPid }
        while ([DateTime]::UtcNow -lt $deadline) {
            try { $owned = $mutex.WaitOne(100) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
            if ($owned) { exit 0 }
        }
        throw "Supervisor did not stop within the bounded wait."
    }
    if (-not $owned) {
        Push-Location $projectRoot
        try {
            & $python @entryArguments --wait 30 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Existing supervisor did not verify the requested service identity." }
        } finally { Pop-Location }
        exit 0
    }
    [System.IO.Directory]::CreateDirectory($controlDirectory) | Out-Null
    Write-ControlFile $stateFile @{ scope = $scope; generation = $generation; supervisorPid = $PID }
    # Create suspended, assign to a private Windows Job, then resume. No child can
    # escape before assignment. Closing this monitor kills only its own job tree.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public sealed class CutroomOwnedChild : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong A,B,C,D,E,F; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public BasicLimits BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct StartupInfo {
        public uint cb; public string reserved, desktop, title;
        public uint x,y,xSize,ySize,xCountChars,yCountChars,fillAttribute,flags;
        public ushort showWindow,reserved2; public IntPtr reserved2Ptr,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {
        public IntPtr process,thread; public uint pid,tid;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int type, ref ExtendedLimits info, uint size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcess(string application, StringBuilder command, IntPtr pa, IntPtr ta,
        bool inherit, uint flags, IntPtr environment, string cwd, ref StartupInfo startup, out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    IntPtr job, process;
    public uint Id { get; private set; }
    public CutroomOwnedChild(string executable, string arguments, string cwd) {
        job=CreateJobObject(IntPtr.Zero,null);
        if(job==IntPtr.Zero) throw new Win32Exception();
        var limits=new ExtendedLimits(); limits.BasicLimitInformation.LimitFlags=0x2000; // KILL_ON_JOB_CLOSE
        ProcessInfo pi=new ProcessInfo();
        try {
            if(!SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(limits))) throw new Win32Exception();
            var si=new StartupInfo(); si.cb=(uint)Marshal.SizeOf(si);
            // CREATE_SUSPENDED | CREATE_NO_WINDOW; child inherits the caller's environment.
            if(!CreateProcess(executable,new StringBuilder("\""+executable+"\" "+arguments),IntPtr.Zero,IntPtr.Zero,
                false,0x08000004,IntPtr.Zero,cwd,ref si,out pi)) throw new Win32Exception();
            process=pi.process; Id=pi.pid;
            if(!AssignProcessToJobObject(job,process)) throw new Win32Exception();
            if(ResumeThread(pi.thread)==0xffffffff) throw new Win32Exception();
        } catch {
            if(pi.process!=IntPtr.Zero) TerminateProcess(pi.process,1);
            Dispose(); throw;
        } finally { if(pi.thread!=IntPtr.Zero) CloseHandle(pi.thread); }
    }
    // WAIT_OBJECT_0 is positive exit evidence. WAIT_TIMEOUT (or an unexpected
    // wait error) must not authorize killing a potentially active process.
    public bool HasExited { get { return process==IntPtr.Zero || WaitForSingleObject(process,0)==0; } }
    public void Dispose() {
        if(job!=IntPtr.Zero) { TerminateJobObject(job,0); CloseHandle(job); job=IntPtr.Zero; }
        if(process!=IntPtr.Zero) { WaitForSingleObject(process,5000); CloseHandle(process); process=IntPtr.Zero; }
    }
}
'@
    Push-Location $projectRoot
    try {
        while (-not (Test-StopRequest)) {
            Invoke-CutroomSupervisorTick ([ref]$child) `
                { Test-CutroomPort } `
                { [CutroomOwnedChild]::new($python, $arguments, $projectRoot) } `
                {
                    param($waitForStartup)
                    if ($waitForStartup) { & $python @entryArguments --wait 30 | Out-Null }
                    else { & $python @entryArguments | Out-Null }
                    return $LASTEXITCODE
                } `
                { param($message) Write-Warning $message } | Out-Null
            $nextCheck = [DateTime]::UtcNow.AddSeconds($RestartDelaySeconds)
            while ([DateTime]::UtcNow -lt $nextCheck -and -not (Test-StopRequest)) {
                Start-Sleep -Milliseconds 100
            }
        }
    } finally { Pop-Location }
} finally {
    if ($null -ne $child) { $child.Dispose() }
    if ($owned -and -not $Stop) {
        if (Test-Path -LiteralPath $stateFile) { Remove-Item -LiteralPath $stateFile }
        if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile }
    }
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
