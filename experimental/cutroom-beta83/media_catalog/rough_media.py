"""Resolve task-authorized visual media for the local rough-cut workbench."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess
from urllib.parse import quote

from .image_probe import probe_image
from .preview import cache_root_for
from .probe import probe_media
from .task_sources import scoped_asset_location, list_eligible_assets
from .tools import (
    ToolNotFound,
    default_ffprobe_search_roots,
    find_ffmpeg,
    find_ffprobe,
)


_PURPOSES = {"thumbnail", "preview"}
_PREVIEW_KINDS = {"image", "video", "audio"}
_BROWSER_CONTENT_TYPES = {
    "image": {"image/avif", "image/bmp", "image/gif", "image/jpeg", "image/png", "image/webp"},
    "video": {"video/mp4", "video/ogg", "video/webm"},
    "audio": {"audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/x-wav", "audio/webm"},
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


def _scoped_location(db, task_id: str, asset_id: str) -> dict:
    _require_task(db, task_id)
    try:
        location = scoped_asset_location(db, task_id, asset_id)
    except ValueError as error:
        raise RoughMediaError(
            404,
            "ROUGH_MEDIA_NOT_ALLOWED",
            "当前任务不能访问该素材。",
        ) from error
    if location is not None:
        return location
    digital_source = bound_digital_source_location(db, task_id, asset_id)
    if digital_source is not None:
        return digital_source
    # Old tasks can retain a selected scene without task_source_scopes. Keep
    # their one unambiguous catalog location readable, but never cross an
    # active scope or guess between duplicate locations.
    active_scope = db.connection.execute(
        "SELECT 1 FROM task_source_scopes WHERE task_id=? AND active=1 LIMIT 1",
        (task_id,),
    ).fetchone()
    selected = db.connection.execute(
        "SELECT 1 FROM scene_cards WHERE task_id=? AND asset_id=? LIMIT 1",
        (task_id, asset_id),
    ).fetchone()
    if active_scope is None and selected is not None:
        legacy_locations = db.connection.execute(
            "SELECT library_id,relative_path FROM asset_locations WHERE asset_id=?",
            (asset_id,),
        ).fetchall()
        if len(legacy_locations) == 1:
            return dict(legacy_locations[0])
    raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "当前任务不能访问该素材。")


def bound_digital_source_location(db, task_id: str, asset_id: str) -> dict | None:
    """The narration plate is task-bound even when B-roll uses another scope."""
    task = db.connection.execute(
        """
        SELECT t.brief_json FROM tasks t
        JOIN task_assets ta ON ta.task_id=t.task_id
        WHERE t.task_id=? AND t.task_type='digital_human'
          AND ta.asset_id=? AND ta.role='digital_human'
        """,
        (task_id, asset_id),
    ).fetchone()
    if task is None:
        return None
    try:
        brief = json.loads(task["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(brief, dict):
        return None
    source = brief.get("digital_human_source")
    if not isinstance(source, dict) or source.get("asset_id") != asset_id:
        return None
    library_id = source.get("library_id")
    bound_path = source.get("path")
    if not isinstance(library_id, str) or not isinstance(bound_path, str):
        return None
    bound = Path(bound_path)
    if not bound.is_absolute():
        return None
    rows = db.connection.execute(
        """
        SELECT al.relative_path,l.mount_path
        FROM asset_locations al JOIN libraries l USING(library_id)
        WHERE al.asset_id=? AND al.library_id=? AND al.state='available'
        """,
        (asset_id, library_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    root = Path(rows[0]["mount_path"]).resolve()
    candidate = (root / rows[0]["relative_path"]).resolve()
    if not _inside(root, candidate) or candidate != bound.resolve():
        return None
    return {"library_id": library_id, "relative_path": rows[0]["relative_path"]}


def list_rough_edit_assets(db, task_id: str) -> list[dict]:
    """Supplemental scope plus this task's exact, already-bound narration plate."""
    items = list_eligible_assets(db, task_id)
    present = {item["asset_id"] for item in items}
    for row in db.connection.execute(
        "SELECT a.asset_id,a.media_kind,a.size_bytes,a.quick_fingerprint "
        "FROM task_assets ta JOIN assets a USING(asset_id) "
        "WHERE ta.task_id=? AND ta.role='digital_human'", (task_id,),
    ).fetchall():
        location = bound_digital_source_location(db, task_id, row["asset_id"])
        if location is not None and row["asset_id"] not in present:
            items.insert(0, {**dict(row), **location, "task_role": "digital_human",
                             "source_mode": "primary", "library_name": "当前口播原片"})
    return items


def _inside(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _available_source(db, asset_id: str, library_id: str,
                      relative_path: str) -> tuple[Path, str]:
    asset = db.connection.execute(
        "SELECT media_kind FROM assets WHERE asset_id=?",
        (asset_id,),
    ).fetchone()
    if asset is None:
        raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "当前任务不能访问该素材。")
    media_kind = str(asset["media_kind"])
    if media_kind not in _PREVIEW_KINDS:
        raise RoughMediaError(415, "ROUGH_MEDIA_UNSUPPORTED", "该素材类型不可在浏览器预览。")

    locations = db.connection.execute(
        """
        SELECT l.mount_path,l.online,l.kind,al.relative_path
        FROM asset_locations AS al
        JOIN libraries AS l USING(library_id)
        WHERE al.asset_id=? AND al.library_id=? AND al.relative_path=?
          AND al.state='available'
        ORDER BY l.online DESC, CASE l.kind WHEN 'local' THEN 0 ELSE 1 END,
                 al.location_id
        """,
        (asset_id, library_id, relative_path),
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


def _source_signature_for(db, asset_id: str, source: Path) -> str:
    asset = db.connection.execute(
        "SELECT quick_fingerprint FROM assets WHERE asset_id=?", (asset_id,),
    ).fetchone()
    stat = source.stat()
    return hashlib.sha256(json.dumps(
        [asset["quick_fingerprint"], stat.st_size, stat.st_mtime_ns, stat.st_ino],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def _valid_cache_path(db, asset_id: str, kind: str,
                      source: Path) -> Path | None:
    # The cache is keyed by asset ID; a duplicate location could have been
    # the producer. Stream the exact scoped source in that case.
    location_count = db.connection.execute(
        "SELECT COUNT(*) FROM asset_locations WHERE asset_id=? AND state='available'",
        (asset_id,),
    ).fetchone()[0]
    if location_count != 1:
        return None
    row = db.connection.execute(
        "SELECT rel_path FROM preview_cache WHERE asset_id=? AND kind=?",
        (asset_id, kind),
    ).fetchone()
    record = _preview_record(db, asset_id)
    if row is None or record is None:
        return None
    try:
        current_signature = _source_signature_for(db, asset_id, source)
    except OSError:
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
    location = _scoped_location(db, task_id, asset_id)
    source, media_kind = _available_source(
        db, asset_id, location["library_id"], location["relative_path"],
    )
    return _resolve_rough_media_from_source(db, asset_id, source, media_kind, purpose)


def _resolve_rough_media_from_source(
    db, asset_id: str, source: Path, media_kind: str, purpose: str,
) -> RoughMediaFile:
    """Use the source already checked in this call; each public request checks anew."""
    if purpose == "thumbnail" and media_kind == "audio":
        raise RoughMediaError(415, "ROUGH_MEDIA_UNSUPPORTED", "音频没有画面缩略图。")
    cache_kind = (
        "audio_preview" if media_kind == "audio" else
        "thumbnail" if purpose == "thumbnail" or media_kind == "image" else
        "proxy"
    )
    cached = _valid_cache_path(db, asset_id, cache_kind, source)
    if cached is not None:
        return RoughMediaFile(cached, media_kind, "cache")
    return RoughMediaFile(source, media_kind, "source")


def resolve_catalog_media(db, asset_id: str) -> RoughMediaFile:
    """Serve only the preferred registered catalog location to a local session."""
    from .asset_index import list_asset_index

    page = list_asset_index(db, asset_id=asset_id, limit=1)
    if not page["items"]:
        raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "素材不在当前素材库。")
    item = page["items"][0]
    if item["availability"] != "available":
        raise RoughMediaError(409, "ROUGH_MEDIA_OFFLINE", "素材文件当前不可访问。")
    source, media_kind = _available_source(
        db, asset_id, item["libraryId"], item["relativePath"],
    )
    cache_kind = "audio_preview" if media_kind == "audio" else "thumbnail" if media_kind == "image" else "proxy"
    cached = _valid_cache_path(db, asset_id, cache_kind, source)
    return RoughMediaFile(cached or source, media_kind, "cache" if cached else "source")


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
        location = _scoped_location(db, task_id, asset_id)
        source, media_kind = _available_source(
            db, asset_id, location["library_id"], location["relative_path"],
        )
        preview = _resolve_rough_media_from_source(
            db, asset_id, source, media_kind, "preview",
        )
        _resolve_rough_media_from_source(db, asset_id, source, media_kind, "thumbnail")
        rough_media_content_type(preview)
        preview_version = _source_signature_for(db, asset_id, source)
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
    verify_decoding: bool | None = None,
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
    if expected_versions is not None and set(expected_versions) - {
        str(card["card_id"]) for card in cards
    }:
        raise RoughMediaError(
            409, "ROUGH_MEDIA_VERSION_CHANGED",
            "镜头集合已变化，请刷新粗剪并重新逐镜确认。",
        )
    checked: set[tuple[str, str, float | None]] = set()
    decode_fn = video_decode_fn or decode_video_frame
    request_cache: dict = {}
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
            location = _scoped_location(db, task_id, str(asset_id))
            source, _ = _available_source(
                db, str(asset_id), location["library_id"], location["relative_path"],
            )
            version = _source_signature_for(db, str(asset_id), source)
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
            # None preserves the existing inspected-version behavior; False is
            # the cheap identity-only recheck inside the picture-lock write.
            if (verify_decoding is True or
                    (verify_decoding is None and expected_versions is not None)):
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
                        request_cache=request_cache,
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
    request_cache: dict | None = None,
) -> None:
    try:
        if media.media_kind == "image":
            image_probe_fn(media.path)
            return
        cache = request_cache if request_cache is not None else {}
        if "ffprobe" not in cache:
            cache["ffprobe"] = ffprobe_path or find_ffprobe(
                os.environ, search_roots=default_ffprobe_search_roots())
        stat = media.path.stat()
        probe_key = (media.path, stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if probe_key not in cache:
            cache[probe_key] = video_probe_fn(media.path, cache["ffprobe"])
        result = cache[probe_key]
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
                "该视频需要兼容预览，请先生成本片兼容预览。",
            )
        if "ffmpeg" not in cache:
            cache["ffmpeg"] = ffmpeg_path or find_ffmpeg(
                os.environ, search_roots=default_ffprobe_search_roots())
        video_decode_fn(media.path, cache["ffmpeg"], decode_at)
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
            "-ss",
            f"{max(0.0, float(at_seconds)):.6f}",
            "-i",
            str(path),
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


def selected_rough_assets(db, task_id: str, cards: list[dict]) -> list[dict]:
    """Metadata for used visuals only; task detail must not scan the candidate library."""
    from .asset_index import with_technical_durations
    items=[]
    for asset_id in dict.fromkeys(card.get('asset_id') for card in cards if card.get('asset_id')):
        asset=db.connection.execute('SELECT asset_id,media_kind,size_bytes,quick_fingerprint FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
        if asset is None:continue
        try:location=_scoped_location(db,task_id,asset_id)
        except RoughMediaError:location={}
        role=db.connection.execute('SELECT role FROM task_assets WHERE task_id=? AND asset_id=?',(task_id,asset_id)).fetchone()
        items.append({**dict(asset),**location,'task_role':role[0] if role else None})
    return with_technical_durations(db,items)


def _asset_geometry(db, asset, required):
    from .ad_matching import display_dimensions
    row=db.connection.execute("SELECT status,result_json FROM analysis_runs WHERE asset_id=? AND stage='technical_probe' ORDER BY updated_at DESC LIMIT 1",(asset['asset_id'],)).fetchone()
    status='not_detected' if row is None else 'failed' if row['status']=='failed' else 'not_detected'
    dimensions=None
    if row is not None and row['status']=='completed':
        try:dimensions=display_dimensions(asset['media_kind'],json.loads(row['result_json']))
        except (ValueError,TypeError):pass
        status='ready' if dimensions else 'unsupported'
    width,height=dimensions or (None,None)
    orientation='unknown' if not dimensions else 'square' if width==height else 'landscape' if width>height else 'portrait'
    match=None if required not in {'9:16','16:9'} or not dimensions else orientation==('portrait' if required=='9:16' else 'landscape')
    return {'display_orientation':orientation,'display_width':width,'display_height':height,
            'technical_status':status,'direction_match':match}


def rough_asset_page(db, task_id: str, *, limit=24, offset=0, query='') -> dict:
    from .asset_index import with_technical_durations
    _require_task(db,task_id)
    if type(limit) is not int or not 1<=limit<=48 or type(offset) is not int or offset<0 or not isinstance(query,str) or len(query)>200:
        raise ValueError('ROUGH_ASSET_QUERY_INVALID: 分页或搜索条件无效。')
    task=db.connection.execute('SELECT task_type,brief_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    digital=task['task_type']=='digital_human'
    try:required=json.loads(task['brief_json'] or '{}').get('aspect_ratio') if not digital else None
    except (ValueError,TypeError,AttributeError):required=None
    if required not in {'9:16','16:9'}:required=None
    term=query.strip().casefold()
    items=[a for a in list_rough_edit_assets(db,task_id)
           if a.get('media_kind') in {'video','image'} and (digital or a.get('task_role')!='digital_human')
           and (not term or term in str(a.get('relative_path','')).casefold() or term in str(a.get('asset_id','')).casefold())]
    total=len(items)
    page=with_technical_durations(db,items[offset:offset+limit])
    return {'items':[{**a,**describe_rough_media(db,task_id,a['asset_id']),**_asset_geometry(db,a,required)} for a in page],
            'required_aspect_ratio':required,'task_type':task['task_type'],
            'total':total,'limit':limit,'offset':offset,'hasMore':offset+len(page)<total,'query':query}