"""Resolve task-authorized visual media for the local rough-cut workbench."""

from __future__ import annotations

from dataclasses import dataclass
import json
import mimetypes
import os
from pathlib import Path
import subprocess
from urllib.parse import quote

from .image_probe import probe_image
from .preview import cache_root_for, source_signature
from .probe import probe_media
from .task_sources import scoped_asset_location
from .tools import (
    ToolNotFound,
    default_ffprobe_search_roots,
    find_ffmpeg,
    find_ffprobe,
)


_PURPOSES = {"thumbnail", "preview"}
_VISUAL_KINDS = {"image", "video"}
_BROWSER_CONTENT_TYPES = {
    "image": {"image/avif", "image/bmp", "image/gif", "image/jpeg", "image/png", "image/webp"},
    "video": {"video/mp4", "video/ogg", "video/webm"},
}
_BROWSER_VIDEO_CODECS = {"av1", "h264", "theora", "vp8", "vp9"}


@dataclass(frozen=True)
class RoughMediaFile:
    path: Path
    media_kind: str
    delivery: str


class RoughMediaError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        public_message: str,
        details: dict | None = None,
    ):
        super().__init__(f"{code}: {public_message}")
        self.status = status
        self.code = code
        self.public_message = public_message
        self.details = details


def _require_task(db, task_id: str) -> None:
    row = db.connection.execute(
        "SELECT 1 FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "当前任务不能访问该素材。")


def _asset_is_authorized(db, task_id: str, asset_id: str) -> bool:
    _require_task(db, task_id)
    selected = db.connection.execute(
        "SELECT 1 FROM scene_cards WHERE task_id=? AND asset_id=? LIMIT 1",
        (task_id, asset_id),
    ).fetchone()
    if selected is not None:
        return True
    try:
        return scoped_asset_location(db, task_id, asset_id) is not None
    except ValueError as error:
        raise RoughMediaError(
            404,
            "ROUGH_MEDIA_NOT_ALLOWED",
            "当前任务不能访问该素材。",
        ) from error


def _inside(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _available_source(db, asset_id: str) -> tuple[Path, str]:
    asset = db.connection.execute(
        "SELECT media_kind FROM assets WHERE asset_id=?",
        (asset_id,),
    ).fetchone()
    if asset is None:
        raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "当前任务不能访问该素材。")
    media_kind = str(asset["media_kind"])
    if media_kind not in _VISUAL_KINDS:
        raise RoughMediaError(415, "ROUGH_MEDIA_UNSUPPORTED", "该素材不是可预览的图片或视频。")

    locations = db.connection.execute(
        """
        SELECT l.mount_path,l.online,l.kind,al.relative_path
        FROM asset_locations AS al
        JOIN libraries AS l USING(library_id)
        WHERE al.asset_id=? AND al.state='available'
        ORDER BY l.online DESC, CASE l.kind WHEN 'local' THEN 0 ELSE 1 END,
                 al.location_id
        """,
        (asset_id,),
    ).fetchall()
    escaped = False
    for location in locations:
        if not bool(location["online"]):
            continue
        root = Path(location["mount_path"]).resolve()
        if not root.is_dir():
            continue
        candidate = (root / str(location["relative_path"])).resolve()
        if not _inside(root, candidate):
            escaped = True
            continue
        if candidate.is_file():
            return candidate, media_kind
    if escaped:
        raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "素材位置超出登记目录。")
    raise RoughMediaError(409, "ROUGH_MEDIA_OFFLINE", "素材文件当前不可访问。")


def _preview_record(db, asset_id: str) -> dict | None:
    row = db.connection.execute(
        """
        SELECT result_json FROM analysis_runs
        WHERE asset_id=? AND stage='preview' AND status='completed'
        """,
        (asset_id,),
    ).fetchone()
    if row is None or not row["result_json"]:
        return None
    try:
        value = json.loads(row["result_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_cache_path(db, asset_id: str, kind: str) -> Path | None:
    row = db.connection.execute(
        "SELECT rel_path FROM preview_cache WHERE asset_id=? AND kind=?",
        (asset_id, kind),
    ).fetchone()
    record = _preview_record(db, asset_id)
    if row is None or record is None:
        return None
    try:
        current_signature = source_signature(db, asset_id)
    except (OSError, RuntimeError):
        return None
    if record.get("source_signature") != current_signature:
        return None
    root = cache_root_for(db).resolve()
    candidate = (root / str(row["rel_path"])).resolve()
    if not _inside(root, candidate) or not candidate.is_file():
        return None
    return candidate


def resolve_rough_media(
    db,
    task_id: str,
    asset_id: str,
    purpose: str,
) -> RoughMediaFile:
    if purpose not in _PURPOSES:
        raise RoughMediaError(404, "ROUGH_MEDIA_UNSUPPORTED", "不支持该预览用途。")
    if not _asset_is_authorized(db, task_id, asset_id):
        raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "当前任务不能访问该素材。")
    source, media_kind = _available_source(db, asset_id)
    cache_kind = (
        "thumbnail"
        if purpose == "thumbnail" or media_kind == "image"
        else "proxy"
    )
    cached = _valid_cache_path(db, asset_id, cache_kind)
    if cached is not None:
        return RoughMediaFile(cached, media_kind, "cache")
    return RoughMediaFile(source, media_kind, "source")


def rough_media_content_type(media: RoughMediaFile) -> str:
    content_type = mimetypes.guess_type(media.path.name)[0] or ""
    if content_type not in _BROWSER_CONTENT_TYPES[media.media_kind]:
        raise RoughMediaError(
            415,
            "ROUGH_MEDIA_UNSUPPORTED",
            "素材文件格式与登记类型不一致，或浏览器不支持该格式。",
        )
    return content_type


def _media_url(task_id: str, asset_id: str, purpose: str) -> str:
    return "/api/tasks/{}/rough-media/{}/{}".format(
        quote(str(task_id), safe=""),
        quote(str(asset_id), safe=""),
        purpose,
    )


def describe_rough_media(db, task_id: str, asset_id: str) -> dict:
    try:
        preview = resolve_rough_media(db, task_id, asset_id, "preview")
        resolve_rough_media(db, task_id, asset_id, "thumbnail")
        rough_media_content_type(preview)
        preview_version = source_signature(db, asset_id)
    except RoughMediaError as error:
        row = db.connection.execute(
            "SELECT media_kind FROM assets WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        return {
            "media_kind": None if row is None else row["media_kind"],
            "thumbnail_url": None,
            "preview_url": None,
            "preview_delivery": None,
            "preview_available": False,
            "preview_problem": error.public_message,
            "preview_version": None,
        }
    return {
        "media_kind": preview.media_kind,
        "thumbnail_url": _media_url(task_id, asset_id, "thumbnail"),
        "preview_url": _media_url(task_id, asset_id, "preview"),
        "preview_delivery": preview.delivery,
        "preview_available": True,
        "preview_problem": None,
        "preview_version": preview_version,
    }


def assert_picture_media_available(
    db,
    task_id: str,
    expected_versions: dict | None = None,
    *,
    image_probe_fn=probe_image,
    video_probe_fn=probe_media,
    video_decode_fn=None,
    ffprobe_path: Path | None = None,
    ffmpeg_path: Path | None = None,
) -> None:
    _require_task(db, task_id)
    cards = db.connection.execute(
        """
        SELECT sc.card_id,sc.idx,sc.asset_id,sc.narrative_start,sc.enter_tc,
               sc.exit_tc,sc.mix_form,s.start_sec AS segment_start
        FROM scene_cards sc
        LEFT JOIN segments s ON s.segment_id=sc.segment_id
        WHERE sc.task_id=?
        ORDER BY sc.idx,sc.card_id
        """,
        (task_id,),
    ).fetchall()
    checked: set[tuple[str, str, float | None]] = set()
    decode_fn = video_decode_fn or decode_video_frame
    for position, card in enumerate(cards, start=1):
        asset_id = card["asset_id"]
        if not asset_id:
            raise RoughMediaError(
                409,
                "ROUGH_MEDIA_OFFLINE",
                f"第 {position} 镜头：尚未绑定画面素材。",
            )
        try:
            media = resolve_rough_media(db, task_id, str(asset_id), "preview")
            rough_media_content_type(media)
            version = source_signature(db, str(asset_id))
            if expected_versions is not None:
                shown_version = expected_versions.get(str(card["card_id"]))
                if not isinstance(shown_version, str) or not shown_version:
                    raise RoughMediaError(
                        409,
                        "ROUGH_MEDIA_VERSION_REQUIRED",
                        "请刷新粗剪画面并重新逐镜确认。",
                    )
                if shown_version != version:
                    raise RoughMediaError(
                        409,
                        "ROUGH_MEDIA_VERSION_CHANGED",
                        "素材预览版本已变化，请刷新后重新确认。",
                    )
                decode_at = _decode_target_seconds(card) if media.media_kind == "video" else None
                identity = (str(asset_id), version, decode_at)
                if identity not in checked:
                    _assert_decodable_media(
                        media,
                        image_probe_fn=image_probe_fn,
                        video_probe_fn=video_probe_fn,
                        video_decode_fn=decode_fn,
                        ffprobe_path=ffprobe_path,
                        ffmpeg_path=ffmpeg_path,
                        decode_at=decode_at or 0.0,
                    )
                    checked.add(identity)
        except RoughMediaError as error:
            raise RoughMediaError(
                error.status,
                error.code,
                f"第 {position} 镜头：{error.public_message}",
                {
                    **(error.details or {}),
                    "assetId": str(asset_id),
                    "cardId": str(card["card_id"]),
                    "position": position,
                },
            ) from error


def _assert_decodable_media(
    media: RoughMediaFile,
    *,
    image_probe_fn,
    video_probe_fn,
    video_decode_fn,
    ffprobe_path: Path | None,
    ffmpeg_path: Path | None,
    decode_at: float,
) -> None:
    try:
        if media.media_kind == "image":
            image_probe_fn(media.path)
            return
        actual_ffprobe = ffprobe_path or find_ffprobe(
            os.environ,
            search_roots=default_ffprobe_search_roots(),
        )
        result = video_probe_fn(media.path, actual_ffprobe)
        streams = result.get("streams") if isinstance(result, dict) else None
        video_streams = [
            stream for stream in (streams or [])
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ]
        if not video_streams:
            raise RuntimeError("没有可解码的视频轨")
        codecs = {str(stream.get("codec_name") or "").lower() for stream in video_streams}
        if not codecs.intersection(_BROWSER_VIDEO_CODECS):
            raise RoughMediaError(
                415,
                "ROUGH_MEDIA_UNSUPPORTED",
                "当前视频编码浏览器不支持，请先生成代理预览。",
            )
        actual_ffmpeg = ffmpeg_path or find_ffmpeg(
            os.environ,
            search_roots=default_ffprobe_search_roots(),
        )
        video_decode_fn(media.path, actual_ffmpeg, decode_at)
    except RoughMediaError:
        raise
    except (OSError, RuntimeError, ValueError, ToolNotFound, subprocess.TimeoutExpired) as error:
        raise RoughMediaError(
            415,
            "ROUGH_MEDIA_UNSUPPORTED",
            "素材无法解码，请重新生成预览或更换素材。",
        ) from error


def _decode_target_seconds(card) -> float:
    enter = card["enter_tc"]
    exit_ = card["exit_tc"]
    if isinstance(enter, (int, float)) and isinstance(exit_, (int, float)) and exit_ > enter:
        return float(enter)
    if card["mix_form"] == "数字人全屏":
        return max(0.0, float(card["narrative_start"] or 0.0))
    return max(0.0, float(card["segment_start"] or 0.0))


def decode_video_frame(
    path: Path,
    ffmpeg_path: Path,
    at_seconds: float,
    runner=subprocess.run,
) -> None:
    completed = runner(
        [
            str(ffmpeg_path),
            "-v",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{max(0.0, float(at_seconds)):.6f}",
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        text=False,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr or "视频帧解码失败"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(str(detail)[-2000:])
    if not completed.stdout:
        raise RuntimeError("没有解码出目标视频帧")
