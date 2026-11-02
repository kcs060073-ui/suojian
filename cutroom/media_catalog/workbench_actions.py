"""Trusted route-aware actions executed by the persistent workbench queue."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .assemble import assemble
from .digital_workflow import _default_process_runner
from .match import rank
from .model_profiles import get_task_profile_binding_for_usage
from .planning_finalize import finalize_digital_storyboard
from .preview import generate_asset_previews
from .provenance import latest_gate
from .rough_media import resolve_rough_media
from .scanner import scan_library
from .snapshots import file_content_signature, latest_review_candidate, latest_retryable_render_snapshot
from .task_sources import list_eligible_assets, sync_task_sources
from .tasks import get_task
from .tools import default_ffprobe_search_roots, find_ffmpeg
from .workflow import complete_stage


def _canonical_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _task_context(db, job, project_root) -> tuple[Path, dict]:
    root = Path(project_root).resolve()
    task_id = job.task_id or job.payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("TASK_ID_REQUIRED: 后台动作缺少任务编号")
    if job.task_id is not None and job.payload.get("task_id", task_id) != task_id:
        raise ValueError("TASK_ID_MISMATCH: 后台任务编号不一致")
    if any(part in task_id for part in ("/", "\\")) or task_id in {".", ".."}:
        raise ValueError("INVALID_TASK_ID: 任务编号包含非法路径字符")
    task = get_task(db, task_id)
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    if task.get("task_type") not in {"digital_human", "ad_mix"}:
        raise ValueError("UNSUPPORTED_ROUTE: 当前动作不支持该视频类型")
    task_root = (
        root / "runtime" / "workbench" / "tasks" / task_id
    ).resolve()
    trusted_tasks = (root / "runtime" / "workbench" / "tasks").resolve()
    if not _contained(task_root, trusted_tasks):
        raise ValueError("INVALID_TASK_ID: 任务目录越界")
    return task_root, task


def _locked_inputs(db, job, project_root) -> tuple[Path, dict, Path, Path, str]:
    task_root, task = _task_context(db, job, project_root)
    if latest_gate(db, task["task_id"], "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定最终文案")
    brief = task.get("brief")
    if not isinstance(brief, dict):
        raise ValueError("BRIEF_INVALID: 任务文案记录无效")
    script_record = brief.get("final_script")
    source_record = brief.get("digital_human_source")
    if not isinstance(script_record, dict) or not isinstance(source_record, dict):
        raise ValueError("BRIEF_INVALID: 任务缺少锁定文案或数字人来源")
    relative_script = script_record.get("relative_path")
    if not isinstance(relative_script, str):
        raise ValueError("BRIEF_INVALID: 锁定文案路径无效")
    script_path = (task_root / relative_script).resolve()
    if not _contained(script_path, task_root) or not script_path.is_file():
        raise ValueError("LOCKED_SCRIPT_MISSING: 锁定文案文件不存在")
    script_sha256 = _file_sha256(script_path)
    if script_sha256 != script_record.get("sha256"):
        raise RuntimeError("LOCKED_SCRIPT_CHANGED: 锁定文案内容已经变化")
    source_path = Path(str(source_record.get("path") or "")).resolve()
    if not source_path.is_file():
        raise ValueError("DIGITAL_HUMAN_SOURCE_MISSING: 数字人口播源文件不存在")
    return task_root, brief, script_path, source_path, script_sha256


def _action_result(cursor: str, phase_label: str, result: dict) -> dict:
    return {
        "cursor": cursor,
        "progress": {"current": 1, "total": 1},
        "phase_label": phase_label,
        "result": result,
    }


def _run_checked(
    runner, command: list[str], *, cwd: Path, timeout_seconds: int,
    max_output_bytes: int = 64 * 1024, env=None,
):
    options = {
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
    }
    if env is not None:
        options["env"] = env
    outcome = runner(command, **options)
    if (
        isinstance(outcome, dict)
        and outcome.get("started") is not True
        and (
            outcome.get("error_code") == "runtime.internal_error"
            or outcome.get("error") == "无法保证硬超时：本机 Agent Tool Runtime（ATR）不可用"
        )
    ):
        # Only fall back when ATR could not create the target process. Keep the
        # same timeout and use the Windows process-tree runner used by rendering.
        from .render_mix_adapter import _run_process_tree

        try:
            completed = _run_process_tree(
                command,
                cwd=str(Path(cwd).resolve()),
                env=env,
                timeout=timeout_seconds,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            outcome = {
                "started": True,
                "timed_out": True,
                "returncode": None,
                "stdout": "",
                "stderr": "",
            }
        else:
            outcome = {
                "started": True,
                "timed_out": False,
                "returncode": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            }
    if not isinstance(outcome, dict) or not outcome.get("started", True):
        raise RuntimeError("PROCESS_START_FAILED: 本机处理进程未能启动")
    if outcome.get("timed_out"):
        raise RuntimeError("PROCESS_TIMEOUT: 本机处理超时，可从当前步骤恢复")
    if outcome.get("returncode") != 0:
        detail = str(outcome.get("stderr") or outcome.get("stdout") or "执行失败")
        raise RuntimeError(f"PROCESS_FAILED: {detail[:500]}")
    return outcome


def _as_subprocess_runner(runner):
    """Adapt the bounded workbench runner to subprocess.run's result contract."""

    def run(command, **options):
        timeout = int(options.get("timeout", 1800))
        outcome = runner(
            command,
            cwd=Path(options.get("cwd", Path.cwd())),
            timeout_seconds=timeout,
            max_output_bytes=1024 * 1024,
            env=options.get("env"),
        )
        if (
            isinstance(outcome, dict)
            and outcome.get("started") is not True
            and outcome.get("error_code") == "runtime.internal_error"
        ):
            from .render_mix_adapter import _run_process_tree

            fallback_options = dict(options)
            if fallback_options.pop("capture_output", False):
                fallback_options.setdefault("stdout", subprocess.PIPE)
                fallback_options.setdefault("stderr", subprocess.PIPE)
            return _run_process_tree(command, **fallback_options)
        if not isinstance(outcome, dict) or outcome.get("started") is not True:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=str((outcome or {}).get("stdout") or ""),
                stderr=str((outcome or {}).get("error") or "进程未能启动"),
            )
        if outcome.get("timed_out"):
            raise subprocess.TimeoutExpired(
                command,
                timeout,
                output=outcome.get("stdout"),
                stderr=outcome.get("stderr"),
            )
        return subprocess.CompletedProcess(
            command,
            int(outcome.get("returncode", 1)),
            stdout=str(outcome.get("stdout") or ""),
            stderr=str(outcome.get("stderr") or ""),
        )

    return run


def _python_executable(root: Path) -> Path:
    windows = root / ".venv" / "Scripts" / "python.exe"
    if windows.is_file():
        return windows
    return root / ".venv" / "bin" / "python"


def _validate_words(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳文件无效") from error
    if not isinstance(value, list) or not value:
        raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 没有识别到词级时间戳")
    previous = -1.0
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("w"), str):
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳结构无效")
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳结构无效") from error
        if start < previous or end < start:
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: 词级时间戳顺序无效")
        previous = end
    return value


def transcribe(db, job, project_root, runner):
    """Create task-scoped Whisper timing artifacts without overwriting them."""
    root = Path(project_root).resolve()
    task_root, _brief, _script_path, source_path, script_sha256 = _locked_inputs(
        db, job, root
    )
    artifacts = task_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    srt_path = artifacts / "transcript.srt"
    words_path = artifacts / "transcript.words.json"
    metadata_path = artifacts / "transcription.json"
    source_stat = source_path.stat()
    binding = {
        "task_id": job.task_id,
        "script_sha256": script_sha256,
        "source_path": str(source_path),
        "source_size_bytes": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "model": str(job.payload.get("model") or "small"),
    }
    binding["input_digest"] = _canonical_digest(binding)

    if metadata_path.is_file() and srt_path.is_file() and words_path.is_file():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("TRANSCRIPTION_STALE: 已有转写绑定记录无效") from error
        if existing.get("input_digest") != binding["input_digest"]:
            raise RuntimeError("TRANSCRIPTION_STALE: 锁定输入已变化，不能覆盖旧转写")
        _validate_words(words_path)
        return _action_result(
            "transcribe:complete",
            "语音识别完成",
            {
                "reused": True,
                "srt_path": str(srt_path.resolve()),
                "words_path": str(words_path.resolve()),
            },
        )
    if any(path.exists() for path in (metadata_path, srt_path, words_path)):
        raise RuntimeError("TRANSCRIPTION_PARTIAL: 检测到不完整转写结果，请人工恢复")

    token = job.job_id.replace("/", "_").replace("\\", "_")
    part_srt = artifacts / f".transcript.{token}.part.srt"
    part_words = artifacts / f".transcript.{token}.part.words.json"
    for part in (part_srt, part_words):
        if part.exists():
            part.unlink()
    command = [
        str(_python_executable(root)),
        "-X",
        "utf8",
        str(root / "scripts" / "transcribe.py"),
        str(source_path),
        binding["model"],
        "--srt-output",
        str(part_srt),
        "--words-output",
        str(part_words),
    ]
    try:
        _run_checked(runner, command, cwd=root, timeout_seconds=3600)
        if not part_srt.is_file() or not part_srt.read_text(encoding="utf-8").strip():
            raise RuntimeError("TRANSCRIPT_OUTPUT_INVALID: SRT 输出为空")
        words = _validate_words(part_words)
        os.replace(part_srt, srt_path)
        os.replace(part_words, words_path)
        binding.update(
            {
                "srt_sha256": _file_sha256(srt_path),
                "words_sha256": _file_sha256(words_path),
                "word_count": len(words),
            }
        )
        part_metadata = artifacts / f".transcription.{token}.part.json"
        part_metadata.write_text(
            json.dumps(binding, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(part_metadata, metadata_path)
    except BaseException:
        for part in (part_srt, part_words):
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return _action_result(
        "transcribe:complete",
        "语音识别完成",
        {
            "reused": False,
            "srt_path": str(srt_path.resolve()),
            "words_path": str(words_path.resolve()),
        },
    )


def sync_sources(db, job, project_root, runner):
    """Synchronize the task's frozen source scope and record the stage."""
    _ = runner
    _task_root, task = _task_context(db, job, project_root)
    if latest_gate(db, task["task_id"], "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定最终文案")
    impact_token = job.payload.get("impact_token")
    if impact_token is not None and not isinstance(impact_token, str):
        raise ValueError("IMPACT_TOKEN_INVALID: 素材同步确认令牌无效")
    result = sync_task_sources(
        db,
        task["task_id"],
        impact_token=impact_token,
    )
    stage_payload = {
        "changed": bool(result["changed"]),
        "configuration_changed": bool(result["configuration_changed"]),
        "health_changed": bool(result["health_changed"]),
        "snapshot_ids": list(result["snapshot_ids"]),
        "sources": result["sources"],
    }
    complete_stage(
        db,
        task["task_id"],
        "material_sync",
        input_digest=_canonical_digest(stage_payload),
        details=stage_payload,
    )
    return _action_result(
        "sync_sources:complete",
        "素材增量同步完成",
        result,
    )


def scan_catalog(db, job, project_root, runner):
    """Catalog one registered local material library for the workbench."""
    _ = project_root, runner
    library_id = job.payload.get("library_id")
    if not isinstance(library_id, str) or not library_id.strip():
        raise ValueError("LIBRARY_ID_REQUIRED: 素材检查缺少目录编号")
    summary = scan_library(db, library_id)
    return _action_result(
        "scan:complete",
        "素材目录检查完成",
        {
            "library_id": library_id,
            "catalog": {
                "discovered": summary.discovered,
                "completed": summary.completed,
                "failed": summary.failed,
                "remaining": summary.remaining,
            },
        },
    )


def generate_preview(db, job, project_root, runner):
    """Generate a browser-safe task-scoped proxy without modifying the source."""
    task_root, task = _task_context(db, job, project_root)
    _ = task_root
    asset_id = job.payload.get("asset_id")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError("ASSET_ID_REQUIRED: 代理预览任务缺少素材编号")
    resolve_rough_media(db, task["task_id"], asset_id, "preview")
    ffmpeg = find_ffmpeg(
        os.environ,
        search_roots=default_ffprobe_search_roots(),
    )
    produced = generate_asset_previews(
        db,
        asset_id,
        ffmpeg,
        {"thumbnail", "proxy"},
        runner=_as_subprocess_runner(runner),
    )
    return _action_result(
        "generate_preview:complete",
        "代理预览生成完成",
        {"asset_id": asset_id, **produced},
    )


def recommend_storyboard(db, job, project_root, runner):
    """Generate one paid AI brief, then bind candidates to scoped local assets."""
    root = Path(project_root).resolve()
    task_root, brief, script_path, _source_path, script_sha256 = _locked_inputs(
        db, job, root
    )
    task_id = job.task_id
    artifacts = task_root / "artifacts"
    words_path = artifacts / "transcript.words.json"
    transcription_path = artifacts / "transcription.json"
    if not words_path.is_file() or not transcription_path.is_file():
        raise RuntimeError("TRANSCRIPTION_REQUIRED: 必须先完成语音识别和词级时间戳")
    words = _validate_words(words_path)
    try:
        transcription = json.loads(transcription_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("TRANSCRIPTION_REQUIRED: 转写绑定记录无效") from error
    if transcription.get("script_sha256") != script_sha256:
        raise RuntimeError("TRANSCRIPTION_STALE: 转写未绑定当前锁定文案")

    source_rows = db.connection.execute(
        "SELECT snapshot_id FROM task_source_snapshots WHERE task_id=? ORDER BY rowid",
        (task_id,),
    ).fetchall()
    source_snapshot_ids = [row["snapshot_id"] for row in source_rows]
    if not source_snapshot_ids:
        raise RuntimeError("SOURCE_SYNC_REQUIRED: 必须先完成本次素材增量同步")
    input_binding = {
        "task_id": task_id,
        "script_sha256": script_sha256,
        "transcription_digest": transcription.get("input_digest"),
        "words_sha256": _file_sha256(words_path),
        "source_snapshot_ids": source_snapshot_ids,
        "profile": str(brief.get("subtype") or "zhaoshang"),
    }
    model_capability = "storyboard"
    model_binding = get_task_profile_binding_for_usage(
        db, task_id, model_capability
    )
    if model_binding is None:
        model_capability = "general_text"
        model_binding = get_task_profile_binding_for_usage(
            db, task_id, model_capability
        )
    packaging_binding = (
        get_task_profile_binding_for_usage(db, task_id, "packaging")
        if model_capability == "storyboard"
        else None
    )
    if model_capability == "storyboard" and packaging_binding is None:
        raise RuntimeError(
            "MODEL_BINDING_INCOMPLETE: 数字人规划必须同时冻结分镜模型和包装模型"
        )
    if model_binding is not None:
        input_binding["model_profile_digest"] = model_binding.get("configDigest")
    if packaging_binding is not None:
        input_binding["packaging_profile_digest"] = packaging_binding.get(
            "configDigest"
        )
    input_digest = _canonical_digest(input_binding)
    storyboard_dir = artifacts / "storyboard"
    storyboard_dir.mkdir(parents=True, exist_ok=True)
    brief_path = storyboard_dir / "brief.json"
    plan_path = storyboard_dir / "plan.json"
    metadata_path = storyboard_dir / "recommendation.json"

    metadata = None
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("STORYBOARD_STALE: 分镜绑定记录无效") from error
        if metadata.get("input_digest") != input_digest:
            raise RuntimeError("STORYBOARD_STALE: 锁定文案、时间或素材范围已变化")
        if metadata.get("state") == "complete":
            try:
                completed_content = {
                    "brief": json.loads(brief_path.read_text(encoding="utf-8")),
                    "plan": json.loads(plan_path.read_text(encoding="utf-8")),
                }
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "STORYBOARD_PARTIAL: 已完成分镜文件无效"
                ) from error
            finalized = finalize_digital_storyboard(
                db,
                task_id=task_id,
                project_root=root,
                content=completed_content,
                input_binding=input_binding,
                input_digest=input_digest,
                source="standalone_model",
            )
            return _action_result(
                "recommend_storyboard:complete",
                "AI 分镜候选已生成",
                finalized,
            )

    if metadata is None:
        if brief_path.exists() or plan_path.exists():
            raise RuntimeError("STORYBOARD_PARTIAL: 检测到无绑定的分镜输出")
        node = shutil.which("node") or "node"
        ts_node = root / "node_modules" / "ts-node" / "dist" / "bin.js"
        token = re.sub(r"[^A-Za-z0-9_.-]", "_", job.job_id)
        part_brief = storyboard_dir / f".brief.{token}.part.json"
        part_plan = storyboard_dir / f".plan.{token}.part.json"
        for part in (part_brief, part_plan):
            part.unlink(missing_ok=True)
        command = [
            str(_python_executable(root)),
            "-X",
            "utf8",
            str(root / "scripts" / "plan_brief_runner.py"),
            "--project-root", str(root),
            "--node", str(Path(node).resolve()),
            "--ts-node", str(ts_node.resolve()),
            "--script",
            str(script_path),
            "--brief-output",
            str(part_brief),
            "--plan-output",
            str(part_plan),
            "--speed",
            "1.25",
            "--profile",
            input_binding["profile"],
        ]
        if model_binding is not None:
            command.extend([
                "--catalog-db", str(Path(db.path).resolve()),
                "--task-id", task_id,
                "--capability", model_capability,
            ])
            if packaging_binding is not None:
                command.extend(["--packaging-capability", "packaging"])
        else:
            setting = db.connection.execute(
                "SELECT model FROM capability_settings WHERE capability='general_text'"
            ).fetchone()
            model = setting["model"] if setting is not None else None
            if not model or model == "未选择模型":
                raise RuntimeError(
                    "GENERAL_TEXT_NOT_CONFIGURED: 请先在设置中配置文案与分镜模型"
                )
            command.extend(["--model", str(model)])
        try:
            _run_checked(
                runner,
                command,
                cwd=root,
                timeout_seconds=300,
            )
            ai_brief = _validate_storyboard_brief(part_brief, script_path)
            try:
                ai_plan = json.loads(part_plan.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件无效") from error
            if not isinstance(ai_plan, dict):
                raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件必须是对象")
            os.replace(part_brief, brief_path)
            os.replace(part_plan, plan_path)
        except BaseException:
            part_brief.unlink(missing_ok=True)
            part_plan.unlink(missing_ok=True)
            raise
        metadata = {
            **input_binding,
            "input_digest": input_digest,
            "source": "standalone_model",
            "state": "generated",
            "brief_sha256": _file_sha256(brief_path),
            "plan_sha256": _file_sha256(plan_path),
            "missing": [],
        }
        _atomic_json(metadata_path, metadata)
    else:
        ai_brief = _validate_storyboard_brief(brief_path, script_path)
        try:
            ai_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件无效") from error
        if not isinstance(ai_plan, dict):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 规划文件必须是对象")

    finalized = finalize_digital_storyboard(
        db,
        task_id=task_id,
        project_root=root,
        content={"brief": ai_brief, "plan": ai_plan},
        input_binding=input_binding,
        input_digest=input_digest,
        source="standalone_model",
    )
    return _action_result(
        "recommend_storyboard:complete",
        "AI 分镜候选已生成",
        finalized,
    )


def _render_input_digest(db, task_id: str) -> str:
    gates = {}
    for gate_type in ("brief", "picture_lock", "packaging"):
        row = latest_gate(db, task_id, gate_type)
        gates[gate_type] = None if row is None else dict(row)
    cards = [
        dict(row)
        for row in db.connection.execute(
            "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx,card_id",
            (task_id,),
        ).fetchall()
    ]
    assets = [
        dict(row)
        for row in db.connection.execute(
            "SELECT task_id,asset_id,role FROM task_assets "
            "WHERE task_id=? ORDER BY role,asset_id",
            (task_id,),
        ).fetchall()
    ]
    rework = db.connection.execute(
        """SELECT rework_id,execution_key FROM rework_requests
        WHERE task_id=? AND status='applied'
        ORDER BY updated_at DESC,rowid DESC LIMIT 1""",
        (task_id,),
    ).fetchone()
    return _canonical_digest({
        "gates": gates,
        "cards": cards,
        "assets": assets,
        "rework": None if rework is None else dict(rework),
    })


def _action_state_path(artifacts: Path, action: str, input_digest: str) -> Path:
    """Keep each input generation, including the previous fixed-name record, immutable."""
    versioned = artifacts / f"{action}.{input_digest}.json"
    if versioned.exists():
        return versioned
    legacy = artifacts / f"{action}.json"
    if legacy.is_file():
        try:
            previous = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("ACTION_STATE_INVALID: 旧动作绑定记录无效") from error
        if previous.get("input_digest") == input_digest:
            return legacy
    return versioned


def _render_snapshot_current(db, task_id: str, snapshot: dict) -> bool:
    from .render_mix_adapter import verify_catalog_snapshot, verify_dependency_snapshot, RenderMixJobError
    try:
        return verify_dependency_snapshot(snapshot.get("dependency_snapshot") or {}) and verify_catalog_snapshot(
            db, task_id, Path(snapshot["output_path"]), snapshot.get("dependency_snapshot") or {},
        )
    except (OSError, RenderMixJobError):
        return False


def _validate_completed_render(db, task_id: str, state: dict) -> None:
    candidate = latest_review_candidate(db, task_id)
    bound = state.get("result", {}).get("snapshots", {})
    if candidate is None or candidate["qa_snapshot_id"] != bound.get("qa_snapshot_id"):
        raise RuntimeError("ACTION_CANDIDATE_CHANGED: 已完成动作不再绑定当前审片候选")
    if not _render_snapshot_current(db, task_id, candidate):
        raise RuntimeError("ACTION_DEPENDENCY_CHANGED: 已完成动作的门禁或依赖已变化")
    if file_content_signature(candidate["output_path"]) != candidate["output_signature"]:
        raise RuntimeError("ACTION_OUTPUT_CHANGED: 候选文件与快照内容不一致")


def _completed_action(path: Path, input_digest: str, output_path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("ACTION_STATE_INVALID: 后台动作绑定记录无效") from error
    if metadata.get("input_digest") != input_digest:
        raise RuntimeError("ACTION_STATE_STALE: 已完成动作与当前锁定输入不一致")
    if not output_path.is_file() or metadata.get("output_sha256") != _file_sha256(output_path):
        raise RuntimeError("ACTION_OUTPUT_CHANGED: 已完成动作的输出文件已经变化")
    return metadata


def render_and_qa(db, job, project_root, runner):
    """Render one immutable candidate and run QA, reusing trusted output on reentry."""
    root = Path(project_root).resolve()
    task_root, _brief, script_path, _source_path, _script_sha256 = _locked_inputs(
        db, job, root
    )
    task_id = job.task_id
    artifacts = task_root / "artifacts"
    words_path = artifacts / "transcript.words.json"
    storyboard_dir = artifacts / "storyboard"
    brief_path = storyboard_dir / "brief.json"
    if not words_path.is_file() or not brief_path.is_file():
        raise RuntimeError("STORYBOARD_REQUIRED: 必须先完成转写和 AI 分镜推荐")
    semantics_path = _subtitle_semantics(
        storyboard_dir,
        _validate_storyboard_brief(brief_path, script_path),
    )

    input_digest = _render_input_digest(db, task_id)
    state_path = _action_state_path(artifacts, "render-action", input_digest)
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            recorded_output = Path(state["output_path"]).resolve()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("ACTION_STATE_INVALID: 渲染动作绑定记录无效") from error
        completed = _completed_action(state_path, input_digest, recorded_output)
        _validate_completed_render(db, task_id, completed)
        return _action_result(
            "render_and_qa:complete",
            "精剪成片与自动质检完成",
            {**completed["result"], "reused": True},
        )

    candidate = latest_review_candidate(db, task_id)
    retry = latest_retryable_render_snapshot(db, task_id)
    if candidate is not None and _render_snapshot_current(db, task_id, candidate):
        output_path = Path(candidate["output_path"]).resolve()
        if not output_path.is_file():
            raise RuntimeError("RENDER_OUTPUT_MISSING: 已审片候选文件不存在")
        if file_content_signature(output_path) != candidate["output_signature"]:
            raise RuntimeError("ACTION_OUTPUT_CHANGED: 候选文件与快照内容不一致")
        result = {
            "status": "awaiting_human_review",
            "output_path": str(output_path),
            "reused": True,
            "snapshots": candidate,
        }
    else:
        if retry is not None and _render_snapshot_current(db, task_id, retry):
            output_path = Path(retry["output_path"]).resolve()
        else:
            version_no = db.connection.execute(
                "SELECT COUNT(*) + 1 FROM render_snapshots "
                "WHERE task_id=? AND stage='mix'",
                (task_id,),
            ).fetchone()[0]
            output_path = (
                root / "out" / task_id / f"candidate-v{version_no}" / "video-final.mp4"
            ).resolve()
            expected_root = (root / "out" / task_id).resolve()
            if not _contained(output_path, expected_root):
                raise RuntimeError("RENDER_OUTPUT_INVALID: 候选成片目录越界")
            if output_path.exists():
                raise RuntimeError("RENDER_OUTPUT_EXISTS: 未绑定输出已存在，不能覆盖")
        work_dir = task_root / "render" / output_path.parent.name
        work_dir.mkdir(parents=True, exist_ok=True)
        subprocess_runner = _as_subprocess_runner(runner)
        assembled = assemble(
            db,
            task_id,
            render=True,
            output_path=output_path,
            work_dir=work_dir,
            runner=subprocess_runner,
            subtitle_runner=subprocess_runner,
            subtitle_sources={
                "script_path": str(script_path.resolve()),
                "words_path": str(words_path.resolve()),
                "semantics_path": str(semantics_path.resolve()),
            },
        )
        if assembled.get("status") not in {"awaiting_human_review", "qa_blocked"}:
            raise RuntimeError(
                f"RENDER_WORKFLOW_BLOCKED: {assembled.get('message') or assembled.get('status')}"
            )
        result = dict(assembled)
        result["reused"] = False

    if result["status"] == "awaiting_human_review":
        output_path = Path(result["output_path"]).resolve()
        state = {
            "input_digest": input_digest,
            "output_path": str(output_path),
            "output_sha256": _file_sha256(output_path),
            "result": result,
        }
        _atomic_json(state_path, state)
        return _action_result(
            "render_and_qa:complete",
            "精剪成片与自动质检完成",
            result,
        )
    return _action_result(
        "render_and_qa:qa_blocked",
        "自动质检发现阻断项",
        result,
    )


def build_publish_package(db, job, project_root, runner):
    """Build a release package from the exact human-approved review candidate."""
    _ = runner
    root = Path(project_root).resolve()
    task_root, _task = _task_context(db, job, root)
    task_id = job.task_id
    candidate = latest_review_candidate(db, task_id)
    if candidate is None:
        raise RuntimeError("REVIEW_CANDIDATE_MISSING: 没有可发布的完整审片候选")
    expected_candidate_id = str(
        job.payload.get("candidate_id") or candidate.get("candidate_id") or ""
    )
    if candidate.get("candidate_id") != expected_candidate_id:
        raise RuntimeError("PUBLISH_CANDIDATE_STALE: 发布任务绑定的候选已不是当前版本")
    output_path = Path(candidate["output_path"]).resolve()
    if not output_path.is_file():
        raise RuntimeError("RENDER_OUTPUT_MISSING: 完整审片对应的候选成片不存在")
    payload = {
        "platform": str(job.payload.get("platform") or "默认"),
        "title": job.payload.get("title"),
        "description": job.payload.get("description"),
        "tags": list(job.payload.get("tags") or []),
        "cover_choice": str(job.payload.get("cover_choice") or "candidate-1"),
        "external_cover_path": job.payload.get("external_cover_path"),
        "delivery_type": str(job.payload.get("delivery_type") or "standard"),
    }
    input_digest = _canonical_digest(
        {
            "candidate": {
                "qa_snapshot_id": candidate.get("qa_snapshot_id"),
                "qa_report_id": candidate.get("qa_report_id"),
                "output_path": str(output_path),
                "output_sha256": _file_sha256(output_path),
            },
            "publish": payload,
        }
    )
    artifacts = task_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    state_path = _action_state_path(artifacts, "publish-action", input_digest)
    # Re-enter the trusted publish path on every retry to revalidate current approval
    # and package integrity. The package builder owns content idempotency.
    previous_state = _completed_action(state_path, input_digest, output_path)
    assembled = assemble(
        db,
        task_id,
        render=True,
        output_path=output_path,
        platform=payload["platform"],
        title=payload["title"],
        description=payload["description"],
        tags=payload["tags"],
        cover_choice=payload["cover_choice"],
        external_cover_path=payload["external_cover_path"],
        delivery_type=payload["delivery_type"],
        candidate_id=expected_candidate_id,
    )
    if assembled.get("status") not in {"published", "publish_blocked"}:
        raise RuntimeError(
            f"PUBLISH_WORKFLOW_BLOCKED: {assembled.get('message') or assembled.get('status')}"
        )
    result = {**assembled, "reused": bool(previous_state) or bool(assembled.get("publish", {}).get("reused"))}
    _atomic_json(
        state_path,
        {
            "input_digest": input_digest,
            "output_path": str(output_path),
            "output_sha256": _file_sha256(output_path),
            "result": result,
        },
    )
    return _action_result(
        "build_publish_package:complete",
        "发布包生成完成",
        result,
    )


def run_packaging_preview_action(db, job, project_root, runner=None) -> dict:
    """Execute one durable packaging-preview job without changing task gates."""
    from .packaging_preview import get_preview_artifact, render_packaging_preview

    preview_id = job.payload.get("preview_id")
    if not isinstance(preview_id, str) or not preview_id:
        raise ValueError("PACKAGING_PREVIEW_ID_REQUIRED: 预览任务缺少记录编号")
    artifact = get_preview_artifact(db, preview_id)
    if artifact is None:
        raise KeyError(f"找不到包装预览：{preview_id}")
    task_id = job.task_id or job.payload.get("task_id")
    if artifact["task_id"] != task_id:
        raise PermissionError("PACKAGING_PREVIEW_NOT_OWNED: 预览不属于当前任务")
    result = render_packaging_preview(
        db, preview_id, Path(project_root).resolve(), runner
    )
    message = (
        "包装预览已生成"
        if result["status"] == "ready"
        else "包装输入已变化，预览已过期"
    )
    return _action_result("packaging_preview:complete", message, result)


def run_packaging_catalog_check_action(
    db, job, project_root, runner=None
) -> dict:
    from .packaging_catalog_check import run_packaging_catalog_check

    check_id = job.payload.get("check_id")
    if not isinstance(check_id, str) or not check_id:
        raise ValueError("PACKAGING_CATALOG_CHECK_ID_REQUIRED: 检查任务缺少记录编号")
    result = run_packaging_catalog_check(
        db, Path(project_root).resolve(), check_id=check_id
    )
    return _action_result(
        "packaging_catalog_check:complete", "包装素材检查完成", result
    )


def build_action_handlers(db, project_root, runner=None) -> dict:
    """Bind route-aware trusted actions to one worker-owned database connection."""
    from .ad_actions import (
        build_ad_publish_package,
        recommend_ad_storyboard,
        render_ad_and_qa,
    )

    process_runner = runner or _default_process_runner
    root = Path(project_root).resolve()

    def bind(digital_action, ad_action=None):
        def handle(job):
            task = get_task(db, job.task_id or job.payload.get("task_id"))
            if task is None:
                raise KeyError("任务不存在")
            action = (
                ad_action
                if task.get("task_type") == "ad_mix" and ad_action is not None
                else digital_action
            )
            return action(db, job, root, process_runner)
        return handle

    return {
        "scan": lambda job: scan_catalog(db, job, root, process_runner),
        "generate_preview": bind(generate_preview),
        "transcribe": bind(transcribe),
        "sync_sources": bind(sync_sources),
        "recommend_storyboard": bind(recommend_storyboard, recommend_ad_storyboard),
        "render_and_qa": bind(render_and_qa, render_ad_and_qa),
        "build_publish_package": bind(
            build_publish_package, build_ad_publish_package
        ),
        "packaging_preview": lambda job: run_packaging_preview_action(
            db, job, root, process_runner
        ),
        "packaging_catalog_check": lambda job: run_packaging_catalog_check_action(
            db, job, root, process_runner
        ),
    }


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _subtitle_semantics(storyboard_dir: Path, ai_brief: dict) -> Path:
    semantics = {
        "keywords": ai_brief.get("keywords"),
        "punchLines": ai_brief.get("punchLines"),
    }
    for key, values in semantics.items():
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise RuntimeError(
                f"STORYBOARD_OUTPUT_INVALID: AI 分镜 {key} 必须是非空字符串数组"
            )
    path = Path(storyboard_dir) / "semantics.json"
    expected = json.dumps(
        semantics, ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n"
    if path.is_file():
        if path.read_text(encoding="utf-8") != expected:
            raise RuntimeError("STORYBOARD_STALE: 字幕语义派生产物与 AI 分镜不一致")
    else:
        _atomic_json(path, semantics)
    return path


def _validate_storyboard_brief(path: Path, script_path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜输出不是有效 JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("broll"), list):
        raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜输出结构无效")
    script = script_path.read_text(encoding="utf-8")
    for item in value["broll"]:
        if not isinstance(item, dict):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜项结构无效")
        anchor = item.get("anchorText")
        if not isinstance(anchor, str) or not anchor or anchor not in script:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 锚点不属于锁定原文")
        if item.get("kind") not in {"broll", "image"}:
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜素材类型无效")
        if not isinstance(item.get("query"), str):
            raise RuntimeError("STORYBOARD_OUTPUT_INVALID: AI 分镜缺少素材查询词")
    return value


def _anchor_time(
    script: str,
    words: list[dict],
    anchor: str,
) -> tuple[float, float]:
    """Mirror src/timeline.ts: map locked-script positions onto word timing.

    Whisper may return traditional characters, verbalized numbers, or imperfect
    proper nouns. The locked script is authoritative, so exact matching against
    ASR text would incorrectly reject valid anchors.
    """
    anchor_offset = script.find(anchor)
    if anchor_offset < 0:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: AI 锚点不属于锁定文案")

    timeline = []
    for item in words:
        characters = [char for char in item["w"] if not char.isspace()]
        if not characters:
            continue
        start = float(item["start"])
        end = float(item["end"])
        span = (end - start) / len(characters)
        timeline.extend(
            {
                "start": start + span * index,
                "end": start + span * (index + 1),
            }
            for index in range(len(characters))
        )
    if not timeline:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: 没有可用词级时间")

    spoken_rank = {}
    rank = 0
    for index, character in enumerate(script):
        if re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff%]", character):
            spoken_rank[index] = rank
            rank += 1
    if rank == 0:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: 锁定文案没有可定位文字")

    anchor_ranks = [
        spoken_rank[index]
        for index in range(anchor_offset, anchor_offset + len(anchor))
        if index in spoken_rank
    ]
    if not anchor_ranks:
        raise RuntimeError("STORYBOARD_TIMING_MISSING: AI 锚点没有可定位文字")

    def timing_at(position: int, edge: str) -> float:
        projected = (position / rank) * len(timeline)
        timeline_index = int(projected + 0.5)
        timeline_index = min(max(timeline_index, 0), len(timeline) - 1)
        return float(timeline[timeline_index][edge])

    start = max(0.0, timing_at(anchor_ranks[0], "start"))
    end = max(start + 0.5, timing_at(anchor_ranks[-1] + 1, "end"))
    return start, end


def _storyboard_cards(db, task_id: str, script: str, words: list[dict], ai_brief: dict):
    digital = db.connection.execute(
        "SELECT asset_id FROM task_assets WHERE task_id=? AND role='digital_human'",
        (task_id,),
    ).fetchall()
    if len(digital) != 1:
        raise RuntimeError("DIGITAL_HUMAN_COUNT_INVALID: 任务必须恰好绑定一个数字人口播源")
    duration = float(words[-1]["end"])
    cards = [
        {
            "card_id": f"{task_id}#0",
            "idx": 0,
            "narrative_text": script,
            "narrative_start": float(words[0]["start"]),
            "narrative_end": duration,
            "asset_id": digital[0]["asset_id"],
            "role": "digital_human",
            "mix_form": "数字人全屏",
            "audio_policy": "keep",
            "source_type": "用户指定",
            "system_suggestion": "锁定的数字人口播主轴，声音和顺序不可切断",
        }
    ]
    eligible = list_eligible_assets(db, task_id)
    missing = []
    for item in ai_brief["broll"]:
        expected_kind = "image" if item["kind"] == "image" else "video"
        candidates = [
            {
                "id": candidate["asset_id"],
                "text": candidate["relative_path"],
                "candidate": candidate,
            }
            for candidate in eligible
            if candidate["media_kind"] == expected_kind
            and candidate["asset_id"] != digital[0]["asset_id"]
        ]
        if not candidates:
            missing.append(dict(item))
            continue
        selected = rank(item["query"], candidates)[0]["candidate"]
        start, end = _anchor_time(script, words, item["anchorText"])
        image = selected["media_kind"] == "image"
        cards.append(
            {
                "card_id": f"{task_id}#{len(cards)}",
                "idx": len(cards),
                "narrative_text": item["anchorText"],
                "narrative_start": start,
                "narrative_end": end,
                "asset_id": selected["asset_id"],
                "role": "information_card" if image else "broll",
                "mix_form": "画中画" if image else "broll全屏",
                "audio_policy": "mute",
                "source_type": "系统推荐",
                "system_suggestion": f"{item['query']}：{item.get('prompt') or ''}".rstrip("："),
            }
        )
    return cards, missing
