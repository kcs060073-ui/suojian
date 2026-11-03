# Support records intentionally exclude exception text, command lines and environment values.
function Write-CutroomFailure {
    param([string]$Root,[string]$Source,[string]$Code,[int]$Line=0,[int]$ExitCode=0)
    try {
        if ($Source -notin @('startup','installer')) { return }
        if ($Code -notmatch '^[A-Z][A-Z0-9_]{0,79}$') { return }
        $directory = Join-Path $Root 'runtime\diagnostics'
        $checkPath = $directory
        while ($checkPath) {
            $item = Get-Item -LiteralPath $checkPath -Force -ErrorAction SilentlyContinue
            if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return }
            if ($checkPath -eq $Root) { break }
            $checkPath = Split-Path -Parent $checkPath
        }
        $null=New-Item -ItemType Directory -Force -Path $directory
        $log=Join-Path $directory 'lifecycle.jsonl'
        $item=Get-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return }
        if ($item -and $item.Length -gt 262144) { [IO.File]::WriteAllText($log,'') }
        $version="unknown"
        try {
            $v=([IO.File]::ReadAllText((Join-Path $Root "package.json")) | ConvertFrom-Json).version
            if ($v -is [string] -and $v.Length -le 80 -and $v -match "^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$") { $version=$v }
        } catch {}
        $event=[ordered]@{version=$version;diagnosticId=('diag-'+[guid]::NewGuid().ToString('N').Substring(0,16));timestamp=[DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");source=$Source;code=$Code;line=[Math]::Max(0,$Line);errno=[Math]::Max(0,$ExitCode)}
        [IO.File]::AppendAllText($log,(($event | ConvertTo-Json -Compress)+[Environment]::NewLine),[Text.UTF8Encoding]::new($false))
    } catch { } # Logging cannot hide or replace the original failure.
}
