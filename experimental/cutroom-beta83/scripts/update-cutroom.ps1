[CmdletBinding()]
param([string]$ProjectRoot,[string]$PackagePath,[switch]$CheckOnly,[switch]$Recover)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
if (-not $ProjectRoot) { $ProjectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')) }
function Select-InstallFolder {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog=New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description='选择原来安装 CUTROOM 的文件夹（包含启动CUTROOM.bat）'
    $dialog.ShowNewFolderButton=$false
    try { if ($dialog.ShowDialog() -eq 'OK') { return $dialog.SelectedPath }; return $null } finally { $dialog.Dispose() }
}
try {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'release-manifest.json')) -or -not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.venv\Scripts\python.exe'))) {
        $ProjectRoot=Select-InstallFolder
        if (-not $ProjectRoot) { Write-Host '已取消。'; exit 0 }
    }
    $python=Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw '所选目录尚未完成安装或环境缺失，请选择原安装目录，或交维护人员处理。' }
    $engine=Join-Path $PSScriptRoot 'product_update.py'
    if (-not (Test-Path -LiteralPath $engine)) { $engine=Join-Path $PSScriptRoot 'product_update.pyc' }
    if (-not (Test-Path -LiteralPath $engine)) { throw '升级组件不完整，请重新下载。' }
    if (-not $PackagePath -and -not $Recover) {
        Write-Host 'CUTROOM 产品更新升级' -ForegroundColor Cyan
        Write-Host '1. 联网检查并下载新版（无需 GitHub 账号）'
        Write-Host '2. 选择已下载的安装 ZIP'
        Write-Host '3. 恢复上次未完成的升级'
        Write-Host '其他键：取消'
        $choice=Read-Host '请选择'
        if ($choice -eq '2') {
            Add-Type -AssemblyName System.Windows.Forms
            $dialog=New-Object System.Windows.Forms.OpenFileDialog
            $dialog.Title='选择 CUTROOM Windows 安装 ZIP（不用解压）'
            $dialog.Filter='CUTROOM 安装包 (*.zip)|*.zip'
            try {
                if ($dialog.ShowDialog() -ne 'OK') { Write-Host '已取消。';exit 0 }
                $PackagePath=$dialog.FileName
            } finally { $dialog.Dispose() }
        } elseif ($choice -eq '3') { $Recover=$true }
        elseif ($choice -ne '1') { Write-Host '已取消。';exit 0 }
    }
    $arguments=@('-X','utf8','-B',$engine,'--root',$ProjectRoot)
    if ($Recover) {
        $pending=Join-Path $ProjectRoot 'runtime\software-updates\pending.json'
        if (Test-Path -LiteralPath $pending) {
            $journal=Get-Content -LiteralPath $pending -Raw | ConvertFrom-Json
            if ($journal.id -notmatch '^[0-9a-f]{32}$') { throw '升级恢复记录无效，请联系维护人员。' }
            $recovery=Join-Path $ProjectRoot ('runtime\software-updates\'+$journal.id+'\recovery')
            foreach ($entry in $journal.recovery.PSObject.Properties) {
                if ($entry.Name -notin @('product_update.recovery','product_update.pyc') -or $entry.Value -notmatch '^[0-9a-f]{64}$') { throw '恢复组件清单无效。' }
                $file=Join-Path $recovery $entry.Name
                $candidate=$file
                while ($candidate) {
                    $item=Get-Item -LiteralPath $candidate -ErrorAction Stop
                    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw '恢复组件路径含重定向，已停止。' }
                    $candidate=Split-Path -Parent $candidate
                }
                $stream=[IO.File]::OpenRead($file)
                $hasher=[Security.Cryptography.SHA256]::Create()
                try { $actualHash=[BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
                finally { $stream.Dispose(); $hasher.Dispose() }
                if ($actualHash -ne $entry.Value) { throw '恢复组件校验失败。' }
            }
            $engine=Join-Path $recovery 'product_update.recovery'
            if (-not (Test-Path -LiteralPath $engine)) { $engine=Join-Path $recovery 'product_update.pyc' }
            $arguments=@('-X','utf8','-B',$engine,'--root',$ProjectRoot)
        }
        $arguments+='--recover'
    }
    elseif ($PackagePath) { $arguments+=@('--zip',$PackagePath) }
    else { $arguments+='--online' }
    if ($CheckOnly) { $arguments+='--check' }
    & $python @arguments
    exit $LASTEXITCODE
} catch {
    Write-Host ('升级未完成：'+$_.Exception.Message) -ForegroundColor Red
    Write-Host '请保留窗口信息，运行“导出报错日志.bat”后联系维护人员。'
    exit 1
}
