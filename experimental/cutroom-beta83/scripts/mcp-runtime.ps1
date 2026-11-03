# Shared, read-only runtime binding for all CUTROOM MCP launchers.
function Resolve-CutroomMcpRuntime {
    param(
        [Parameter(Mandatory=$true)][string]$ProjectRoot,
        [string]$DatabasePath = "",
        [string]$WorkbenchOrigin = "http://127.0.0.1:8765"
    )
    $root = [System.IO.Path]::GetFullPath($ProjectRoot)
    if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
        $DatabasePath = Join-Path $root "runtime\workbench\catalog.sqlite"
    } elseif (-not [System.IO.Path]::IsPathRooted($DatabasePath)) {
        $DatabasePath = Join-Path $root $DatabasePath
    }
    $database = [System.IO.Path]::GetFullPath($DatabasePath)
    $origin = $WorkbenchOrigin.Trim().TrimEnd('/')
    $parsed = $null
    if (-not [Uri]::TryCreate($origin, [UriKind]::Absolute, [ref]$parsed) -or
        $parsed.Scheme -ne 'http' -or $parsed.Host -notin @('127.0.0.1', 'localhost', '[::1]') -or
        $parsed.AbsolutePath -ne '/' -or $parsed.Query -or $parsed.Fragment -or $parsed.UserInfo) {
        throw "WorkbenchOrigin must be an HTTP loopback origin without a path, query or credentials."
    }
    # Resolve only: never open, create, migrate or probe the user's database.
    return [ordered]@{
        PythonPath = Join-Path $root ".venv\Scripts\python.exe"
        ServerPath = Join-Path $root "scripts\cutroom_mcp.py"
        Arguments = @('--db', $database, '--workbench-origin', $origin)
    }
}
