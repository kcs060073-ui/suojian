"""P1-B 音频分析：人声区间（VAD）+ 粗转写 + 响度异常。

跑在已技术探测完成的视频/音频素材上。先抽低采样音频，再用 faster_whisper
（内置 VAD）得到"哪几秒在说话"和大致文字，顺带用 volumedetect 标出过响/过轻。
转写文本留在 analysis_runs，供隐私标记（联系方式）复用，不自动打码。
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import re
import sqlite3
import subprocess

from .path_utils import filesystem_path

QUIET_DB = -35.0
LOUD_DB = -8.0
TRANSCRIPT_WAV_RATE = 16000

CONTACT_PATTERN = re.compile(
    r"(?P<phone>1[3-9]\d{9})"
    r"|(?P<wechat>(微信|vx|Vx|WX|薇信|微信号)[\s:：]?\S{3,20})"
    r"|(?P<email>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)


def _load_model(model_size: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:  # pragma: no cover - 依赖缺失时给出可执行提示
        raise RuntimeError(
            "未安装 faster_whisper，无法做粗转写。请先 pip install faster_whisper"
        ) from error
    return WhisperModel(model_size, device="cpu", compute_type="int8")


def cache_root_for(db) -> Path:
    return db.path.parent / "cache"


def _asset_source(db, asset_id: str):
    row = db.connection.execute(
        """
        SELECT l.mount_path, al.relative_path, a.media_kind
        FROM asset_locations al
        JOIN libraries l USING(library_id)
        JOIN assets a USING(asset_id)
        WHERE al.asset_id=? AND al.state='available'
        """,
        (asset_id,),
    ).fetchone()
    return None if row is None else (row["mount_path"], row["relative_path"], row["media_kind"])


def _extract_audio(
    ffmpeg: Path, source: Path, out: Path, runner: Callable[..., object]
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    completed = runner(
        [
            str(ffmpeg),
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ar",
            str(TRANSCRIPT_WAV_RATE),
            "-ac",
            "1",
            str(out),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "音频抽取失败")[-2000:])


def _mean_volume_db(ffmpeg: Path, wav: Path, runner: Callable[..., object]) -> float | None:
    completed = runner(
        [str(ffmpeg), "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    for line in (completed.stderr or "").splitlines():
        if "mean_volume" in line:
            match = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", line)
            if match:
                return float(match.group(1))
    return None


def _transcribe(model, wav: Path) -> tuple[list[dict], str, float]:
    segments, info = model.transcribe(
        str(wav),
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=5,
    )
    out_segments = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            out_segments.append(
                {"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text}
            )
    language = getattr(info, "language", "") or ""
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    return out_segments, language, duration


def _scan_contacts(text: str) -> list[dict]:
    hits = []
    for match in CONTACT_PATTERN.finditer(text):
        kind = next(k for k, v in match.groupdict().items() if v)
        hits.append({"type": kind, "value": match.group(0)})
    return hits


def analyze_asset_audio(
    db,
    asset_id: str,
    model,
    ffmpeg: Path,
    runner: Callable[..., object] = subprocess.run,
) -> dict:
    info = _asset_source(db, asset_id)
    if info is None:
        raise RuntimeError(f"素材当前不可访问：{asset_id}")
    mount_path, relative_path, media_kind = info
    if media_kind not in ("video", "audio"):
        raise RuntimeError(f"素材类型 {media_kind} 没有音轨")
    source = filesystem_path(Path(mount_path) / relative_path)
    cache_root = cache_root_for(db)
    result = _analyze_audio_files(asset_id, source, cache_root, model, ffmpeg, runner)
    _persist(db, asset_id, result)
    return result


def _analyze_audio_files(
    asset_id: str,
    source: Path,
    cache_root: Path,
    model,
    ffmpeg: Path,
    runner: Callable[..., object],
) -> dict:
    # 纯文件 I/O + 模型推理，不碰数据库；可在工作线程内安全调用
    wav = cache_root / f"{asset_id}.transcribe.wav"
    _extract_audio(ffmpeg, source, wav, runner)
    try:
        segments, language, _duration = _transcribe(model, wav)
    finally:
        # 临时 wav 清理是 best-effort：本机回收站不可用会使 safe-delete 守卫抛异常，
        # 绝不能让清理失败掩盖真实的分析结果（否则 _persist 不执行）。
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass
    text = " ".join(s["text"] for s in segments)
    mean_volume = _mean_volume_standalone(ffmpeg, source, runner)
    loud_anomaly = None
    if mean_volume is not None:
        if mean_volume < QUIET_DB:
            loud_anomaly = "too_quiet"
        elif mean_volume > LOUD_DB:
            loud_anomaly = "too_loud"
    contacts = _scan_contacts(text)
    return {
        "language": language,
        "segments": segments,
        "speech_seconds": round(sum(s["end"] - s["start"] for s in segments), 2),
        "mean_volume_db": mean_volume,
        "loud_anomaly": loud_anomaly,
        "contact_hits": contacts,
    }


def _mean_volume_standalone(ffmpeg: Path, source: Path, runner: Callable[..., object]) -> float | None:
    # 直接对原片测响度，避免重复抽音频
    completed = runner(
        [str(ffmpeg), "-i", str(source), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    for line in (completed.stderr or "").splitlines():
        if "mean_volume" in line:
            match = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", line)
            if match:
                return float(match.group(1))
    return None


def _persist(db, asset_id: str, result: dict) -> None:
    import json

    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO analysis_runs(asset_id, stage, status, result_json, updated_at)
            VALUES(?, 'audio_analysis', 'completed', ?, datetime('now'))
            ON CONFLICT(asset_id, stage) DO UPDATE SET
              status='completed', result_json=excluded.result_json, updated_at=datetime('now')
            """,
            (asset_id, json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
        )


def analyze_audio_pending(
    db,
    library_id: str,
    ffmpeg: Path,
    runner: Callable[..., object] = subprocess.run,
    *,
    model_size: str = "tiny",
    workers: int = 1,
    batch_size: int = 16,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> object:
    if not 1 <= workers <= 4:
        raise ValueError("workers 必须在 1 到 4 之间")
    cache_root = cache_root_for(db)
    rows = db.connection.execute(
        """
        SELECT a.asset_id, a.media_kind, l.mount_path, al.relative_path
        FROM assets a
        JOIN asset_locations al USING(asset_id)
        JOIN libraries l USING(library_id)
        LEFT JOIN analysis_runs ar
          ON ar.asset_id=a.asset_id AND ar.stage='audio_analysis'
        WHERE al.library_id=? AND al.state='available' AND l.online=1
          AND a.media_kind IN ('video', 'audio')
          AND (ar.status IS NULL OR ar.status IN ('failed','interrupted','stale'))
        """,
        (library_id,),
    ).fetchall()
    discovered = len(rows)
    completed_count = failed_count = 0
    model = _load_model(model_size)
    for batch in _chunks(list(rows), batch_size):
        # 主线程读取素材信息（sqlite 连接只能在创建它的线程用）
        work: list[tuple] = []
        for row in batch:
            info = _asset_source(db, row["asset_id"])
            if info is None:
                continue
            mount_path, relative_path, _media_kind = info
            source = filesystem_path(Path(mount_path) / relative_path)
            work.append((row["asset_id"], source, cache_root, relative_path))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for (aid, src, croot, rel) in work:
                fut = executor.submit(
                    _analyze_audio_files, aid, src, croot, model, ffmpeg, runner
                )
                futures[fut] = (aid, rel)
            for future in as_completed(futures):
                aid, rel = futures[future]
                try:
                    result = future.result()
                    _persist(db, aid, result)  # 主线程写回
                    completed_count += 1
                except (OSError, RuntimeError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                    failed_count += 1
                    _mark_failed(db, aid, str(error)[:2000])
                if on_progress is not None:
                    on_progress(rel, completed_count, failed_count)
    from .models import ScanSummary

    return ScanSummary(discovered=discovered, completed=completed_count, failed=failed_count)


def _mark_failed(db, asset_id: str, error_summary: str) -> None:
    import json

    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO analysis_runs(asset_id, stage, status, error_summary, updated_at)
            VALUES(?, 'audio_analysis', 'failed', ?, datetime('now'))
            ON CONFLICT(asset_id, stage) DO UPDATE SET
              status='failed', error_summary=excluded.error_summary, updated_at=datetime('now')
            """,
            (asset_id, error_summary),
        )


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
