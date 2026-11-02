"""Technical ffprobe analysis with resumable per-asset persistence."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess

from .image_probe import probe_image
from .models import ScanSummary
from .path_utils import filesystem_path


PENDING_COUNT_SQL = """
SELECT count(*)
FROM assets a
JOIN asset_locations al USING(asset_id)
JOIN libraries l USING(library_id)
LEFT JOIN analysis_runs ar
  ON ar.asset_id=a.asset_id AND ar.stage='technical_probe'
WHERE al.library_id=?
  AND al.state='available'
  AND l.online=1
  AND (ar.status IS NULL OR ar.status IN ('failed', 'interrupted', 'stale'))
"""

PENDING_BATCH_SQL = """
SELECT a.asset_id, a.media_kind, l.mount_path, al.relative_path
FROM assets a
JOIN asset_locations al USING(asset_id)
JOIN libraries l USING(library_id)
LEFT JOIN analysis_runs ar
  ON ar.asset_id=a.asset_id AND ar.stage='technical_probe'
WHERE al.library_id=?
  AND al.state='available'
  AND l.online=1
  AND (ar.status IS NULL OR ar.status IN ('failed', 'interrupted', 'stale'))
  AND (al.relative_path > ? OR (al.relative_path = ? AND a.asset_id > ?))
ORDER BY al.relative_path, a.asset_id
LIMIT ?
"""


def probe_media(
    path: Path,
    ffprobe_path: Path,
    runner: Callable[..., object] = subprocess.run,
) -> dict:
    completed = runner(
        [
            str(ffprobe_path),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "ffprobe 执行失败")[-2000:])
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe 返回了无法解析的结果") from error
    if not isinstance(result, dict):
        raise RuntimeError("ffprobe 返回结果格式不正确")
    return result


def probe_pending_assets(
    db,
    library_id: str,
    ffprobe_path: Path | None,
    runner: Callable[..., object] = subprocess.run,
    *,
    image_probe_fn: Callable[[Path], dict] = probe_image,
    workers: int = 1,
    batch_size: int = 64,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> ScanSummary:
    if not 1 <= workers <= 4:
        raise ValueError("workers 必须在 1 到 4 之间")
    if not 1 <= batch_size <= 512:
        raise ValueError("batch_size 必须在 1 到 512 之间")
    discovered = db.connection.execute(
        PENDING_COUNT_SQL,
        (library_id,),
    ).fetchone()[0]
    completed_count = failed_count = 0
    for rows in _pending_batches(db, library_id, batch_size):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _probe_row,
                    row,
                    ffprobe_path,
                    runner,
                    image_probe_fn,
                ): row
                for row in rows
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    result = future.result()
                    status = "completed"
                    result_json = json.dumps(
                        result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    error_summary = None
                    completed_count += 1
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    subprocess.TimeoutExpired,
                ) as error:
                    status = "failed"
                    result_json = None
                    error_summary = _bounded_error(error)
                    failed_count += 1
                _persist_probe_result(
                    db,
                    row["asset_id"],
                    status,
                    result_json,
                    error_summary,
                )
                if on_progress is not None:
                    on_progress(
                        row["relative_path"],
                        completed_count,
                        failed_count,
                    )
    return ScanSummary(
        discovered=discovered,
        completed=completed_count,
        failed=failed_count,
    )


def _pending_batches(db, library_id: str, batch_size: int):
    last_path = ""
    last_asset_id = ""
    while True:
        rows = db.connection.execute(
            PENDING_BATCH_SQL,
            (library_id, last_path, last_path, last_asset_id, batch_size),
        ).fetchall()
        if not rows:
            return
        yield rows
        last_path = rows[-1]["relative_path"]
        last_asset_id = rows[-1]["asset_id"]


def _probe_row(
    row,
    ffprobe_path: Path | None,
    runner: Callable[..., object],
    image_probe_fn: Callable[[Path], dict],
) -> dict:
    path = filesystem_path(Path(row["mount_path"]) / row["relative_path"])
    if row["media_kind"] == "image":
        return image_probe_fn(path)
    if ffprobe_path is None:
        raise RuntimeError("视频或音频技术探测需要 ffprobe")
    return probe_media(path, ffprobe_path, runner)


def _persist_probe_result(
    db,
    asset_id: str,
    status: str,
    result_json: str | None,
    error_summary: str | None,
) -> None:
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO analysis_runs(
              asset_id, stage, status, result_json, error_summary, updated_at
            ) VALUES(?, 'technical_probe', ?, ?, ?, datetime('now'))
            ON CONFLICT(asset_id, stage) DO UPDATE SET
              status=excluded.status,
              result_json=excluded.result_json,
              error_summary=excluded.error_summary,
              updated_at=excluded.updated_at
            """,
            (asset_id, status, result_json, error_summary),
        )


def _bounded_error(error: BaseException, limit: int = 2000) -> str:
    summary = f"{type(error).__name__}: {error}"
    return summary[-limit:]
