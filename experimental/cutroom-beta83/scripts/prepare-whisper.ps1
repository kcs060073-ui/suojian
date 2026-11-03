[CmdletBinding()]
param([ValidateRange(30, 1800)][int]$TimeoutSeconds = 600)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$prepareScript = @'
import json
from pathlib import Path

# Systran/faster-whisper-small files required by CTranslate2 and its local tokenizer.
# preprocessor_config.json is optional in faster-whisper 1.2.1 and is absent from
# the current small snapshot; requiring it would cause unnecessary downloads.
SMALL_REQUIRED_FILES = ('config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt')

def missing_small_files(snapshot):
    return [name for name in SMALL_REQUIRED_FILES if not (Path(snapshot) / name).is_file()]

def main():
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        from huggingface_hub.constants import HF_HUB_CACHE
        from huggingface_hub.errors import LocalEntryNotFoundError
        cache = str(HF_HUB_CACHE)
        print(json.dumps({'stage': 'local-cache-check', 'model': 'small', 'cache': cache}), flush=True)
        reused = True
        try:
            snapshot = download_model('small', cache_dir=cache, local_files_only=True)
            missing = missing_small_files(snapshot)
        except (LocalEntryNotFoundError, FileNotFoundError):
            missing = list(SMALL_REQUIRED_FILES)
        if missing:
            reused = False
            print(json.dumps({'stage': 'download', 'model': 'small', 'attempt': 1,
                              'missingFiles': missing}), flush=True)
            snapshot = download_model('small', cache_dir=cache)
            if missing_small_files(snapshot):
                raise FileNotFoundError('required small model files are still missing')
        # Use exactly the same model-name/default-cache lookup as film_review and
        # transcribe. A downloaded directory or successful import is insufficient.
        model = WhisperModel('small', device='cpu', compute_type='int8',
                             cpu_threads=2, local_files_only=True)
        del model
        print(json.dumps({'status': 'ready', 'model': 'small', 'offlineLoad': True,
                          'cache': cache, 'reused': reused}), flush=True)
        return 0
    except Exception as error:
        # Provider exceptions may contain signed URLs; never print their bodies.
        print(json.dumps({'status': 'failed', 'code': 'WHISPER_PREPARE_FAILED',
                          'errorType': type(error).__name__}), flush=True)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
'@

$process = $null
try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw '未找到本安装目录的 Python。请先双击安装CUTROOM.bat。'
    }
    Write-Host '[CUTROOM] 准备本地语音识别 small 模型（首次约 500 MB）；已有完整缓存时不重新下载。'
    Write-Host '[CUTROOM] 使用当前 Windows 用户的 Hugging Face 缓存；实际目录将在下方显示。'
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $python
    $info.Arguments = '-u -'
    $info.WorkingDirectory = $projectRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $info.EnvironmentVariables['PYTHONUTF8'] = '1'
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $info.EnvironmentVariables['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    $info.EnvironmentVariables['HF_HUB_ETAG_TIMEOUT'] = '20'
    $info.EnvironmentVariables['HF_HUB_DOWNLOAD_TIMEOUT'] = '30'
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    if (-not $process.Start()) { throw '无法启动模型准备进程。' }
    # This worker only imports, downloads in-process and loads a local model;
    # it never spawns children. Async drains prevent redirected-pipe deadlocks.
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.StandardInput.Write($prepareScript)
    $process.StandardInput.Close()
    $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
    if ($timedOut) {
        $process.Kill()
        $process.WaitForExit()
    }
    $outputText = $stdoutTask.GetAwaiter().GetResult()
    $null = $stderrTask.GetAwaiter().GetResult() # Do not expose signed URLs from native dependencies.
    if ($outputText) { Write-Host $outputText.TrimEnd() }
    if ($timedOut) { throw "模型准备超过 $TimeoutSeconds 秒，已停止本次进程；没有自动重试。" }
    if ($process.ExitCode -ne 0) { throw 'small 模型下载或离线加载未通过。' }
    Write-Host '[CUTROOM] 本地 small 模型已离线加载验证，数字人转写和首个广告的成片听审可以使用。' -ForegroundColor Green
    exit 0
} catch {
    Write-Host "[CUTROOM] 模型准备未完成：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '请检查网络能否访问 Hugging Face、磁盘空间和可用内存；不要删除任务或素材。' -ForegroundColor Yellow
    Write-Host '恢复后在本安装目录运行：powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\prepare-whisper.ps1' -ForegroundColor Yellow
    Write-Host '模型准备通过后，重新运行安装CUTROOM.bat完成安装自检。缓存损坏或持续失败时，保留诊断结果交维护人员处理。' -ForegroundColor Yellow
    exit 1
} finally {
    if ($null -ne $process) {
        try { if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() } } catch { }
        $process.Dispose()
    }
}
