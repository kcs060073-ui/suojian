"""Information-flow ad task preflight and durable creation."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .model_profiles import (
    ModelProfileUnavailable,
    override_snapshot_profile_settings,
    resolve_task_tts_voice,
    resolve_task_profile_snapshots,
    save_task_profile_bindings,
)
from .task_sources import get_task_sources, set_task_sources
from .tasks import create_task, get_task
from .workflow import complete_stage, project_workflow


SUPPORTED_ROUTE = "ad_mix"
SUPPORTED_ASPECT_RATIOS = {"9:16", "16:9"}
SUPPORTED_VOICE_MODES = {"ai_tts"}
SPLIT_TEXT_CAPABILITIES = ("copywriting", "storyboard", "packaging")
CONFIRMATIONS = (
    {"key": "brief", "label": "文案锁定", "stage": "brief_lock",
     "description": "确认广告最终文案、目标时长、画幅和配音方式。"},
    {"key": "picture", "label": "画面和顺序锁定", "stage": "picture_lock",
     "description": "确认逐句配片、镜头顺序和转场节奏。"},
    {"key": "packaging", "label": "包装方案确认", "stage": "packaging_confirm",
     "description": "确认字幕、音色、BGM、Logo、水印和其他包装组合。"},
    {"key": "full_review", "label": "完整审片", "stage": "full_review",
     "description": "完整观看当前候选版本后人工确认是否可发布。"},
)


def _selected_model_capabilities(selections: object) -> tuple[str, ...]:
    if not isinstance(selections, Mapping):
        return ("general_text", "tts")
    if "planning" in selections or "broll" in selections:
        capabilities = ["planning", "tts"]
        if selections.get("broll"):
            capabilities.append("broll")
        return tuple(capabilities)
    if any(key in selections for key in SPLIT_TEXT_CAPABILITIES):
        capabilities = ["storyboard", "tts"]
        for capability in ("copywriting", "packaging"):
            if selections.get(capability):
                capabilities.append(capability)
    else:
        capabilities = ["general_text", "tts"]
    if selections.get("video_generation"):
        capabilities.append("video_generation")
    return tuple(capabilities)


def _orchestration_mode(request: Mapping[str, object]) -> str:
    value = request.get("orchestration_mode", "standalone")
    if value not in {"standalone", "agent"}:
        raise ValueError(
            "ORCHESTRATION_MODE_INVALID: 编排模式必须是 standalone 或 agent"
        )
    return str(value)


def _agent_external_capabilities(selections: object) -> tuple[str, ...]:
    capabilities = ["tts"]
    if isinstance(selections, Mapping):
        capabilities.extend(
            name
            for name in ("video_generation", "broll")
            if selections.get(name)
        )
    return tuple(capabilities)


class AdTaskPreflightError(ValueError):
    code = "AD_TASK_PREFLIGHT_FAILED"

    def __init__(self, preflight: Mapping[str, object]):
        self.preflight = dict(preflight)
        messages = [
            str(item.get("message"))
            for item in self.preflight.get("blocking", [])
            if isinstance(item, Mapping)
        ]
        super().__init__("；".join(messages) or "信息流广告任务预检未通过")


def _check(code: str, status: str, message: str, basis=None) -> dict:
    return {"code": code, "status": status, "message": message, "basis": basis}


def _add(checks, blocking, warnings, item) -> None:
    checks.append(item)
    if item["status"] == "blocking":
        blocking.append(item)
    elif item["status"] == "warning":
        warnings.append(item)


def _voice_error_code(error: ValueError) -> str:
    code, separator, _message = str(error).partition(":")
    return code if separator and re.fullmatch(r"[A-Z][A-Z0-9_]+", code) else "VOICE_NOT_SELECTABLE"


def _task_voice_selection(db, request, snapshots) -> dict | None:
    snapshot = snapshots.get("tts") if isinstance(snapshots, Mapping) else None
    if not isinstance(snapshot, Mapping):
        return None
    requested = request.get("tts_voice_id")
    voice_id = requested.strip() if isinstance(requested, str) else ""
    if snapshot.get("provider") == "doubao_tts" and not voice_id:
        raise ValueError("TTS_VOICE_REQUIRED: 请选择本条视频使用的音色")
    if not voice_id:
        return None
    return resolve_task_tts_voice(db, snapshot, voice_id)


def _libraries(db, ids: list[str]) -> dict:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.connection.execute(
        f"SELECT library_id,name,mount_path,online,purpose,is_default_generic "
        f"FROM libraries WHERE library_id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    return {row["library_id"]: row for row in rows}


def _string_list(value) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        item = item.strip()
        if item not in result:
            result.append(item)
    return result


def _estimates(duration: float) -> dict:
    return {
        "machine_seconds": {
            "status": "estimated", "min": max(60, round(duration * 2)),
            "max": max(180, round(duration * 8)), "unit": "seconds",
            "basis": "按目标时长估算配片、渲染和质检时间", "reason": None,
        },
        "human_seconds": {
            "status": "estimated", "min": 120, "max": 600,
            "unit": "seconds", "basis": "四个人工确认点", "reason": None,
        },
        "api_cost": {
            "status": "unavailable", "min": None, "max": None, "unit": None,
            "basis": None, "reason": "模型与 TTS 调用前按供应商配置再次确认",
        },
    }


def _queue(db) -> dict:
    waiting = db.connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='queued' AND cancel_requested=0"
    ).fetchone()[0]
    running = db.connection.execute(
        "SELECT job_id,job_type FROM jobs WHERE status='running' ORDER BY rowid LIMIT 1"
    ).fetchone()
    return {
        "heavy_slot": "busy" if running else "available",
        "running_job_id": None if running is None else running["job_id"],
        "running_job_type": None if running is None else running["job_type"],
        "waiting_count": waiting, "queued_jobs": [], "reason": None,
        "observed_at": None,
    }


def preflight_ad_task(db, request: Mapping[str, object], project_root: Path | str) -> dict:
    """Validate ad inputs and material scope without changing business state."""

    checks, blocking, warnings = [], [], []
    orchestration_mode = _orchestration_mode(request)
    route = request.get("route")
    if route != SUPPORTED_ROUTE:
        item = _check("AD_ROUTE_NOT_AVAILABLE", "blocking", "该请求不是信息流广告混剪路线")
        return {
            "route": route, "checks": [item], "blocking": [item], "warnings": [],
            "estimated_space_bytes": None, "next_action": "fix_blocking_items",
            "estimates": _estimates(30), "queue": _queue(db),
            "confirmations": [dict(item) for item in CONFIRMATIONS],
        }
    _add(checks, blocking, warnings,
         _check("AD_ROUTE_SUPPORTED", "passed", "信息流广告混剪路线已开放"))

    selections = request.get("model_profile_ids")
    if selections is not None:
        model_capabilities = (
            _selected_model_capabilities(selections)
            if orchestration_mode == "standalone"
            else _agent_external_capabilities(selections)
        )
        try:
            snapshots = resolve_task_profile_snapshots(
                db,
                selections,
                required_capabilities=model_capabilities,
            )
        except (ModelProfileUnavailable, TypeError, ValueError) as error:
            _add(
                checks,
                blocking,
                warnings,
                _check(
                    "MODEL_PROFILE_UNAVAILABLE",
                    "blocking",
                    "所选 AI 模型当前不可用",
                    str(error),
                ),
            )
        else:
            _add(
                checks,
                blocking,
                warnings,
                _check(
                    "MODEL_PROFILES_READY",
                    "passed",
                    "文案模型和配音模型均已通过检测",
                ),
            )
            try:
                voice_selection = _task_voice_selection(
                    db,
                    request,
                    snapshots,
                )
            except ValueError as error:
                _add(
                    checks,
                    blocking,
                    warnings,
                    _check(
                        _voice_error_code(error),
                        "blocking",
                        str(error).partition(":")[2].strip() or str(error),
                    ),
                )
            else:
                if voice_selection is not None:
                    _add(
                        checks,
                        blocking,
                        warnings,
                        _check(
                            "TTS_VOICE_READY",
                            "passed",
                            f"本条视频使用：{voice_selection['displayName']}",
                        ),
                    )

    title = request.get("title")
    if not isinstance(title, str) or not title.strip():
        _add(checks, blocking, warnings,
             _check("TASK_TITLE_REQUIRED", "blocking", "任务名称不能为空"))
    script = request.get("final_script")
    if not isinstance(script, str) or not script.strip():
        _add(checks, blocking, warnings,
             _check("FINAL_SCRIPT_REQUIRED", "blocking", "必须提供本次广告最终文案"))
    else:
        _add(checks, blocking, warnings,
             _check("FINAL_SCRIPT_READY", "passed", "广告最终文案已提供",
                    f"UTF-8 字符数={len(script.strip())}"))
    purpose = request.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        _add(checks, blocking, warnings,
             _check("AD_PURPOSE_REQUIRED", "blocking", "必须填写广告投放目的"))

    ratio = request.get("aspect_ratio")
    ratio_ok = ratio in SUPPORTED_ASPECT_RATIOS
    _add(checks, blocking, warnings, _check(
        "AD_ASPECT_RATIO_READY" if ratio_ok else "AD_ASPECT_RATIO_INVALID",
        "passed" if ratio_ok else "blocking",
        f"广告画幅为 {ratio}" if ratio_ok else "广告画幅只能选择 9:16 或 16:9",
    ))
    voice = request.get("voice_mode")
    voice_ok = voice in SUPPORTED_VOICE_MODES
    _add(checks, blocking, warnings, _check(
        "AD_VOICE_MODE_READY" if voice_ok else "AD_VOICE_MODE_INVALID",
        "passed" if voice_ok else "blocking",
        "广告将使用已配置的 AI 配音" if voice_ok else "当前只支持 AI 配音",
    ))

    duration = request.get("target_duration_seconds")
    duration_ok = (
        isinstance(duration, (int, float)) and not isinstance(duration, bool)
        and 0 < float(duration) <= 600
    )
    if not duration_ok:
        _add(checks, blocking, warnings, _check(
            "TARGET_DURATION_INVALID", "blocking",
            "目标时长必须大于 0 秒且不超过 600 秒",
        ))
    duration_value = float(duration) if duration_ok else 30.0

    include_defaults = request.get("include_default_generic")
    if not isinstance(include_defaults, bool):
        _add(checks, blocking, warnings, _check(
            "DEFAULT_GENERIC_SETTING_INVALID", "blocking",
            "默认通用素材开关必须是布尔值",
        ))
        include_defaults = False
    selected = _string_list(request.get("selected_generic_ids", []))
    if selected is None:
        _add(checks, blocking, warnings, _check(
            "GENERIC_LIBRARY_SELECTION_INVALID", "blocking",
            "通用素材目录选择格式无效",
        ))
        selected = []

    primary_id = request.get("primary_library_id")
    primary_id = primary_id.strip() if isinstance(primary_id, str) else ""
    default_ids = []
    if include_defaults:
        default_ids = [
            row["library_id"] for row in db.connection.execute(
                "SELECT library_id FROM libraries "
                "WHERE purpose='generic' AND is_default_generic=1 ORDER BY library_id"
            ).fetchall()
        ]
    ids = list(dict.fromkeys(([primary_id] if primary_id else []) + selected + default_ids))
    libraries = _libraries(db, ids)
    primary = libraries.get(primary_id)
    if primary is None or primary["purpose"] != "task_specific":
        _add(checks, blocking, warnings, _check(
            "PRIMARY_LIBRARY_INVALID", "blocking", "必须选择一个本次主素材目录",
        ))
    else:
        online = bool(primary["online"]) and Path(primary["mount_path"]).is_dir()
        _add(checks, blocking, warnings, _check(
            "PRIMARY_LIBRARY_ONLINE" if online else "PRIMARY_LIBRARY_OFFLINE",
            "passed" if online else "blocking",
            "主素材目录在线且可读" if online else "主素材目录当前离线或不可读",
            f"library_id={primary_id}",
        ))

    for library_id in [*selected, *default_ids]:
        library = libraries.get(library_id)
        if library is None or library["purpose"] != "generic":
            _add(checks, blocking, warnings, _check(
                "GENERIC_LIBRARY_INVALID", "blocking",
                f"通用素材目录不存在或用途不合法：{library_id}",
            ))
            continue
        online = bool(library["online"]) and Path(library["mount_path"]).is_dir()
        _add(checks, blocking, warnings, _check(
            "GENERIC_LIBRARY_ONLINE" if online else "GENERIC_LIBRARY_OFFLINE",
            "passed" if online else "warning",
            f"通用素材目录可用：{library['name']}"
            if online else f"通用素材目录离线，将跳过：{library['name']}",
            f"library_id={library_id}",
        ))

    return {
        "route": SUPPORTED_ROUTE, "checks": checks, "blocking": blocking,
        "warnings": warnings,
        "estimated_space_bytes": max(
            64 * 1024 * 1024, round(duration_value) * 8 * 1024 * 1024
        ),
        "next_action": "create_task" if not blocking else "fix_blocking_items",
        "estimates": _estimates(duration_value), "queue": _queue(db),
        "confirmations": [dict(item) for item in CONFIRMATIONS],
    }


def create_ad_task(db, request: Mapping[str, object], project_root: Path | str) -> dict:
    """Create one ad task with task-local inputs and explicit material scope."""

    root = Path(project_root).resolve()
    orchestration_mode = _orchestration_mode(request)
    preflight = preflight_ad_task(db, request, root)
    if preflight["blocking"]:
        raise AdTaskPreflightError(preflight)

    tasks_root = root / "runtime" / "workbench" / "tasks"
    tasks_root.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        task_id = "task_" + uuid.uuid4().hex
        staging = tasks_root / f".{task_id}.staging"
        final = tasks_root / task_id
        if get_task(db, task_id) is None and not staging.exists() and not final.exists():
            break
    else:
        raise RuntimeError("无法生成唯一任务 ID，请重试")

    script = str(request["final_script"]).strip()
    script_bytes = script.encode("utf-8")
    script_digest = hashlib.sha256(script_bytes).hexdigest()
    materials = {
        "primary_library_id": str(request["primary_library_id"]).strip(),
        "selected_generic_ids": list(request.get("selected_generic_ids", [])),
        "include_default_generic": bool(request.get("include_default_generic", False)),
    }
    brief = {
        "route": SUPPORTED_ROUTE,
        "final_script": {"relative_path": "inputs/final-script.txt", "sha256": script_digest},
        "materials": materials, "purpose": str(request["purpose"]).strip(),
        "target_duration_seconds": float(request["target_duration_seconds"]),
        "aspect_ratio": str(request["aspect_ratio"]),
        "voice_mode": str(request["voice_mode"]),
    }
    profile_snapshots = None
    tts_voice = None
    if request.get("model_profile_ids") is not None:
        selections = request.get("model_profile_ids")
        model_capabilities = (
            _selected_model_capabilities(selections)
            if orchestration_mode == "standalone"
            else _agent_external_capabilities(selections)
        )
        profile_snapshots = resolve_task_profile_snapshots(
            db,
            selections,
            required_capabilities=model_capabilities,
        )
        tts_voice = _task_voice_selection(db, request, profile_snapshots)
        if tts_voice is not None:
            profile_snapshots = dict(profile_snapshots)
            profile_snapshots["tts"] = override_snapshot_profile_settings(
                profile_snapshots["tts"],
                {
                    "voice": tts_voice["voiceId"],
                    "voice_name": tts_voice["displayName"],
                    "resource_id": tts_voice["resourceId"],
                },
            )
            brief["tts_voice_id"] = tts_voice["voiceId"]
            brief["tts_voice_name"] = tts_voice["displayName"]

    task_created = False
    try:
        staging.mkdir()
        inputs = staging / "inputs"
        inputs.mkdir()
        (inputs / "final-script.txt").write_bytes(script_bytes)
        ad_settings = {
                "purpose": brief["purpose"],
                "target_duration_seconds": brief["target_duration_seconds"],
                "aspect_ratio": brief["aspect_ratio"],
                "voice_mode": brief["voice_mode"],
            }
        if tts_voice is not None:
            ad_settings["tts_voice_id"] = tts_voice["voiceId"]
            ad_settings["tts_voice_name"] = tts_voice["displayName"]
        (inputs / "ad-settings.json").write_text(
            json.dumps(ad_settings, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        create_task(
            db, task_id, str(request["title"]).strip(),
            task_type=SUPPORTED_ROUTE, brief=brief,
            orchestration_mode=orchestration_mode,
        )
        task_created = True
        if profile_snapshots is not None:
            save_task_profile_bindings(db, task_id, profile_snapshots)
        set_task_sources(
            db, task_id,
            primary_library_id=materials["primary_library_id"],
            selected_generic_ids=materials["selected_generic_ids"],
            include_default_generic=materials["include_default_generic"],
        )
        stage_digest = hashlib.sha256(json.dumps({
            "script_sha256": script_digest, "materials": materials,
            "purpose": brief["purpose"],
            "target_duration_seconds": brief["target_duration_seconds"],
            "aspect_ratio": brief["aspect_ratio"], "voice_mode": brief["voice_mode"],
            "tts_voice_id": brief.get("tts_voice_id"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        complete_stage(
            db, task_id, "create_preflight", input_digest=stage_digest,
            details={
                "route": SUPPORTED_ROUTE,
                "input_directory": f"runtime/workbench/tasks/{task_id}/inputs",
            },
        )
        os.replace(staging, final)
    except BaseException:
        if task_created:
            with db.transaction() as connection:
                connection.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        if staging.exists():
            shutil.rmtree(staging)
        if final.exists():
            shutil.rmtree(final)
        raise

    return {
        "task_id": task_id, "task": get_task(db, task_id),
        "input_dir": str(final / "inputs"), "sources": get_task_sources(db, task_id),
        "workflow": project_workflow(db, task_id), "preflight": preflight,
    }

def require_ad_picture_assets(db, task_id: str) -> None:
    """Fail closed before picture lock when any ad sentence lacks real footage."""

    row = db.connection.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN asset_id IS NULL THEN 1 ELSE 0 END) AS missing "
        "FROM scene_cards WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row["total"] == 0 or int(row["missing"] or 0) > 0:
        raise RuntimeError(
            "AD_SCENE_ASSET_REQUIRED: "
            "每句广告文案都必须选定真实素材后才能锁定画面"
        )
