"""Bounded local PCM measurements for packaging advice; no semantic listening.

Metrics describe the first audio stream decoded at 16 kHz (at most stereo),
not source-rate peaks, integrated LUFS, true peak, or EBU loudness range.
Cache entries are immutable measurements keyed by content and method version.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading

import numpy as np

from .tools import find_ffmpeg, find_ffprobe, default_ffprobe_search_roots

METHOD_VERSION = "pcm-energy-envelope-v1"
MAX_ANALYSIS_SECONDS = 600
SAMPLE_RATE = 16000
MIN_ANALYSIS_SECONDS = .1
DECODE_TIMEOUT_SECONDS = 90


class AudioEvidenceError(RuntimeError):
    pass


def file_sha256(source: Path) -> str:
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_audio_evidence(db, source_sha256: str) -> dict | None:
    row = db.connection.execute(
        "SELECT evidence_json FROM packaging_audio_evidence WHERE source_sha256=? AND method_version=?",
        (source_sha256, METHOD_VERSION),
    ).fetchone()
    return json.loads(row["evidence_json"]) if row else None


def _run_bounded(command, *, timeout: float, max_bytes: int) -> bytes:
    """Drain one bounded pipe; discard diagnostics to avoid leaking local paths.

    FFmpeg/ffprobe are launched directly without a shell and spawn no children.
    The timeout kills and reaps the process; output overflow does the same.
    """
    output = bytearray()
    overflow = threading.Event()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def read_output():
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                return
            if len(output) + len(chunk) > max_bytes:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            output.extend(chunk)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait(timeout=5)
        raise AudioEvidenceError("AUDIO_PROCESS_TIMEOUT") from error
    finally:
        reader.join(timeout=5)
        process.stdout.close()
    if overflow.is_set():
        raise AudioEvidenceError("AUDIO_OUTPUT_LIMIT")
    if process.returncode:
        raise AudioEvidenceError("AUDIO_DECODE_FAILED")
    return bytes(output)


def _decode_audio(source: Path):
    roots = default_ffprobe_search_roots()
    ffmpeg = find_ffmpeg(os.environ, search_roots=roots)
    ffprobe = find_ffprobe(os.environ, search_roots=roots)
    probe = json.loads(_run_bounded([str(ffprobe), "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=channels,duration:format=duration", "-of", "json", str(source)],
        timeout=15, max_bytes=32768))
    streams = probe.get("streams", [])
    if not streams:
        raise AudioEvidenceError("AUDIO_NO_STREAM")
    original_channels = int(streams[0].get("channels") or 0)
    if original_channels < 1:
        raise AudioEvidenceError("AUDIO_DECODE_FAILED")
    channels = min(original_channels, 2)
    duration = None
    for value in [streams[0].get("duration"), probe.get("format", {}).get("duration")]:
        try:
            candidate = float(value)
            if math.isfinite(candidate) and candidate > 0:
                duration = candidate
                break
        except (TypeError, ValueError):
            pass
    # One extra short interval detects truncation even if duration is absent.
    decode_seconds = MAX_ANALYSIS_SECONDS + .05
    data = _run_bounded([str(ffmpeg), "-v", "error", "-xerror", "-nostdin", "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", str(decode_seconds),
        "-ac", str(channels), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"],
        timeout=DECODE_TIMEOUT_SECONDS,
        max_bytes=math.ceil((decode_seconds + .1) * SAMPLE_RATE) * channels * 4)
    if not data or len(data) % (4 * channels):
        raise AudioEvidenceError("AUDIO_DECODE_FAILED")
    samples = np.frombuffer(data, dtype="<f4").reshape(-1, channels)
    if not np.isfinite(samples).all():
        raise AudioEvidenceError("AUDIO_DECODE_FAILED")
    return samples, duration, original_channels


def _db(amplitude):
    return round(20 * math.log10(max(float(amplitude), 1e-6)), 3)


def _window(samples, start, end):
    block = samples[round(start*SAMPLE_RATE):round(end*SAMPLE_RATE)]
    return {"start_seconds": round(start, 4), "end_seconds": round(end, 4),
        "rms_dbfs": _db(np.sqrt(np.mean(np.square(block, dtype=np.float64)))),
        "sample_peak_dbfs": _db(np.max(np.abs(block)))}


def _tempo(samples):
    unknown = {"status": "unknown", "bpm": None, "confidence": 0.0,
        "reason": "insufficient_or_irregular_energy_onsets",
        "method": "10ms_rms_onset_intervals_v1"}
    if len(samples) < 8*SAMPLE_RATE:
        return {**unknown, "reason": "less_than_8_seconds"}
    hop = SAMPLE_RATE // 100
    frames = len(samples) // hop
    blocks = samples[:frames*hop].reshape(frames, hop, -1)
    energy = np.sqrt(np.mean(np.square(blocks, dtype=np.float64), axis=(1, 2)))
    if np.std(energy) / max(np.mean(energy), 1e-6) < .15:
        return {**unknown, "reason": "no_clear_energy_modulation"}
    onset = np.maximum(0, np.diff(energy, prepend=energy[0]))
    threshold = max(float(np.max(onset))*.2, float(np.mean(onset)+2*np.std(onset)))
    peaks = []
    for i in range(1, len(onset)-1):
        if onset[i] >= threshold and onset[i] > onset[i-1] and onset[i] >= onset[i+1]:
            if not peaks or i-peaks[-1] >= 20:
                peaks.append(i)
            elif onset[i] > onset[peaks[-1]]:
                peaks[-1] = i
    if len(peaks) < 8:
        return unknown
    intervals = np.diff(peaks) / 100
    interval = float(np.median(intervals))
    deviation = float(np.std(intervals) / max(np.mean(intervals), .01))
    confidence = float(np.mean(np.abs(intervals-interval) <= .04)) * max(0, 1-deviation)
    bpm = 60/interval
    if not 60 <= bpm <= 200 or deviation > .12 or confidence < .75:
        return unknown
    return {"status": "estimated", "bpm": round(bpm, 2), "confidence": round(confidence, 3),
        "reason": "regular_energy_onsets_not_musical_beat_confirmation",
        "method": unknown["method"]}


ERRORS = {
    "AUDIO_SOURCE_UNAVAILABLE": "音频文件不可读取",
    "AUDIO_SOURCE_CHANGED": "音频在分析期间发生变化，请重试",
    "AUDIO_DECODE_FAILED": "音频损坏或无法完整解码分析范围",
    "AUDIO_NO_STREAM": "文件没有可分析的音轨",
    "AUDIO_TOO_SHORT": "音频不足0.1秒，无法形成可靠证据",
    "AUDIO_SILENT": "分析范围为静音或近静音，无法形成可靠证据",
    "AUDIO_PROCESS_TIMEOUT": "音频分析超过本地执行时限",
    "AUDIO_OUTPUT_LIMIT": "音频解码输出超过安全上限",
    "AUDIO_TOOL_UNAVAILABLE": "本机FFmpeg或ffprobe不可用",
}


def analyze_audio_file(db, source: Path) -> dict:
    source = Path(source)
    identity = {"source_sha256": None, "method_version": METHOD_VERSION}
    try:
        try:
            identity["source_sha256"] = file_sha256(source)
        except OSError as error:
            raise AudioEvidenceError("AUDIO_SOURCE_UNAVAILABLE") from error
        cached = get_audio_evidence(db, identity["source_sha256"])
        if cached is not None:
            if file_sha256(source) != identity["source_sha256"]:
                raise AudioEvidenceError("AUDIO_SOURCE_CHANGED")
            return cached
        samples, source_duration, original_channels = _decode_audio(source)
        truncated = len(samples) > MAX_ANALYSIS_SECONDS*SAMPLE_RATE
        samples = samples[:int(MAX_ANALYSIS_SECONDS*SAMPLE_RATE)]
        duration = len(samples)/SAMPLE_RATE
        if duration < MIN_ANALYSIS_SECONDS:
            raise AudioEvidenceError("AUDIO_TOO_SHORT")
        rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
        peak = float(np.max(np.abs(samples)))
        if rms < .0001:
            raise AudioEvidenceError("AUDIO_SILENT")
        frame = int(.4*SAMPLE_RATE)
        levels = [_db(np.sqrt(np.mean(np.square(samples[i:i+frame], dtype=np.float64))))
                  for i in range(0, len(samples), frame)]
        span = min(3.0, duration)
        middle = max(0, (duration-span)/2)
        result = {**identity, "status": "completed",
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "coverage": {"source_duration_seconds": source_duration,
                "analyzed_start_seconds": 0, "analyzed_end_seconds": round(duration, 4),
                "analyzed_seconds": round(duration, 4), "truncated": truncated,
                "fraction": round(min(1, duration/source_duration), 6) if source_duration else None,
                "scope": "first_audio_stream_prefix"},
            "pcm": {"sample_rate_hz": SAMPLE_RATE, "channels": samples.shape[1],
                "source_channels": original_channels, "format": "float32",
                "downmixed": original_channels > 2},
            "metrics": {"rms_dbfs": _db(rms), "sample_peak_dbfs": _db(peak),
                "crest_factor_db": round(_db(peak)-_db(rms), 3),
                "dynamic_range_db": round(float(np.percentile(levels, 95)-np.percentile(levels, 10)), 3),
                "dynamic_range_method": "400ms_rms_dbfs_p95_minus_p10_floor_minus120"},
            "energy_windows": {"opening": _window(samples, 0, span),
                "middle": _window(samples, middle, middle+span),
                "ending": _window(samples, duration-span, duration)},
            "energy_segments": [_window(samples, start, min(duration, start+10))
                for start in range(0, math.ceil(duration), 10)],
            "tempo": _tempo(samples), "source_music": "unknown", "vocals": "unknown",
            "limitations": ["仅本地PCM客观度量；没有试听、曲风、情绪、人声或原声配乐识别",
                "RMS不是LUFS；sample_peak是16kHz解码PCM采样峰值，不是源采样率峰值或true peak",
                "动态范围为400ms能量分位差，不是EBU响度范围；静音度量下限为-120dBFS",
                "节拍仅为能量起音周期估计，置信度是规则得分而非统计概率，可能存在半倍或双倍歧义",
                "所有窗口包括ending均相对于已分析范围，截断后不代表源文件结尾",
                "证据不代表适合当前视频，音乐适配仍需结合本片语义与人工预览"]}
        if file_sha256(source) != identity["source_sha256"]:
            raise AudioEvidenceError("AUDIO_SOURCE_CHANGED")
        with db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO packaging_audio_evidence "
                "(source_sha256,method_version,evidence_json,analyzed_at) VALUES(?,?,?,?)",
                (identity["source_sha256"], METHOD_VERSION,
                 json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")), result["analyzed_at"]))
        return get_audio_evidence(db, identity["source_sha256"])
    except (AudioEvidenceError, OSError, ValueError, RuntimeError) as error:
        code = str(error) if isinstance(error, AudioEvidenceError) else "AUDIO_DECODE_FAILED"
        from .tools import ToolNotFound
        if isinstance(error, ToolNotFound):
            code = "AUDIO_TOOL_UNAVAILABLE"
        return {**identity, "status": "failed", "error_code": code,
            "error": ERRORS.get(code, "音频分析失败"), "source_music": "unknown", "vocals": "unknown"}
