"""P1-A 预览缓存层：缩略图 / 接触表 / 按需代理 / 音频预览。

所有产物只落在缓存目录（db 同级 cache/），都是可重建的派生文件，
绝不修改或复制原片。缓存索引写入 preview_cache 表，便于安全清理与续跑。
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import shutil
import hashlib
import json
import sqlite3
import subprocess
import tempfile

from PIL import Image

from .image_probe import probe_image
from .models import ScanSummary
from .path_utils import filesystem_path


THUMB_MAX_W = 480
CONTACT_COLS = 4
CONTACT_W = 320
CONTACT_H = 180
PROXY_MAX_H = 480
PROXY_CRF = 28
PROXY_AUDIO_KB = 64
AUDIO_PREVIEW_KB = 48
AUDIO_PREVIEW_RATE = 22050

ALL_KINDS = ("thumbnail", "contact_sheet", "proxy", "audio_preview")
DEFAULT_LIBRARY_KINDS = {"thumbnail", "contact_sheet", "audio_preview"}
VIDEO_KINDS = {"contact_sheet", "proxy"}
MEDIA_KIND_FOR_KIND = {
    "thumbnail": {"image", "video"},
    "contact_sheet": {"video"},
    "proxy": {"video"},
    "audio_preview": {"video", "audio"},
}


def cache_root_for(db) -> Path:
    return db.path.parent / "cache"


def _asset_source(db, asset_id: str) -> tuple[str, str, str] | None:
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
    if row is None:
        return None
    return row["mount_path"], row["relative_path"], row["media_kind"]


def _source_signature(db, asset_id: str) -> str:
    info = _asset_source(db, asset_id)
    if info is None:
        raise RuntimeError(f"素材当前不可访问：{asset_id}")
    source = filesystem_path(Path(info[0]) / info[1])
    stat = source.stat()
    row = db.connection.execute('SELECT quick_fingerprint FROM assets WHERE asset_id=?', (asset_id,)).fetchone()
    return hashlib.sha256(_json([row[0], stat.st_size, stat.st_mtime_ns, stat.st_ino]).encode()).hexdigest()


def source_signature(db, asset_id: str) -> str:
    """Return the source identity used to validate rebuildable preview files."""
    return _source_signature(db, asset_id)


def _duration_of(db, asset_id: str) -> float | None:
    row = db.connection.execute(
        """
        SELECT result_json FROM analysis_runs
        WHERE asset_id=? AND stage='technical_probe' AND status='completed'
        """,
        (asset_id,),
    ).fetchone()
    if row is None or not row["result_json"]:
        return None
    import json

    try:
        data = json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    fmt = data.get("format")
    if isinstance(fmt, dict):
        duration = fmt.get("duration")
        if isinstance(duration, (int, float)):
            return float(duration)
    for stream in data.get("streams", []) or []:
        if isinstance(stream, dict) and stream.get("duration"):
            try:
                return float(stream["duration"])
            except (TypeError, ValueError):
                continue
    return None


def _has_audio(db, asset_id: str) -> bool:
    row = db.connection.execute(
        """
        SELECT result_json FROM analysis_runs
        WHERE asset_id=? AND stage='technical_probe' AND status='completed'
        """,
        (asset_id,),
    ).fetchone()
    if row is None or not row["result_json"]:
        return True  # 未知时假定有音轨，交给 ffmpeg 报错
    import json

    try:
        data = json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return True
    for stream in data.get("streams", []) or []:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            return True
    return False


def _run_ffmpeg(
    ffmpeg: Path,
    args: list[str],
    runner: Callable[..., object] = subprocess.run,
) -> None:
    completed = runner(
        [str(ffmpeg), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "ffmpeg 执行失败")[-2000:])


def make_image_thumbnail(source: Path, out: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        if image.width > THUMB_MAX_W:
            ratio = THUMB_MAX_W / image.width
            image = image.resize((THUMB_MAX_W, max(1, int(image.height * ratio))))
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(out, "JPEG", quality=82)


def make_video_thumbnail(
    ffmpeg: Path,
    source: Path,
    out: Path,
    duration: float | None,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    at = max(0.0, (duration or 0.0) * 0.1)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-ss",
            f"{at:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({THUMB_MAX_W},iw)':-2",
            str(out),
        ],
        runner,
    )


def make_contact_sheet(
    ffmpeg: Path,
    source: Path,
    out: Path,
    duration: float | None,
    frames: int = 12,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    if not duration or duration < 1:
        raise RuntimeError("视频时长未知，无法生成接触表")
    fps = frames / duration
    work = out.parent / f".cs_{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-ss",
            "0",
            "-i",
            str(source),
            "-vf",
            f"fps={fps:.6f},scale={CONTACT_W}:{CONTACT_H}",
            "-frames:v",
            str(frames),
            str(work / "cs_%03d.jpg"),
        ],
        runner,
    )
    frames_files = sorted(work.glob("cs_*.jpg"))
    if not frames_files:
        raise RuntimeError("接触表抽帧失败")
    cols = CONTACT_COLS
    rows = (len(frames_files) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * CONTACT_W, rows * CONTACT_H), "white")
    for i, fpath in enumerate(frames_files):
        with Image.open(fpath) as frame:
            frame = frame.convert("RGB").resize((CONTACT_W, CONTACT_H))
            canvas.paste(frame, ((i % cols) * CONTACT_W, (i // cols) * CONTACT_H))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "JPEG", quality=82)
    shutil.rmtree(work, ignore_errors=True)


def make_proxy(
    ffmpeg: Path,
    source: Path,
    out: Path,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-i",
            str(source),
            "-vf",
            f"scale='min({PROXY_MAX_H},ih)':-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(PROXY_CRF),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            f"{PROXY_AUDIO_KB}k",
            "-movflags",
            "+faststart",
            str(out),
        ],
        runner,
    )


def make_audio_preview(
    ffmpeg: Path,
    source: Path,
    out: Path,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ar",
            str(AUDIO_PREVIEW_RATE),
            "-b:a",
            f"{AUDIO_PREVIEW_KB}k",
            str(out),
        ],
        runner,
    )


def generate_asset_previews(
    db,
    asset_id: str,
    ffmpeg: Path,
    kinds: set[str] | None = None,
    runner: Callable[..., object] = subprocess.run,
    cache_root: Path | None = None,
) -> dict:
    target = set(kinds) if kinds else set(ALL_KINDS)
    info = _asset_source(db, asset_id)
    if info is None:
        raise RuntimeError(f"素材当前不可访问：{asset_id}")
    mount_path, relative_path, media_kind = info
    source = filesystem_path(Path(mount_path) / relative_path)
    cache_root = cache_root or cache_root_for(db)
    produced: dict[str, str] = {}
    signature = _source_signature(db, asset_id)
    cache_key = asset_id + "." + signature[:16]
    duration = _duration_of(db, asset_id)

    if "thumbnail" in target and media_kind in MEDIA_KIND_FOR_KIND["thumbnail"]:
        out = cache_root / f"{cache_key}.thumb.jpg"
        if media_kind == "image":
            make_image_thumbnail(source, out)
        else:
            make_video_thumbnail(ffmpeg, source, out, duration, runner)
        produced["thumbnail"] = out.name

    if "contact_sheet" in target and media_kind == "video":
        out = cache_root / f"{cache_key}.contact.jpg"
        make_contact_sheet(ffmpeg, source, out, duration, runner=runner)
        produced["contact_sheet"] = out.name

    if "proxy" in target and media_kind == "video":
        out = cache_root / f"{cache_key}.proxy.mp4"
        make_proxy(ffmpeg, source, out, runner)
        produced["proxy"] = out.name

    if "audio_preview" in target and media_kind in ("video", "audio"):
        if _has_audio(db, asset_id):
            out = cache_root / f"{cache_key}.audioprev.mp3"
            make_audio_preview(ffmpeg, source, out, runner)
            produced["audio_preview"] = out.name
        else:
            produced["audio_preview"] = None  # 原片无音轨，跳过

    if not produced:
        raise RuntimeError(f"素材类型 {media_kind} 不需要所选预览类型")
    _persist_preview(db, asset_id, produced, cache_root, signature)
    return produced


def _persist_preview(db, asset_id: str, produced: dict[str, str], cache_root: Path, signature: str) -> None:
    with db.transaction() as connection:
        if _source_signature(db, asset_id) != signature:
            raise RuntimeError("SOURCE_CHANGED: 预览期间源素材变化，请重新扫描生成")
        result = {
            "source_signature": signature,
            "thumbnail": produced.get("thumbnail"),
            "contact_sheet": produced.get("contact_sheet"),
            "proxy": produced.get("proxy"),
            "audio_preview": produced.get("audio_preview"),
            "generated_at": _now(),
        }
        connection.execute(
            """
            INSERT INTO analysis_runs(
              asset_id, stage, status, result_json, updated_at
            ) VALUES(?, 'preview', 'completed', ?, datetime('now'))
            ON CONFLICT(asset_id, stage) DO UPDATE SET
              status='completed',
              result_json=excluded.result_json,
              updated_at=datetime('now')
            """,
            (asset_id, _json(result)),
        )
        for kind, name in produced.items():
            if not name:
                continue
            full = cache_root / name
            size = full.stat().st_size if full.exists() else None
            connection.execute(
                """
                INSERT INTO preview_cache(asset_id, kind, rel_path, size_bytes)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(asset_id, kind) DO UPDATE SET
                  rel_path=excluded.rel_path,
                  size_bytes=excluded.size_bytes,
                  generated_at=datetime('now')
                """,
                (asset_id, kind, name, size),
            )


def _gen_files(
    asset_id: str,
    ffmpeg: Path,
    source: Path,
    media_kind: str,
    duration: float | None,
    has_audio: bool,
    kinds: set[str],
    cache_root: Path,
    runner: Callable[..., object] = subprocess.run,
) -> dict:
    produced: dict[str, str] = {}
    if "thumbnail" in kinds and media_kind in MEDIA_KIND_FOR_KIND["thumbnail"]:
        out = cache_root / f"{asset_id}.thumb.jpg"
        if media_kind == "image":
            make_image_thumbnail(source, out)
        else:
            make_video_thumbnail(ffmpeg, source, out, duration, runner)
        produced["thumbnail"] = out.name
    if "contact_sheet" in kinds and media_kind == "video":
        out = cache_root / f"{asset_id}.contact.jpg"
        make_contact_sheet(ffmpeg, source, out, duration, runner=runner)
        produced["contact_sheet"] = out.name
    if "proxy" in kinds and media_kind == "video":
        out = cache_root / f"{asset_id}.proxy.mp4"
        make_proxy(ffmpeg, source, out, runner)
        produced["proxy"] = out.name
    if "audio_preview" in kinds and media_kind in ("video", "audio") and has_audio:
        out = cache_root / f"{asset_id}.audioprev.mp3"
        make_audio_preview(ffmpeg, source, out, runner)
        produced["audio_preview"] = out.name
    if not produced:
        raise RuntimeError(f"素材类型 {media_kind} 不需要所选预览类型")
    return produced


def generate_previews(
    db,
    library_id: str,
    ffmpeg: Path,
    runner: Callable[..., object] = subprocess.run,
    *,
    kinds: set[str] | None = None,
    workers: int = 1,
    batch_size: int = 32,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> ScanSummary:
    if not 1 <= workers <= 4:
        raise ValueError("workers 必须在 1 到 4 之间")
    target_kinds = set(kinds) if kinds is not None else set(DEFAULT_LIBRARY_KINDS)
    unknown = target_kinds - set(ALL_KINDS)
    if unknown:
        raise ValueError(f"不支持的预览类型：{sorted(unknown)}")
    cache_root = cache_root_for(db)
    cache_root.mkdir(parents=True, exist_ok=True)

    rows = _eligible_rows(db, library_id, target_kinds)
    pending = [r for r in rows if _missing_kinds(db, r["asset_id"], target_kinds)]
    discovered = len(pending)
    completed_count = failed_count = 0
    for batch in _chunks(pending, batch_size):
        # 主线程读取素材信息（sqlite 连接只能在创建它的线程用）
        work: list[tuple] = []
        for row in batch:
            info = _asset_source(db, row["asset_id"])
            if info is None:
                continue
            mount_path, relative_path, media_kind = info
            source = filesystem_path(Path(mount_path) / relative_path)
            work.append(
                (
                    row["asset_id"],
                    source,
                    media_kind,
                    _duration_of(db, row["asset_id"]),
                    _has_audio(db, row["asset_id"]),
                    row["relative_path"],
                    _source_signature(db, row["asset_id"]),
                )
            )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _gen_files,
                    aid + "." + signature[:16],
                    ffmpeg,
                    src,
                    mk,
                    dur,
                    ha,
                    target_kinds,
                    cache_root,
                    runner,
                ): (aid, rel, signature)
                for (aid, src, mk, dur, ha, rel, signature) in work
            }
            for future in as_completed(futures):
                aid, rel, signature = futures[future]
                try:
                    produced = future.result()
                    _persist_preview(db, aid, produced, cache_root, signature)
                    completed_count += 1
                except (OSError, RuntimeError, subprocess.TimeoutExpired, sqlite3.Error) as error:
                    failed_count += 1
                    _mark_failed(db, aid, str(error)[:2000])
                if on_progress is not None:
                    on_progress(rel, completed_count, failed_count)
    return ScanSummary(discovered=discovered, completed=completed_count, failed=failed_count)


def _eligible_rows(db, library_id: str, kinds: set[str]) -> list:
    return db.connection.execute(
        """
        SELECT a.asset_id, a.media_kind, l.mount_path, al.relative_path
        FROM assets a
        JOIN asset_locations al USING(asset_id)
        JOIN libraries l USING(library_id)
        JOIN analysis_runs ar
          ON ar.asset_id=a.asset_id AND ar.stage='technical_probe' AND ar.status='completed'
        WHERE al.library_id=? AND al.state='available' AND l.online=1
        """,
        (library_id,),
    ).fetchall()


def _missing_kinds(db, asset_id: str, kinds: set[str]) -> set[str]:
    have = {
        r["kind"]
        for r in db.connection.execute(
            "SELECT kind,rel_path FROM preview_cache WHERE asset_id=?", (asset_id,)
        )
        if (cache_root_for(db) / r["rel_path"]).is_file()
    }
    row = db.connection.execute("SELECT status,result_json FROM analysis_runs WHERE asset_id=? AND stage='preview'", (asset_id,)).fetchone()
    try:
        valid = row is not None and row["status"] == "completed" and json.loads(row["result_json"] or "{}").get("source_signature") == _source_signature(db, asset_id)
    except (TypeError, ValueError, OSError, RuntimeError):
        valid = False
    if not valid:
        have.clear()
    need = {
        k
        for k in kinds
        if db.connection.execute(
            "SELECT media_kind FROM assets WHERE asset_id=?", (asset_id,)
        ).fetchone()["media_kind"] in MEDIA_KIND_FOR_KIND[k]
    }
    return need - have


def _mark_failed(db, asset_id: str, error_summary: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO analysis_runs(asset_id, stage, status, error_summary, updated_at)
            VALUES(?, 'preview', 'failed', ?, datetime('now'))
            ON CONFLICT(asset_id, stage) DO UPDATE SET
              status='failed', error_summary=excluded.error_summary, updated_at=datetime('now')
            """,
            (asset_id, error_summary),
        )


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    import datetime

    return datetime.datetime.now().isoformat(timespec="seconds")
