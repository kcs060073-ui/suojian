"""Compile catalog-backed digital-human jobs for the existing render-mix engine."""

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from types import SimpleNamespace

from . import packaging_plan
from .packaging_layers import PackagingTimingContext, compile_packaging_layers
from .tools import ToolNotFound, default_ffprobe_search_roots, find_ffmpeg, find_ffprobe
from .provenance import canonical_digest, latest_gate, selection_digest
from .subtitles import compile_task_ass, bind_external_ass, verify_subtitle_binding


class RenderMixJobError(RuntimeError):
    """Stable, user-facing blocker raised before a render can start."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _blocked(code: str, message: str) -> None:
    raise RenderMixJobError(code, message)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_WINDOWS_SAFE_RENDER_PLAN_PATH_LENGTH = 240


def _default_atr_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return Path("__cutroom_atr_unavailable__")
    return Path(local_app_data) / "PeterAgentRuntime" / "bin" / "atr.exe"


ATR_PATH = _default_atr_path()


_PIP_POSITION = {"x": 0.1, "y": 0.08, "width": 0.8, "height": 0.34}


def _file_signature(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_catalog_asset(db, asset_id: str) -> dict:
    asset = db.connection.execute(
        """
        SELECT asset_id, media_kind, size_bytes, quick_fingerprint, full_sha256
        FROM assets WHERE asset_id=?
        """,
        (asset_id,),
    ).fetchone()
    if asset is None:
        _blocked("ASSET_NOT_FOUND", f"素材未入册：{asset_id}")
    locations = db.connection.execute(
        """
        SELECT al.location_id, al.library_id, al.relative_path, al.mtime_ns,
               l.mount_path, l.kind
        FROM asset_locations al
        JOIN libraries l USING(library_id)
        WHERE al.asset_id=? AND al.state='available' AND l.online=1
        ORDER BY CASE l.kind WHEN 'local' THEN 0 ELSE 1 END,
                 al.library_id, al.relative_path, al.location_id
        """,
        (asset_id,),
    ).fetchall()
    if not locations:
        _blocked("ASSET_LOCATION_UNAVAILABLE", f"素材没有可用库位置：{asset_id}")
    for location in locations:
        relative = Path(location["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            _blocked("ASSET_PATH_INVALID", f"素材位置越界：{asset_id}/{relative}")
        mount_root = Path(location["mount_path"]).resolve()
        path = (mount_root / relative).resolve()
        if not _contained(path, mount_root):
            _blocked("ASSET_PATH_INVALID", f"素材位置越界：{asset_id}/{relative}")
        if path.is_file():
            return {
                "asset_id": asset_id,
                "media_kind": asset["media_kind"],
                "path": str(path),
                "location_id": location["location_id"],
                "library_id": location["library_id"],
                "relative_path": location["relative_path"],
                "catalog_mtime_ns": location["mtime_ns"],
                "catalog_size_bytes": asset["size_bytes"],
                "quick_fingerprint": asset["quick_fingerprint"],
                "full_sha256": asset["full_sha256"],
                **_file_signature(path),
            }
    _blocked("ASSET_FILE_MISSING", f"素材登记为可用但文件不存在：{asset_id}")


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_card_timecode(card: dict) -> None:
    card_id = card["card_id"]
    start = card["narrative_start"]
    end = card["narrative_end"]
    if (
        not _finite_number(start)
        or not _finite_number(end)
        or start < 0
        or end <= start
    ):
        _blocked("INVALID_TIMECODE", f"{card_id} 的成片时间轴非法")


def _validate_segment_timecode(card_id: str, segment: dict) -> tuple[float, float]:
    start = segment["start_sec"]
    end = segment["end_sec"]
    if (
        not _finite_number(start)
        or not _finite_number(end)
        or start < 0
        or end <= start
    ):
        _blocked("INVALID_TIMECODE", f"{card_id} 的素材片段时间轴非法")
    return float(start), float(end)


def _validate_mix_binding(card: dict, asset: dict, role: str | None) -> None:
    mix_form = card["mix_form"]
    media_kind = asset["media_kind"]
    card_id = card["card_id"]
    if role == "digital_human":
        _blocked("DIGITAL_HUMAN_VISUAL_EDIT", f"{card_id} 不得把口播主输入作为插入画面")
    expected = None
    if mix_form == "broll全屏" and media_kind in ("video", "image"):
        expected = "broll"
    elif mix_form == "信息卡" and media_kind == "image":
        expected = "information_card"
    elif mix_form == "画中画" and media_kind == "video":
        expected = "broll"
    elif mix_form == "画中画" and media_kind == "image":
        expected = "information_card"
    if expected is None:
        _blocked(
            "SCENE_MEDIA_KIND_MISMATCH",
            f"{card_id} 的混剪形态 {mix_form} 与媒体类型 {media_kind} 不匹配",
        )
    if role != expected:
        _blocked(
            "SCENE_ASSET_ROLE_MISMATCH",
            f"{card_id} 需要任务素材角色 {expected}，实际为 {role}",
        )


def _validate_digital_anchor(card: dict, asset: dict | None, role: str | None) -> None:
    card_id = card["card_id"]
    if asset is not None and (asset["media_kind"] != "video" or role != "digital_human"):
        _blocked(
            "DIGITAL_HUMAN_ANCHOR_INVALID",
            f"{card_id} 必须绑定 role=digital_human 的视频主输入",
        )
    if card.get("segment_id"):
        _blocked("DIGITAL_HUMAN_ANCHOR_INVALID", f"{card_id} 的口播主轴不能绑定可编辑片段")
    values = (card.get("enter_tc"), card.get("exit_tc"))
    for value in values:
        if value is not None and (not _finite_number(value) or value != 0):
            _blocked("INVALID_TIMECODE", f"{card_id} 的口播主轴源时间窗必须固定为 0/0")


def _source_window(card: dict, asset: dict, segment: dict | None) -> tuple[float, float]:
    """Resolve scene_cards enter/exit as material source_start/source_end."""
    card_id = card["card_id"]
    media_kind = asset["media_kind"]
    raw_start = card["enter_tc"]
    raw_end = card["exit_tc"]
    if raw_start is None and raw_end is None:
        source_start = source_end = 0.0
    elif raw_start is None or raw_end is None:
        _blocked("INVALID_TIMECODE", f"{card_id} 的源时间窗必须同时提供开始和结束")
    elif (
        not _finite_number(raw_start)
        or not _finite_number(raw_end)
        or raw_start < 0
        or raw_end < 0
    ):
        _blocked("INVALID_TIMECODE", f"{card_id} 的源时间窗非法")
    else:
        source_start = float(raw_start)
        source_end = float(raw_end)

    if media_kind == "image":
        if segment is not None or source_start != 0 or source_end != 0:
            _blocked("INVALID_TIMECODE", f"{card_id} 的图片源时间窗必须为 0/0")
        return 0.0, 0.0
    if media_kind != "video":
        _blocked("SCENE_MEDIA_KIND_MISMATCH", f"{card_id} 不是可渲染的画面媒体")

    narrative_duration = float(card["narrative_end"] - card["narrative_start"])
    segment_start = segment_end = None
    if segment is not None:
        segment_start, segment_end = _validate_segment_timecode(card_id, segment)
    if source_start == 0 and source_end == 0:
        source_start = segment_start if segment_start is not None else 0.0
        source_end = source_start + narrative_duration
        if segment_end is not None and source_end > segment_end + 1e-9:
            _blocked("SEGMENT_TOO_SHORT", f"片段时长不足：{card_id}")
    elif source_end <= source_start:
        _blocked("INVALID_TIMECODE", f"{card_id} 的源时间窗顺序非法")
    if source_end - source_start + 1e-9 < narrative_duration:
        _blocked("SOURCE_WINDOW_TOO_SHORT", f"{card_id} 的源时间窗不足以覆盖叙事槽")
    if segment_start is not None and (
        source_start < segment_start - 1e-9 or source_end > segment_end + 1e-9
    ):
        _blocked("SOURCE_WINDOW_OUTSIDE_SEGMENT", f"{card_id} 的源时间窗越过素材片段")
    return source_start, source_end


def _compile_packaging(
    db, plan_id: str, context: PackagingTimingContext
) -> tuple[dict, dict]:
    try:
        compiled = compile_packaging_layers(db, plan_id, context)
    except (RuntimeError, ValueError) as error:
        message = str(error)
        code, separator, detail = message.partition(":")
        _blocked(code, detail.strip() if separator else message)
    return compiled.snapshot, compiled.plan_fields


def _build_render_mix_job(
    db,
    task_id: str,
    output_path: Path,
    ass_path: Path | None,
    *,
    packaging_plan_id: str | None = None,
    require_packaging_gate: bool = True,
    output_profile: dict | None = None,
    allow_existing_output: bool = False,
) -> dict:
    """Validate catalog gates and build a render job without filesystem writes."""
    if not task_id or task_id in (".", "..") or "/" in task_id or "\\" in task_id:
        _blocked("INVALID_TASK_ID", "任务 ID 不能包含路径分隔符")
    output_path = Path(output_path).resolve()
    ass_path = None if ass_path is None else Path(ass_path).resolve()
    if ass_path is not None and not ass_path.is_file():
        _blocked("ASS_FILE_MISSING", f"字幕文件不存在：{ass_path}")
    task = db.connection.execute(
        "SELECT task_id, title, brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None:
        _blocked("TASK_NOT_FOUND", f"任务不存在：{task_id}")
    brief_gate = latest_gate(db, task_id, "brief")
    picture_gate = latest_gate(db, task_id, "picture_lock")
    packaging_gate = (
        latest_gate(db, task_id, "packaging")
        if require_packaging_gate
        else None
    )
    if brief_gate is None:
        _blocked("BRIEF_GATE_MISSING", "任务缺少文案锁定事件")
    try:
        current_brief = json.loads(task["brief_json"] or "{}")
    except json.JSONDecodeError:
        _blocked("BRIEF_INVALID", "当前文案不是有效 JSON")
    if canonical_digest(current_brief) != brief_gate["gate_digest"]:
        _blocked("BRIEF_GATE_DIGEST_MISMATCH", "当前文案在锁定后发生变化")
    if picture_gate is None:
        _blocked("PICTURE_GATE_MISSING", "任务缺少画面锁定事件")
    if require_packaging_gate and packaging_gate is None:
        _blocked("PACKAGING_GATE_MISSING", "任务缺少包装确认事件")

    asset_count = db.connection.execute(
        "SELECT COUNT(*) FROM task_assets WHERE task_id=?", (task_id,)
    ).fetchone()[0]
    if asset_count == 0:
        _blocked("NO_TASK_ASSETS", "任务没有已选素材")

    cards = db.connection.execute(
        """
        SELECT card_id, idx, narrative_start, narrative_end, asset_id, segment_id, mix_form,
               enter_tc, exit_tc, audio_policy_draft, confirm_status
        FROM scene_cards WHERE task_id=? ORDER BY idx, card_id
        """,
        (task_id,),
    ).fetchall()
    if not cards:
        _blocked("NO_SCENE_CARDS", "任务没有场景卡片")
    unconfirmed = [row["card_id"] for row in cards if not row["confirm_status"]]
    if unconfirmed:
        _blocked("SCENE_CARD_UNCONFIRMED", f"场景卡片未确认：{', '.join(unconfirmed)}")

    picture_lock = db.connection.execute(
        "SELECT changed_since_lock,selection_digest FROM picture_lock WHERE task_id=?", (task_id,)
    ).fetchone()
    if picture_lock is None:
        _blocked("PICTURE_LOCK_MISSING", "任务尚未画面锁定")
    if picture_lock["changed_since_lock"]:
        _blocked("PICTURE_LOCK_CHANGED", "画面锁定后内容发生变化")

    if require_packaging_gate:
        try:
            packaging_gate_details = json.loads(
                packaging_gate["details_json"] or "{}"
            )
            gate_plan_id = packaging_gate_details["plan_id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            _blocked("PACKAGING_GATE_INVALID", "包装确认事件没有绑定明确方案")
        required_status = " AND status='confirmed'"
    else:
        if not packaging_plan_id:
            _blocked("PACKAGING_PLAN_REQUIRED", "包装预览必须指定任务方案")
        gate_plan_id = packaging_plan_id
        required_status = " AND status IN ('draft','confirmed')"
    packaging = db.connection.execute(
        f"""
        SELECT plan_id, plan_name, route, confirmed_at, created_at
        FROM packaging_plans
        WHERE task_id=? AND plan_id=?{required_status}
        """,
        (task_id, gate_plan_id),
    ).fetchone()
    if packaging is None:
        code = (
            "PACKAGING_NOT_CONFIRMED"
            if require_packaging_gate
            else "PACKAGING_PLAN_NOT_OWNED"
        )
        _blocked(code, "包装方案不存在、不属于当前任务或状态不可用")

    task_assets = db.connection.execute(
        """
        SELECT ta.asset_id, ta.role
        FROM task_assets ta
        WHERE ta.task_id=?
        ORDER BY CASE WHEN ta.role='digital_human' THEN 0 ELSE 1 END,
                 ta.asset_id
        """,
        (task_id,),
    ).fetchall()
    digital_humans = [row for row in task_assets if row["role"] == "digital_human"]
    if not digital_humans:
        _blocked("DIGITAL_HUMAN_MISSING", "任务没有 role=digital_human 的口播素材")
    if len(digital_humans) != 1:
        _blocked("DIGITAL_HUMAN_COUNT_INVALID", "任务必须恰好有一个 digital_human")
    resolved_assets = {
        row["asset_id"]: _resolve_catalog_asset(db, row["asset_id"])
        for row in task_assets
    }
    roles = {row["asset_id"]: row["role"] for row in task_assets}
    if resolved_assets[digital_humans[0]["asset_id"]]["media_kind"] != "video":
        _blocked("DIGITAL_HUMAN_NOT_VIDEO", "digital_human 必须是视频素材")
    current_selection_digest, selection_state = selection_digest(db, task_id)

    inserts = []
    card_snapshot = []
    for row in cards:
        card = dict(row)
        mix_form = card["mix_form"]
        if mix_form not in ("数字人全屏", "broll全屏", "画中画", "信息卡"):
            _blocked("SCENE_CARD_INVALID", f"场景卡片混剪形态无效：{card['card_id']}")
        _validate_card_timecode(card)
        asset_id = card["asset_id"]
        if asset_id is not None and asset_id not in resolved_assets:
            _blocked(
                "SCENE_ASSET_NOT_IN_TASK",
                f"场景卡片素材未加入任务素材：{card['card_id']}/{asset_id}",
            )
        if mix_form == "数字人全屏":
            _validate_digital_anchor(
                card,
                None if asset_id is None else resolved_assets[asset_id],
                None if asset_id is None else roles.get(asset_id),
            )
        if mix_form != "数字人全屏":
            if asset_id is None:
                _blocked("SCENE_CARD_ASSET_MISSING", f"场景卡片没有素材：{card['card_id']}")
            asset = resolved_assets[asset_id]
            _validate_mix_binding(card, asset, roles.get(asset_id))
            segment = None
            if card["segment_id"]:
                segment_row = db.connection.execute(
                    "SELECT segment_id, asset_id, start_sec, end_sec FROM segments WHERE segment_id=?",
                    (card["segment_id"],),
                ).fetchone()
                if segment_row is None:
                    _blocked("SEGMENT_NOT_FOUND", f"场景卡片片段不存在：{card['card_id']}")
                segment = dict(segment_row)
                if segment["asset_id"] != asset_id:
                    _blocked("SEGMENT_ASSET_MISMATCH", f"片段不属于卡片素材：{card['card_id']}")
            source_start, source_end = _source_window(card, asset, segment)
            insert = {
                "type": "broll" if mix_form == "broll全屏" else (
                    "image" if asset["media_kind"] == "image" else "video"
                ),
                "src": asset["path"],
                "start": float(card["narrative_start"]),
                "end": float(card["narrative_end"]),
                "sourceStart": source_start,
                "sourceEnd": source_end,
                "audio": card["audio_policy_draft"] or "mute",
            }
            if mix_form != "broll全屏":
                insert["position"] = dict(_PIP_POSITION)
            inserts.append(insert)
        card_state = {
            key: card[key]
            for key in (
                "card_id", "idx", "narrative_start", "narrative_end", "asset_id",
                "segment_id", "mix_form", "enter_tc", "exit_tc", "audio_policy_draft", "confirm_status",
            )
        }
        if card.get("segment_id"):
            segment_row = db.connection.execute(
                "SELECT segment_id, asset_id, start_sec, end_sec FROM segments WHERE segment_id=?",
                (card["segment_id"],),
            ).fetchone()
            card_state["segment"] = None if segment_row is None else dict(segment_row)
        card_snapshot.append(card_state)

    if (
        not picture_lock["selection_digest"]
        or picture_lock["selection_digest"] != current_selection_digest
        or picture_gate["gate_digest"] != current_selection_digest
    ):
        _blocked("PICTURE_LOCK_DIGEST_MISMATCH", "当前选择或文件内容与画面锁不一致")

    keywords = current_brief.get("keywords")
    keyword = (
        keywords[0]
        if isinstance(keywords, list) and keywords and isinstance(keywords[0], str)
        else current_brief.get("keyword") or task["title"]
    )
    duration = max(float(card["narrative_end"]) for card in cards)
    packaging_snapshot, packaging_plan_fields = _compile_packaging(
        db,
        packaging["plan_id"],
        PackagingTimingContext(
            duration_seconds=duration,
            title=task["title"],
            keyword=str(keyword),
            voice_mode="original",
        ),
    )
    packaging_snapshot.update({
        "plan_name": packaging["plan_name"],
        "route": packaging["route"],
        "confirmed_at": packaging["confirmed_at"],
    })
    packaging_gate_items = [
        dict(row) for row in db.connection.execute(
            "SELECT slot,asset_ref,asset_file,asset_version,is_backup,rank,notes "
            "FROM packaging_plan_items WHERE plan_id=? ORDER BY item_id",
            (packaging["plan_id"],),
        ).fetchall()
    ]
    packaging_gate_payload = packaging_plan.canonical_packaging_gate_payload(
        dict(packaging), packaging_gate_items
    )
    if (
        require_packaging_gate
        and canonical_digest(packaging_gate_payload) != packaging_gate["gate_digest"]
    ):
        _blocked("PACKAGING_GATE_DIGEST_MISMATCH", "已确认包装内容在门禁后发生变化")
    plan = {"speed": 1.25, "inserts": inserts, **packaging_plan_fields}
    if output_profile:
        plan["output"] = dict(output_profile)
    plan_bytes = json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8")
    plan_digest = hashlib.sha256(plan_bytes).hexdigest()

    asset_snapshot = []
    for asset_id in sorted(resolved_assets):
        asset_snapshot.append({**resolved_assets[asset_id], "role": roles[asset_id]})
    project_root = PROJECT_ROOT.resolve()
    runtime_files, controlled_environment = _runtime_file_dependencies(project_root)
    dependency_files = [
        {"kind": f"asset:{entry['asset_id']}", **_file_signature(Path(entry["path"]))}
        for entry in asset_snapshot
    ]
    if ass_path is not None:
        dependency_files.append({"kind": "ass", **_file_signature(ass_path)})
    for item in packaging_snapshot["items"]:
        if item.get("file"):
            dependency_files.append({
                "kind": (
                    f"packaging-backup:{item['slot']}"
                    if item.get("is_backup") else f"packaging:{item['slot']}"
                ),
                **_file_signature(Path(item["file"])),
            })
    dependency_snapshot = {
        "task_id": task_id,
        "picture_lock": {
            "changed_since_lock": picture_lock["changed_since_lock"],
            "selection_digest": picture_lock["selection_digest"],
        },
        "selection_state": selection_state,
        "gate_events": {
            "brief": dict(brief_gate),
            "picture_lock": dict(picture_gate),
            "packaging": None if packaging_gate is None else dict(packaging_gate),
        },
        "assets": asset_snapshot,
        "scene_cards": card_snapshot,
        "packaging": packaging_snapshot,
        "ass": None if ass_path is None else _file_signature(ass_path),
        "plan": plan,
        "files": dependency_files + runtime_files,
        "environment": controlled_environment,
    }
    dependency_digest = hashlib.sha256(
        json.dumps(
            dependency_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    out_root = (
        (project_root / "runtime/workbench/cache/packaging-previews").resolve()
        if output_profile
        else (project_root / "out").resolve()
    )
    if not _contained(output_path, out_root):
        _blocked("OUTPUT_OUTSIDE_PROJECT", f"输出必须位于项目 out/：{output_path}")
    collision_paths = {Path(item["path"]).resolve() for item in asset_snapshot}
    if ass_path is not None:
        collision_paths.add(ass_path)
    for item in packaging_snapshot["items"]:
        if item.get("file"):
            collision_paths.add(Path(item["file"]).resolve())
    if output_path in collision_paths:
        _blocked("OUTPUT_PATH_COLLISION", f"输出与输入或依赖文件冲突：{output_path}")
    if output_path.exists() and not allow_existing_output:
        _blocked("RENDER_OUTPUT_EXISTS", f"最终输出已存在：{output_path}")
    return {
        "task_id": task_id,
        "project_root": str(project_root),
        "input_path": resolved_assets[digital_humans[0]["asset_id"]]["path"],
        "plan_path": None,
        "output_path": str(output_path),
        "ass_path": None if ass_path is None else str(ass_path),
        "asset_root": str(project_root),
        "plan": plan,
        "plan_digest": plan_digest,
        "packaging": packaging_snapshot,
        "dependency_snapshot": dependency_snapshot,
        "dependency_digest": dependency_digest,
        "controlled_env": controlled_environment,
        "output_profile": None if output_profile is None else dict(output_profile),
    }


def build_render_mix_job(db, task_id: str, output_path: Path,
                         ass_path: Path | None, *,
                         allow_existing_output: bool = False) -> dict:
    """Build inside one stable read transaction and leave SQLite unchanged."""
    connection = db.connection
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
        connection.execute("PRAGMA query_only=ON")
    try:
        return _build_render_mix_job(
            db, task_id, output_path, ass_path,
            packaging_plan_id=None,
            require_packaging_gate=True,
            output_profile=None,
            allow_existing_output=allow_existing_output,
        )
    finally:
        if owned:
            connection.rollback()
            connection.execute("PRAGMA query_only=OFF")


def build_packaging_preview_mix_job(
    db,
    task_id: str,
    plan_id: str,
    work_dir: Path,
    output_path: Path,
    duration_seconds: float,
    *,
    subtitle_sources: dict | None,
    subtitle_runner=None,
) -> dict:
    """Build an explicit draft-plan preview without writing a packaging gate."""
    profile = {
        "maxDuration": float(duration_seconds),
        "width": 540,
        "height": 960,
        "crf": 28,
    }
    connection = db.connection
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
        connection.execute("PRAGMA query_only=ON")
    try:
        job = _build_render_mix_job(
            db,
            task_id,
            output_path,
            None,
            packaging_plan_id=plan_id,
            require_packaging_gate=False,
            output_profile=profile,
        )
    finally:
        if owned:
            connection.rollback()
            connection.execute("PRAGMA query_only=OFF")
    if subtitle_sources is None:
        return write_render_mix_plan(job, work_dir)
    required_sources = {"script_path", "words_path", "semantics_path"}
    if not isinstance(subtitle_sources, dict) or set(subtitle_sources) != required_sources:
        _blocked(
            "ASS_SOURCE_INVALID",
            "subtitle_sources 必须包含 script_path、words_path 和 semantics_path",
        )
    _validate_locked_subtitle_sources(db, task_id, subtitle_sources)
    presets = [
        item["asset_ref"]
        for item in job["packaging"]["items"]
        if item["slot"] == "subtitle" and not item["is_backup"]
    ]
    if len(presets) != 1:
        _blocked("ASS_PRESET_MISMATCH", "预览方案必须恰好包含一个字幕预设")
    binding = compile_task_ass(
        PROJECT_ROOT.resolve(),
        Path(work_dir),
        task_id,
        presets[0],
        Path(subtitle_sources["script_path"]),
        Path(subtitle_sources["words_path"]),
        Path(subtitle_sources["semantics_path"]),
        runner=subtitle_runner,
    )
    connection = db.connection
    owned = not connection.in_transaction
    if owned:
        connection.execute("BEGIN")
        connection.execute("PRAGMA query_only=ON")
    try:
        job = _build_render_mix_job(
            db,
            task_id,
            output_path,
            Path(binding["ass"]["path"]),
            packaging_plan_id=plan_id,
            require_packaging_gate=False,
            output_profile=profile,
        )
    finally:
        if owned:
            connection.rollback()
            connection.execute("PRAGMA query_only=OFF")
    _validate_final_subtitle_preset(job, binding)
    return write_render_mix_plan(_attach_subtitle_binding(job, binding), work_dir)


def verify_catalog_snapshot(db, task_id: str, output_path: Path, snapshot: dict) -> bool:
    """Rebuild catalog provenance without regenerating immutable subtitle bytes."""
    if snapshot.get("task_id") != task_id:
        return False
    subtitle_state = snapshot.get("subtitle")
    binding = None
    if subtitle_state is not None:
        binding = verify_subtitle_binding(PROJECT_ROOT.resolve(), subtitle_state)
        if binding is None:
            return False
        if _confirmed_subtitle_preset(db, task_id) != binding["preset_id"]:
            return False
        ass_path = Path(binding["ass"]["path"])
    else:
        ass_state = snapshot.get("ass")
        if ass_state is None:
            ass_path = None
        else:
            ass_value = ass_state.get("path")
            if not ass_value:
                return False
            ass_path = Path(ass_value)
    current_job = build_render_mix_job(
        db, task_id, Path(output_path), ass_path, allow_existing_output=True,
    )
    current = _attach_subtitle_binding(current_job, binding)["dependency_snapshot"]
    stored = dict(snapshot)
    stored.pop("plan_file", None)
    return canonical_digest(current) == canonical_digest(stored)


def write_render_mix_plan(job: dict, work_dir: Path) -> dict:
    """Write the built plan once under caller-owned work_dir; never overwrite it."""
    work_root = Path(work_dir).resolve()
    task_id = job["task_id"]
    preferred_path = (
        work_root / "runtime" / "jobs" / task_id /
        f"render-mix.{job['plan_digest']}.plan.json"
    ).resolve()
    plan_path = preferred_path
    if os.name == "nt" and len(str(preferred_path)) > _WINDOWS_SAFE_RENDER_PLAN_PATH_LENGTH:
        project_root = Path(job.get("project_root") or PROJECT_ROOT).resolve()
        plan_path = (
            project_root / "runtime" / "render-plans" /
            f"render-mix.{job['plan_digest']}.plan.json"
        ).resolve()
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    expected_bytes = json.dumps(
        job["plan"], ensure_ascii=False, indent=2
    ).encode("utf-8")

    def verify_existing_plan() -> None:
        try:
            existing_bytes = plan_path.read_bytes()
        except FileNotFoundError:
            _blocked("PLAN_WRITE_RACE", f"MixPlan 并发创建后不可读：{plan_path}")
        if existing_bytes != expected_bytes:
            _blocked("PLAN_CONTENT_MISMATCH", f"同摘要 MixPlan 内容不一致：{plan_path}")

    if plan_path.exists():
        verify_existing_plan()
    else:
        try:
            with plan_path.open("xb") as target:
                target.write(expected_bytes)
        except FileExistsError:
            verify_existing_plan()
    written = dict(job)
    written["work_dir"] = str(work_root)
    written["plan_path"] = str(plan_path)
    written["dependency_snapshot"] = dict(job["dependency_snapshot"])
    written["dependency_snapshot"]["plan_file"] = _file_signature(plan_path)
    written["dependency_digest"] = hashlib.sha256(
        json.dumps(written["dependency_snapshot"], ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return written


def _confirmed_subtitle_preset(db, task_id: str) -> str | None:
    gate = latest_gate(db, task_id, "packaging")
    if gate is None:
        return None
    try:
        plan_id = json.loads(gate["details_json"] or "{}")["plan_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    row = db.connection.execute(
        """
        SELECT item.asset_ref
        FROM packaging_plans plan
        JOIN packaging_plan_items item USING(plan_id)
        WHERE plan.task_id=? AND plan.plan_id=? AND plan.status='confirmed'
          AND item.slot='subtitle' AND item.is_backup=0
        ORDER BY item.rank, item.item_id
        LIMIT 1
        """,
        (task_id, plan_id),
    ).fetchone()
    return None if row is None else row["asset_ref"]


def _preflight_render_mix_job(
    db, task_id: str, output_path: Path,
) -> tuple[dict, str | None]:
    """Purely validate render prerequisites before any subtitle filesystem writes."""
    job = build_render_mix_job(db, task_id, output_path, None)
    presets = [
        item["asset_ref"]
        for item in job["packaging"]["items"]
        if item["slot"] == "subtitle" and not item["is_backup"]
    ]
    return job, presets[0] if len(presets) == 1 else None


def _attach_subtitle_binding(job: dict, binding: dict | None) -> dict:
    if binding is None:
        return job
    written = dict(job)
    subtitle = {
        key: binding[key]
        for key in (
            "preset_id", "alignment_coverage", "script", "words", "semantics",
            "ass", "metadata", "font",
        )
    }
    snapshot = dict(job["dependency_snapshot"])
    snapshot["subtitle"] = subtitle
    snapshot["ass"] = binding["ass"]
    files = [entry for entry in snapshot["files"] if entry.get("kind") != "ass"]
    for key in ("script", "words", "semantics", "ass", "metadata", "font"):
        files.append({"kind": f"subtitle:{key}", **binding[key]})
    files.extend(binding["runtime_files"])
    snapshot["files"] = files
    written["ass_path"] = binding["ass"]["path"]
    written["dependency_snapshot"] = snapshot
    written["dependency_digest"] = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return written


def _validate_final_subtitle_preset(job: dict, binding: dict | None) -> None:
    """Ensure the bound ASS still matches the final confirmed packaging snapshot."""
    if binding is None:
        return
    primary_presets = [
        item.get("asset_ref")
        for item in job.get("packaging", {}).get("items", [])
        if item.get("slot") == "subtitle" and not item.get("is_backup")
    ]
    if len(primary_presets) != 1:
        _blocked(
            "ASS_PRESET_MISMATCH",
            "最终包装必须恰好包含一个 primary subtitle preset",
        )
    if primary_presets[0] != binding.get("preset_id"):
        _blocked(
            "ASS_PRESET_MISMATCH",
            "最终包装 subtitle preset 与已生成 ASS 不一致",
        )


def _locked_subtitle_sources(db, task_id: str) -> dict | None:
    row = db.connection.execute(
        "SELECT brief_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        brief = json.loads(row["brief_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        _blocked("ASS_SOURCE_MISMATCH", "任务 brief 无法验证锁定字幕来源")
    if not isinstance(brief, dict) or "subtitle_sources" not in brief:
        return None
    sources = brief["subtitle_sources"]
    required = {"script_path", "words_path", "semantics_path"}
    if not isinstance(sources, dict) or set(sources) != required:
        _blocked("ASS_SOURCE_MISMATCH", "任务 brief 的锁定字幕来源不完整")
    try:
        return {key: str(Path(sources[key]).resolve()) for key in required}
    except (TypeError, ValueError):
        _blocked("ASS_SOURCE_MISMATCH", "任务 brief 的锁定字幕来源路径无效")


def _validate_locked_subtitle_sources(db, task_id: str, supplied: dict | None) -> None:
    locked = _locked_subtitle_sources(db, task_id)
    if locked is None:
        return
    if supplied is None:
        _blocked("ASS_SOURCE_MISMATCH", "锁定任务必须使用 brief 中的三份字幕来源")
    try:
        current = {key: str(Path(supplied[key]).resolve()) for key in locked}
    except (KeyError, TypeError, ValueError):
        _blocked("ASS_SOURCE_MISMATCH", "提供的字幕来源无法与任务锁定来源比较")
    if current != locked:
        _blocked("ASS_SOURCE_MISMATCH", "提供的字幕来源与任务 brief 锁定来源不一致")


def _validate_locked_subtitle_binding(db, task_id: str, binding: dict) -> None:
    locked = _locked_subtitle_sources(db, task_id)
    if locked is None:
        return
    for source_name, path_name in (
        ("script", "script_path"),
        ("words", "words_path"),
        ("semantics", "semantics_path"),
    ):
        expected_path = Path(locked[path_name]).resolve()
        if not expected_path.is_file():
            _blocked("ASS_SOURCE_MISMATCH", f"锁定字幕来源不存在：{expected_path}")
        expected_hash = _file_signature(expected_path)["sha256"]
        try:
            actual = binding[source_name]
            actual_path = Path(actual["path"]).resolve()
            actual_hash = actual["sha256"]
        except (KeyError, TypeError, ValueError):
            _blocked("ASS_SOURCE_MISMATCH", "外部 ASS 的字幕来源签名不完整")
        if actual_path != expected_path or actual_hash != expected_hash:
            _blocked("ASS_SOURCE_MISMATCH", "外部 ASS 的字幕来源与任务 brief 锁定来源不一致")


def compile_render_mix_job(
    db,
    task_id: str,
    work_dir: Path,
    output_path: Path,
    ass_path: Path | None,
    *,
    subtitle_sources: dict | None = None,
    subtitle_runner=None,
) -> dict:
    """Compile immutable subtitles, then pure-build and immutably write the mix plan."""
    if ass_path is not None and subtitle_sources is not None:
        _blocked("ASS_SOURCE_CONFLICT", "外部 ASS 不能与 script/words/semantics 同时提供")
    preflight_job, preset_id = _preflight_render_mix_job(
        db, task_id, Path(output_path)
    )
    binding = None
    project_root = PROJECT_ROOT.resolve()
    if subtitle_sources is not None:
        if preset_id is None:
            _blocked("ASS_PRESET_MISMATCH", "任务没有已确认 subtitle preset")
        if not isinstance(subtitle_sources, dict) or set(subtitle_sources) != {
            "script_path", "words_path", "semantics_path",
        }:
            _blocked(
                "ASS_SOURCE_INVALID",
                "subtitle_sources 必须包含 script_path、words_path 和 semantics_path",
            )
        _validate_locked_subtitle_sources(db, task_id, subtitle_sources)
        binding = compile_task_ass(
            project_root,
            Path(work_dir),
            task_id,
            preset_id,
            Path(subtitle_sources["script_path"]),
            Path(subtitle_sources["words_path"]),
            Path(subtitle_sources["semantics_path"]),
            runner=subtitle_runner,
        )
        ass_path = Path(binding["ass"]["path"])
    elif ass_path is not None:
        if preset_id is None:
            _blocked(
                "ASS_PRESET_MISMATCH",
                "外部 ASS 必须绑定已确认 subtitle preset 和 metadata sidecar",
            )
        binding = bind_external_ass(project_root, preset_id, Path(ass_path))
        _validate_locked_subtitle_binding(db, task_id, binding)
        ass_path = Path(binding["ass"]["path"])
    elif preset_id is not None:
        _validate_locked_subtitle_sources(db, task_id, None)
        _blocked(
            "ASS_PRESET_MISMATCH",
            "已确认 subtitle preset 必须提供 script/words/semantics 或匹配 ASS sidecar",
        )
    job = (
        preflight_job
        if ass_path is None
        else build_render_mix_job(db, task_id, output_path, ass_path)
    )
    _validate_final_subtitle_preset(job, binding)
    return write_render_mix_plan(_attach_subtitle_binding(job, binding), work_dir)


def _required_file(path_value, code: str, label: str) -> Path:
    if not path_value:
        _blocked(code, f"缺少{label}路径")
    path = Path(path_value).resolve()
    if not path.is_file():
        _blocked(code, f"{label}不存在：{path}")
    return path


def _find_ts_node(project_root: Path) -> Path | None:
    candidates = [project_root / "node_modules" / "ts-node" / "dist" / "bin.js"]
    if project_root.parent.name == ".worktrees":
        candidates.append(
            project_root.parent.parent / "node_modules" / "ts-node" / "dist" / "bin.js"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _runtime_file_dependencies(project_root: Path) -> tuple[list[dict], dict]:
    """Resolve every executable/source/font that can influence render bytes."""
    required = [
        project_root / "scripts" / "render-mix.ts",
        project_root / "scripts" / "render-mix-options.ts",
        project_root / "src" / "types.ts",
        project_root / "src" / "mix-sfx.ts",
        project_root / "assets" / "fonts" / "SourceHanSerifSC-Heavy.otf",
    ]
    node_value = shutil.which("node")
    ts_node = _find_ts_node(project_root)
    try:
        roots = default_ffprobe_search_roots(os.environ)
        ffmpeg = find_ffmpeg(os.environ, search_roots=roots)
        ffprobe = find_ffprobe(os.environ, search_roots=roots)
    except ToolNotFound as error:
        _blocked("RENDER_TOOL_MISSING", str(error))
    required.extend((Path(node_value) if node_value else None, ts_node, ffmpeg, ffprobe))
    labels = ("render_mix", "render_options", "types", "mix_sfx", "font", "node",
              "ts_node", "ffmpeg", "ffprobe")
    files = []
    for label, candidate in zip(labels, required):
        if candidate is None:
            _blocked("RENDER_TOOL_MISSING", f"缺少渲染依赖：{label}")
        path = Path(candidate).resolve()
        if not path.is_file():
            _blocked("RENDER_TOOL_MISSING", f"渲染依赖不存在：{label}/{path}")
        files.append({"kind": label, **_file_signature(path)})
    if ATR_PATH.is_file():
        files.append({"kind": "atr", **_file_signature(ATR_PATH.resolve())})
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    if taskkill.is_file():
        files.append({"kind": "taskkill", **_file_signature(taskkill.resolve())})
    environment = {
        "PATH": os.pathsep.join((str(Path(ffmpeg).resolve().parent),
                                  str(Path(node_value).resolve().parent))),
        "LANG": "C.UTF-8",
    }
    return files, environment


def verify_dependency_snapshot(snapshot: dict) -> bool:
    """Rehash stored dependency paths; timestamps and sizes are never sufficient."""
    entries = list(snapshot.get("files", []))
    if snapshot.get("plan_file"):
        entries.append(snapshot["plan_file"])
    for entry in entries:
        path = Path(entry["path"])
        if not path.is_file() or _file_signature(path)["sha256"] != entry.get("sha256"):
            return False
    return True


def _process_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _run_process_tree(command: list[str], **kwargs):
    """Fallback runner that terminates the whole Windows child process tree."""
    timeout = kwargs.pop("timeout")
    kwargs.pop("check", None)
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(command, creationflags=creationflags, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=30,
            )
        else:
            process.kill()
        process.communicate()
        raise subprocess.TimeoutExpired(command, timeout) from error
    return SimpleNamespace(returncode=process.returncode, stdout=stdout, stderr=stderr)


def _default_process_runner(command: list[str], **kwargs):
    """Prefer ATR; fall back only for the observed runtime.internal_error."""
    timeout = int(kwargs["timeout"])
    if ATR_PATH.is_file():
        atr_command = [
            str(ATR_PATH.resolve()), "run-process",
            "--exe", str(Path(command[0]).resolve()),
            "--cwd", str(Path(kwargs["cwd"]).resolve()),
            "--timeout-ms", str(timeout * 1000),
            "--max-output-bytes", "1048576",
            "--encoding", "utf-8",
            "--caller-agent", "codex",
            "--json", "--", *command[1:],
        ]
        atr = subprocess.run(
            atr_command, shell=False, cwd=kwargs["cwd"], env=kwargs["env"],
            timeout=timeout + 30, check=False, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        try:
            payload = json.loads((atr.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            payload = {}
        if payload.get("status") == "succeeded":
            result = payload.get("result") or payload
            return SimpleNamespace(
                returncode=int(result.get("exit_code", 0)),
                stdout=result.get("stdout", ""), stderr=result.get("stderr", ""),
            )
        error_code = (payload.get("error") or {}).get("code")
        if error_code not in (
            "runtime.internal_error",
            "request.flag_unknown",
            "request.argument_unexpected",
        ):
            return SimpleNamespace(
                returncode=atr.returncode or 1, stdout=atr.stdout, stderr=atr.stderr,
            )
    return _run_process_tree(command, **kwargs)


def _controlled_render_env(job: dict, supplied: dict | None, node_path: Path) -> dict:
    source = os.environ if supplied is None else supplied
    allowed = ("SystemRoot", "WINDIR", "ComSpec", "TEMP", "TMP", "PATHEXT")
    result = {name: source[name] for name in allowed if source.get(name)}
    declared = job.get("controlled_env") or {}
    result.update({str(key): str(value) for key, value in declared.items()})
    for name in (
        "TS_NODE_PROJECT", "TS_NODE_TRANSPILE_ONLY", "TS_NODE_COMPILER_OPTIONS",
    ):
        result.pop(name, None)
    if "PATH" not in result:
        result["PATH"] = source.get("PATH", str(node_path.parent))
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy", "NODE_OPTIONS",
    ):
        result[name] = ""
    return result


def run_render_mix(
    job: dict,
    *,
    runner=None,
    env: dict | None = None,
) -> Path:
    """Execute one compiled render-mix job without shell or network package lookup."""
    project_root = Path(job.get("project_root") or Path(__file__).resolve().parents[1]).resolve()
    input_path = _required_file(job.get("input_path"), "RENDER_INPUT_MISSING", "数字人口播")
    plan_path = _required_file(job.get("plan_path"), "RENDER_PLAN_MISSING", "MixPlan")
    render_script = _required_file(
        job.get("render_script") or project_root / "scripts" / "render-mix.ts",
        "RENDER_SCRIPT_MISSING",
        "render-mix.ts",
    )

    node_value = job.get("node_path") or shutil.which("node")
    node_path = _required_file(node_value, "NODE_NOT_FOUND", "Node")
    ts_node_value = job.get("ts_node_path") or _find_ts_node(project_root)
    ts_node_path = _required_file(ts_node_value, "TS_NODE_NOT_FOUND", "本地 ts-node")

    output_path = Path(job["output_path"]).resolve()
    out_root = (
        (project_root / "runtime/workbench/cache/packaging-previews").resolve()
        if job.get("output_profile")
        else (project_root / "out").resolve()
    )
    if not _contained(output_path, out_root):
        _blocked("OUTPUT_OUTSIDE_PROJECT", f"输出必须位于项目 out/：{output_path}")
    collisions = {
        input_path, plan_path, render_script, node_path, ts_node_path,
    }
    if job.get("ass_path"):
        collisions.add(Path(job["ass_path"]).resolve())
    for asset in job.get("dependency_snapshot", {}).get("assets", []):
        if asset.get("path"):
            collisions.add(Path(asset["path"]).resolve())
    for item in job.get("packaging", {}).get("items", []):
        if item.get("file"):
            collisions.add(Path(item["file"]).resolve())
    if output_path in collisions:
        _blocked("OUTPUT_PATH_COLLISION", f"输出与输入或依赖文件冲突：{output_path}")
    if output_path.exists():
        _blocked("RENDER_OUTPUT_EXISTS", f"最终输出已存在：{output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_name(
        f"{output_path.stem}.part-{uuid.uuid4().hex}{output_path.suffix}"
    )
    if part_path.exists():
        _blocked("RENDER_PART_EXISTS", f"临时输出意外已存在：{part_path}")
    asset_root = Path(job.get("asset_root") or project_root).resolve()
    ass_value = job.get("ass_path")
    if ass_value:
        ass_path = _required_file(ass_value, "ASS_FILE_MISSING", "字幕文件")
    else:
        # Passing a deliberate missing path prevents the TypeScript CLI from silently
        # falling back to public/scripts/<task>.ass.
        ass_path = (
            plan_path.parent / f"subtitles-disabled-{uuid.uuid4().hex}.ass"
        ).resolve()
        if ass_path.exists():
            _blocked("ASS_SENTINEL_COLLISION", f"字幕禁用哨兵意外存在：{ass_path}")

    command = [
        str(node_path),
        str(ts_node_path),
        str(render_script),
        str(job["task_id"]),
        "--input", str(input_path),
        "--plan", str(plan_path),
        "--ass", str(ass_path),
        "--output", str(part_path),
        "--asset-root", str(asset_root),
    ]
    output_profile = job.get("output_profile") or {}
    for key, flag in (
        ("maxDuration", "--max-duration"),
        ("width", "--width"),
        ("height", "--height"),
        ("crf", "--crf"),
    ):
        if output_profile.get(key) is not None:
            command.extend((flag, str(output_profile[key])))
    controlled_env = _controlled_render_env(job, env, node_path)
    timeout = int(job.get("timeout_seconds", 1800))
    try:
        process_runner = _default_process_runner if runner is None else runner
        result = process_runner(
            command,
            shell=False,
            cwd=str(project_root),
            env=controlled_env,
            timeout=timeout,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        part_path.unlink(missing_ok=True)
        _blocked("RENDER_TIMEOUT", f"渲染超过 {timeout} 秒，已停止等待")
    if result.returncode != 0:
        part_path.unlink(missing_ok=True)
        detail = _process_text(getattr(result, "stderr", "")).strip()[-2000:]
        _blocked(
            "RENDER_PROCESS_FAILED",
            f"render-mix 退出码 {result.returncode}" + (f"：{detail}" if detail else ""),
        )
    if not part_path.is_file():
        _blocked("RENDER_OUTPUT_MISSING", f"渲染进程成功但未生成输出：{part_path}")
    after_stat = part_path.stat()
    if after_stat.st_size <= 0:
        part_path.unlink(missing_ok=True)
        _blocked("RENDER_OUTPUT_EMPTY", f"渲染输出为空：{output_path}")
    # Hash the complete temporary output before atomically publishing it.
    _file_signature(part_path)
    if output_path.exists():
        part_path.unlink(missing_ok=True)
        _blocked("RENDER_OUTPUT_EXISTS", f"发布前最终输出已存在：{output_path}")
    os.replace(part_path, output_path)
    return output_path
