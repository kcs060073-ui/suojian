[CmdletBinding()]
param([string]$ProjectRoot, [switch]$OpenFolder, [string]$RecordCode)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
if (-not $ProjectRoot) { $ProjectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')) }
if ($RecordCode) {
    . (Join-Path $ProjectRoot 'workbench\diagnostics.ps1')
    Write-CutroomFailure -Root $ProjectRoot -Source startup -Code $RecordCode
    exit 0
}
$output=Join-Path $ProjectRoot 'out'
$null=New-Item -ItemType Directory -Force -Path $output
$target=Join-Path $output ('SuoJian-errors-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,6)+'.zip')
$python=Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$exported=$false
if (Test-Path -LiteralPath $python -PathType Leaf) {
    Push-Location $ProjectRoot
    try {
        & $python -X utf8 -B -m workbench.diagnostics --root $ProjectRoot --export $target
        $exported=($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $target -PathType Leaf))
    } catch { $exported=$false } finally { Pop-Location }
}
if (-not $exported) {
    if (Test-Path -LiteralPath $target) { $target=Join-Path $output ("SuoJian-errors-fallback-"+[guid]::NewGuid().ToString("N")+".zip") }
    # Python may not be installed yet. Export only validated installation/startup events.
    $events=@()
    $log=Join-Path $ProjectRoot 'runtime\diagnostics\lifecycle.jsonl'
    $safe=$true;$checkPath=$log
    while ($checkPath) {
        $item=Get-Item -LiteralPath $checkPath -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { $safe=$false;break }
        if ($checkPath -eq $ProjectRoot) { break }
        $checkPath=Split-Path -Parent $checkPath
    }
    if ($safe -and (Test-Path -LiteralPath $log -PathType Leaf) -and (Get-Item -LiteralPath $log).Length -le 524288) {
        foreach($line in [IO.File]::ReadAllLines($log)) {
            try {
                $e=$line | ConvertFrom-Json
                if ($e.timestamp -is [DateTime]) { $e.timestamp=$e.timestamp.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }
                if ($e.diagnosticId -notmatch '^diag-[a-f0-9]{16}$' -or $e.timestamp -notmatch '^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$' -or $e.source -notin @('startup','installer') -or $e.code -notmatch '^[A-Z][A-Z0-9_]{0,79}$') { continue }
                $version='unknown'
                if ($e.version -is [string] -and $e.version.Length -le 80 -and $e.version -match '^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$') { $version=$e.version }
                $events += ([ordered]@{version=$version;diagnosticId=$e.diagnosticId;timestamp=$e.timestamp;source=$e.source;code=$e.code;line=[Math]::Max(0,[int]$e.line);errno=[Math]::Max(0,[int]$e.errno)} | ConvertTo-Json -Compress)
            } catch { }
        }
    }
    $version='unknown'
    try {
        $v=([IO.File]::ReadAllText((Join-Path $ProjectRoot 'package.json')) | ConvertFrom-Json).version
        if ($v -is [string] -and $v.Length -le 80 -and $v -match '^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$') { $version=$v }
    } catch {}
    Add-Type -AssemblyName System.IO.Compression
    $stream=[IO.File]::Open($target,[IO.FileMode]::CreateNew)
    try {
        $zip=[IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Create)
        try {
            $contents=@{'environment.json'=([ordered]@{product='SuoJian';version=$version;coverage='installation-startup-only';exportedAt=[DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")} | ConvertTo-Json);'errors.jsonl'=($events -join "`n");'说明.txt'='安装环境尚未就绪：此包只含安装/启动错误类别，不含服务端和浏览器日志。请补充出错时间及所点操作。日志不含密钥、文案或素材。'}
            foreach($name in $contents.Keys) {
                $entry=$zip.CreateEntry($name)
                $writer=[IO.StreamWriter]::new($entry.Open(),[Text.UTF8Encoding]::new($false))
                try {$writer.Write($contents[$name])}finally{$writer.Dispose()}
            }
        }finally{$zip.Dispose()}
    }finally{$stream.Dispose()}
}
Write-Host ('报错日志已导出：'+$target)
Write-Host '请把 ZIP 发给维护人员，并说明出错时间和刚才点了什么。'
if($OpenFolder){Start-Process explorer.exe -ArgumentList ('/select,"'+$target+'"')}
