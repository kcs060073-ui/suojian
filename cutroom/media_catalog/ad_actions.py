"""Task-scoped information-flow ad planning and rendering actions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

from .ad_subtitles import layout_ad_subtitles
from .reference_editing import apply_reference_policy
from .agent_artifacts import get_agent_artifact
from .agent_context import current_agent_input_digest
from .candidate_versions import record_candidate_qa
from .material_authorization import get_material_authorization, shot_authorization
from .packaging_plan import find_item, get_plan
from .packaging_layers import PackagingTimingContext, compile_packaging_layers
from .model_profiles import get_task_profile_binding_for_usage
from .provenance import latest_gate
from .publish import build_publish_package as create_publish_package
from .qa import run_qa
from .qa_evidence import qa_evidence_complete
from .scene_cards import add_scene_card, list_scene_cards
from .snapshots import (
    file_content_signature,
    latest_review_candidate,
    latest_retryable_render_snapshot,
    record_qa_review_snapshot,
    record_render_dependency_snapshot,
)
from .task_sources import list_eligible_assets
from .tasks import add_asset, get_task
from .workflow import complete_stage, project_gates
from .workbench_actions import (
    _action_result,
    _canonical_digest,
    _contained,
    _file_sha256,
    _run_checked,
    _render_input_digest,
)


def _task_context(db, job, project_root) -> tuple[Path, dict, dict, Path, str]:
    root = Path(project_root).resolve()
    task_id = job.task_id or job.payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("TASK_ID_REQUIRED: 后台动作缺少任务编号")
    if any(part in task_id for part in ("/", "\\")) or task_id in {".", ".."}:
        raise ValueError("INVALID_TASK_ID: 任务编号包含非法路径字符")
    task = get_task(db, task_id)
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    if task.get("task_type") != "ad_mix":
        raise ValueError("UNSUPPORTED_ROUTE: 当前动作只支持信息流广告混剪")
    if latest_gate(db, task_id, "brief") is None:
        raise RuntimeError("BRIEF_GATE_MISSING: 必须先锁定广告最终文案")
    brief = task.get("brief")
    if not isinstance(brief, dict) or brief.get("route") != "ad_mix":
        raise ValueError("BRIEF_INVALID: 广告任务记录无效")
    script_record = brief.get("final_script")
    if not isinstance(script_record, dict):
        raise ValueError("BRIEF_INVALID: 广告任务缺少锁定文案")
    task_root = (root / "runtime" / "workbench" / "tasks" / task_id).resolve()
    trusted = (root / "runtime" / "workbench" / "tasks").resolve()
    if not _contained(task_root, trusted):
        raise ValueError("INVALID_TASK_ID: 任务目录越界")
    script_path = (task_root / str(script_record.get("relative_path") or "")).resolve()
    if not _contained(script_path, task_root) or not script_path.is_file():
        raise ValueError("LOCKED_SCRIPT_MISSING: 锁定广告文案不存在")
    digest = _file_sha256(script_path)
    if digest != script_record.get("sha256"):
        raise RuntimeError("LOCKED_SCRIPT_CHANGED: 锁定广告文案已经变化")
    return task_root, task, brief, script_path, digest


def _ensure_scene_asset_bindings(db, task_id: str, asset_ids) -> None:
    expected = {asset_id for asset_id in asset_ids if asset_id is not None}
    if not expected:
        return
    existing = {
        row["asset_id"]
        for row in db.connection.execute(
            "SELECT asset_id FROM task_assets WHERE task_id=?",
            (task_id,),
        ).fetchall()
    }
    missing = expected - existing
    if missing and db.connection.execute(
        "SELECT 1 FROM picture_lock WHERE task_id=?",
        (task_id,),
    ).fetchone() is not None:
        raise RuntimeError(
            "STORYBOARD_ASSET_BINDING_STALE: 画面已锁定但广告素材主账本不完整"
        )
    for asset_id in sorted(missing):
        add_asset(db, task_id, asset_id, "broll")


def _capability_model(db, capability: str, missing_code: str) -> str:
    row = db.connection.execute(
        "SELECT model FROM capability_settings WHERE capability=?",
        (capability,),
    ).fetchone()
    model = None if row is None else row["model"]
    if not model or str(model).startswith("未选择"):
        raise RuntimeError(f"{missing_code}: 请先在设置中配置对应服务")
    return str(model)


def _validate_ad_outputs(
    script: str,
    ratio: str,
    plan_path: Path,
    timing_path: Path,
    voice_path: Path,
) -> tuple[dict, dict]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜或配音时间表无效") from error
    plan_segments = plan.get("segments") if isinstance(plan, dict) else None
    timing_segments = timing.get("segments") if isinstance(timing, dict) else None
    if (
        plan.get("ratio") != ratio
        or not isinstance(plan_segments, list)
        or not plan_segments
        or not isinstance(timing_segments, list)
        or len(timing_segments) != len(plan_segments)
        or not voice_path.is_file()
        or voice_path.stat().st_size <= 0
    ):
        raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜、时间表或配音文件不完整")
    compact = lambda value: re.sub(r"\s+", "", value)
    planned_text = "".join(str(item.get("text") or "") for item in plan_segments)
    if compact(planned_text) != compact(script):
        raise RuntimeError("AD_COPY_COVERAGE_INVALID: 分镜文案必须逐字完整覆盖锁定原文")

    previous = 0.0
    for index, (segment, timed) in enumerate(zip(plan_segments, timing_segments, strict=True)):
        if not isinstance(segment, dict) or not isinstance(timed, dict):
            raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜项结构无效")
        text = segment.get("text")
        if (
            not isinstance(text, str)
            or timed.get("text") != text
            or segment.get("type") not in {"video", "image"}
            or not isinstance(segment.get("query"), str)
            or not isinstance(segment.get("prompt"), str)
        ):
            raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜字段无效")
        try:
            start = float(timed["start"])
            end = float(timed["end"])
            duration = float(timed["dur"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("AD_OUTPUT_INVALID: 配音时间字段无效") from error
        if index == 0 and abs(start) > 0.01:
            raise RuntimeError("AD_OUTPUT_INVALID: 配音时间表必须从 0 开始")
        if abs(start - previous) > 0.02 or end <= start or abs(duration - (end - start)) > 0.03:
            raise RuntimeError("AD_OUTPUT_INVALID: 配音时间表不连续")
        previous = end
    try:
        total = float(timing["total"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("AD_OUTPUT_INVALID: 配音总时长无效") from error
    if abs(total - previous) > 0.03:
        raise RuntimeError("AD_OUTPUT_INVALID: 配音总时长与逐句时间不一致")
    return plan, timing


def _match_assets(segments: list[dict], assets: list[dict]) -> list[dict | None]:
    visual = [
        asset for asset in assets if asset.get("media_kind") in {"video", "image"}
    ]
    if not visual:
        return [None for _ in segments]
    unused = set(range(len(visual)))
    result = []
    for segment in segments:
        query = str(segment.get("query") or "").lower()
        scored = []
        for index, asset in enumerate(visual):
            name = str(asset.get("relative_path") or "").lower()
            overlap = len(set(query) & set(name))
            score = (100 if query and query in name else 0) + overlap
            scored.append((score, index in unused, -index, index))
        _score, _unused, _order, chosen = max(scored)
        result.append(visual[chosen])
        unused.discard(chosen)
    return result


def _validate_ad_plan(script: str, ratio: str, plan: dict) -> None:
    segments = plan.get("segments") if isinstance(plan, dict) else None
    if plan.get("ratio") != ratio or not isinstance(segments, list) or not segments:
        raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜结构无效")
    compact = lambda value: re.sub(r"\s+", "", value)
    planned_text = "".join(str(item.get("text") or "") for item in segments)
    if compact(planned_text) != compact(script):
        raise RuntimeError("AD_COPY_COVERAGE_INVALID: 分镜文案必须逐字完整覆盖锁定原文")
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or not isinstance(segment.get("text"), str)
            or segment.get("type") not in {"video", "image"}
            or not isinstance(segment.get("query"), str)
            or not isinstance(segment.get("prompt"), str)
        ):
            raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜字段无效")


def _write_json_atomically(path: Path, value: dict) -> None:
    part = path.with_name(f".{path.name}.part")
    try:
        part.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _require_approved_tts_job(db, job_id: object, task_id: str) -> None:
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError(
            "TTS_COST_APPROVAL_REQUIRED: Agent 配音必须绑定已批准的持久任务"
        )
    row = db.connection.execute(
        """
        SELECT j.task_id, p.cost_status
        FROM jobs AS j JOIN job_payloads AS p USING(job_id)
        WHERE j.job_id=?
        """,
        (job_id,),
    ).fetchone()
    if (
        row is None
        or row["task_id"] != task_id
        or row["cost_status"] != "approved"
    ):
        raise RuntimeError(
            "TTS_COST_APPROVAL_REQUIRED: Agent 配音费用尚未明确批准"
        )


def _persist_ad_cards(db, task_id: str, plan: dict, timing: dict) -> list[dict]:
    assets = list_eligible_assets(db, task_id)
    matches = _match_assets(plan["segments"], assets)
    _ensure_scene_asset_bindings(
        db,
        task_id,
        [asset["asset_id"] for asset in matches if asset is not None],
    )
    existing = list_scene_cards(db, task_id)
    if existing:
        expected = [
            (
                index,
                segment["text"],
                float(timed["start"]),
                float(timed["end"]),
                None if asset is None else asset["asset_id"],
            )
            for index, (segment, timed, asset) in enumerate(
                zip(plan["segments"], timing["segments"], matches, strict=True)
            )
        ]
        actual = [
            (
                card["idx"],
                card["narrative_text"],
                float(card["narrative_start"]),
                float(card["narrative_end"]),
                card["asset_id"],
            )
            for card in existing
        ]
        if actual != expected:
            raise RuntimeError(
                "STORYBOARD_EXISTS: 任务已有不同广告分镜，不能静默覆盖"
            )
        return existing
    for index, (segment, timed, asset) in enumerate(
        zip(plan["segments"], timing["segments"], matches, strict=True)
    ):
        add_scene_card(
            db,
            task_id,
            index,
            narrative_text=segment["text"],
            narrative_start=float(timed["start"]),
            narrative_end=float(timed["end"]),
            asset_id=None if asset is None else asset["asset_id"],
            mix_form="broll全屏",
            audio_policy_draft="replace",
            source_type="缺口替代" if asset is None else "系统推荐",
            system_suggestion=(
                f"搜索词：{segment['query']}；生成提示：{segment['prompt']}"
            ),
        )
    return list_scene_cards(db, task_id)


def finalize_ad_storyboard(
    db,
    *,
    task_id: str,
    project_root: Path | str,
    plan: dict,
    input_binding: dict,
    input_digest: str,
    runner,
    source: str,
) -> dict:
    """Finalize one ad plan; Agent inputs invoke only task-scoped TTS."""
    if source not in {"standalone_model", "agent_artifact"}:
        raise ValueError(
            "STORYBOARD_SOURCE_INVALID: 规划来源必须是 standalone_model 或 agent_artifact"
        )
    if not isinstance(input_binding, dict) or input_binding.get("task_id") != task_id:
        raise ValueError("STORYBOARD_INPUT_INVALID: 广告规划未绑定当前任务")
    if not isinstance(input_digest, str) or not input_digest:
        raise ValueError("STORYBOARD_INPUT_INVALID: 广告规划输入摘要缺失")

    root = Path(project_root).resolve()
    task_root, task, brief, script_path, script_sha = _task_context(
        db,
        SimpleNamespace(task_id=task_id, payload={"task_id": task_id}),
        root,
    )
    if input_binding.get("script_sha256") != script_sha:
        raise RuntimeError("STORYBOARD_STALE: 广告规划未绑定当前锁定文案")
    script = script_path.read_text(encoding="utf-8")
    ratio = str(brief["aspect_ratio"])
    _validate_ad_plan(script, ratio, plan)

    ad_dir = task_root / "artifacts" / "ad"
    plan_path = ad_dir / "adplan.json"
    timing_path = ad_dir / "adtiming.json"
    voice_path = ad_dir / "voice.mp3"
    metadata_path = ad_dir / "recommendation.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("STORYBOARD_STALE: 广告分镜绑定记录无效") from error
        if metadata.get("input_digest") != input_digest:
            raise RuntimeError("STORYBOARD_STALE: 文案、配音或素材范围已经变化")
        recorded_source = metadata.get("source")
        if recorded_source is not None and recorded_source != source:
            raise RuntimeError("STORYBOARD_SOURCE_MISMATCH: 已有广告规划来源不一致")
        try:
            existing_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("STORYBOARD_PARTIAL: 广告分镜文件无效") from error
        if _canonical_digest(existing_plan) != _canonical_digest(plan):
            raise RuntimeError("STORYBOARD_EXISTS: 已有广告规划内容不一致")
        plan, timing = _validate_ad_outputs(
            script,
            ratio,
            plan_path,
            timing_path,
            voice_path,
        )
        cards = list_scene_cards(db, task_id)
        if len(cards) != len(plan["segments"]):
            raise RuntimeError("STORYBOARD_STALE: 广告分镜与镜头账本数量不一致")
        _ensure_scene_asset_bindings(
            db,
            task_id,
            [card["asset_id"] for card in cards],
        )
        if recorded_source is None:
            metadata["source"] = source
            _write_json_atomically(metadata_path, metadata)
        complete_stage(
            db,
            task_id,
            "ai_storyboard",
            input_digest=input_digest,
            details={
                "scene_card_count": len(cards),
                "missing_count": sum(card["asset_id"] is None for card in cards),
                "source": source,
            },
        )
        return {
            "reused": True,
            "scene_card_count": len(cards),
            "missing_count": sum(card["asset_id"] is None for card in cards),
            "plan_path": str(plan_path),
            "timing_path": str(timing_path),
            "voice_path": str(voice_path),
            "source": source,
        }

    ad_dir.mkdir(parents=True, exist_ok=True)
    if source == "agent_artifact":
        if plan_path.is_file():
            try:
                existing_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError("STORYBOARD_PARTIAL: Agent 广告规划文件无效") from error
            if _canonical_digest(existing_plan) != _canonical_digest(plan):
                raise RuntimeError("STORYBOARD_EXISTS: 已有 Agent 广告规划内容不一致")
        else:
            _write_json_atomically(plan_path, plan)
        if not timing_path.is_file() or not voice_path.is_file():
            _require_approved_tts_job(db, input_binding.get("job_id"), task_id)
            python = root / ".venv" / "Scripts" / "python.exe"
            if not python.is_file():
                python = root / ".venv" / "bin" / "python"
            node = root / "node.exe"
            if not node.is_file():
                found = shutil.which("node")
                node = Path(found) if found else node
            ts_node = root / "node_modules" / "ts-node" / "dist" / "bin.js"
            command = [
                str(python),
                "-X",
                "utf8",
                str(root / "scripts" / "ad_tts_runner.py"),
                "--project-root",
                str(root),
                "--node",
                str(node),
                "--ts-node",
                str(ts_node),
                "--catalog-db",
                str(Path(db.path).resolve()),
                "--task-id",
                task_id,
                "--plan-input",
                str(plan_path),
                "--timing-output",
                str(timing_path),
                "--voice-output",
                str(voice_path),
                "--segments-dir",
                str(ad_dir / "segments"),
            ]
            _run_checked(runner, command, cwd=root, timeout_seconds=3600)
    else:
        if not plan_path.is_file():
            raise RuntimeError("AD_OUTPUT_INVALID: 独立模式没有生成广告分镜文件")
        try:
            generated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜文件无效") from error
        if _canonical_digest(generated_plan) != _canonical_digest(plan):
            raise RuntimeError("STORYBOARD_EXISTS: 模型输出与落地广告规划不一致")

    plan, timing = _validate_ad_outputs(
        script,
        ratio,
        plan_path,
        timing_path,
        voice_path,
    )
    cards = _persist_ad_cards(db, task_id, plan, timing)
    metadata = {
        **input_binding,
        "input_digest": input_digest,
        "source": source,
        "scene_card_count": len(cards),
        "missing_count": sum(card["asset_id"] is None for card in cards),
    }
    _write_json_atomically(metadata_path, metadata)
    complete_stage(
        db,
        task_id,
        "ai_storyboard",
        input_digest=input_digest,
        details={
            "scene_card_count": len(cards),
            "missing_count": metadata["missing_count"],
            "source": source,
        },
    )
    return {
        "reused": False,
        "scene_card_count": len(cards),
        "missing_count": metadata["missing_count"],
        "plan_path": str(plan_path),
        "timing_path": str(timing_path),
        "voice_path": str(voice_path),
        "source": source,
    }


def recommend_ad_storyboard(db, job, project_root, runner):
    """Create strict full-copy ad segments, TTS timing, and scoped scene cards."""

    root = Path(project_root).resolve()
    task_root, task, brief, script_path, script_sha = _task_context(
        db, job, root
    )
    source_rows = db.connection.execute(
        "SELECT snapshot_id FROM task_source_snapshots WHERE task_id=? ORDER BY rowid",
        (task["task_id"],),
    ).fetchall()
    if not source_rows:
        raise RuntimeError("SOURCE_SYNC_REQUIRED: 必须先完成本次素材增量同步")
    if task.get("orchestration_mode") == "agent":
        artifact_id = job.payload.get("artifact_id")
        artifact = (
            get_agent_artifact(db, artifact_id)
            if isinstance(artifact_id, str)
            else None
        )
        if (
            artifact is None
            or artifact["task_id"] != task["task_id"]
            or artifact["artifact_type"] != "ad_storyboard"
            or artifact["status"] != "accepted"
        ):
            raise RuntimeError(
                "AGENT_ARTIFACT_REQUIRED: Agent 广告任务缺少已接受的规划产物"
            )
        if artifact["input_digest"] != current_agent_input_digest(
            db,
            task["task_id"],
            root,
        ):
            raise RuntimeError(
                "AGENT_INPUT_STALE: Agent 广告规划已不匹配当前输入"
            )
        content = artifact["content"]
        plan = content.get("plan") if isinstance(content, dict) else None
        if not isinstance(plan, dict):
            raise RuntimeError(
                "AGENT_CONTENT_INVALID: Agent 广告规划必须包含 plan 对象"
            )
        finalized = finalize_ad_storyboard(
            db,
            task_id=task["task_id"],
            project_root=root,
            plan=plan,
            input_binding={
                "task_id": task["task_id"],
                "script_sha256": script_sha,
                "source_snapshot_ids": [
                    row["snapshot_id"] for row in source_rows
                ],
                "ratio": brief["aspect_ratio"],
                "job_id": job.job_id,
                "artifact_id": artifact_id,
            },
            input_digest=artifact["input_digest"],
            runner=runner,
            source="agent_artifact",
        )
        return _action_result(
            "recommend_storyboard:complete",
            "Agent 广告分镜与配音已生成",
            finalized,
        )
    text_capability = "storyboard"
    general_binding = get_task_profile_binding_for_usage(
        db, task["task_id"], text_capability
    )
    if general_binding is None:
        text_capability = "general_text"
        general_binding = get_task_profile_binding_for_usage(
            db, task["task_id"], text_capability
        )
    tts_binding = get_task_profile_binding_for_usage(
        db, task["task_id"], "tts"
    )
    if (general_binding is None) != (tts_binding is None):
        raise RuntimeError(
            "MODEL_BINDING_INCOMPLETE: 广告任务必须同时冻结文案模型和配音模型"
        )
    if general_binding is not None:
        general_model = str(general_binding["model"])
        voice = str(tts_binding.get("profileSettings", {}).get("voice") or "")
        if not voice:
            raise RuntimeError("TTS_VOICE_MISSING: 冻结的配音配置缺少音色")
    else:
        general_model = _capability_model(
            db, "general_text", "GENERAL_TEXT_NOT_CONFIGURED"
        )
        voice = _capability_model(db, "tts", "TTS_NOT_CONFIGURED")
    binding = {
        "task_id": task["task_id"],
        "script_sha256": script_sha,
        "source_snapshot_ids": [row["snapshot_id"] for row in source_rows],
        "ratio": brief["aspect_ratio"],
        "general_model": general_model,
        "voice": voice,
    }
    if general_binding is not None:
        binding["general_profile_digest"] = general_binding.get("configDigest")
        binding["tts_profile_digest"] = tts_binding.get("configDigest")
    input_digest = _canonical_digest(binding)
    ad_dir = task_root / "artifacts" / "ad"
    ad_dir.mkdir(parents=True, exist_ok=True)
    plan_path = ad_dir / "adplan.json"
    timing_path = ad_dir / "adtiming.json"
    voice_path = ad_dir / "voice.mp3"
    metadata_path = ad_dir / "recommendation.json"

    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("STORYBOARD_STALE: 广告分镜绑定记录无效") from error
        if metadata.get("input_digest") != input_digest:
            raise RuntimeError("STORYBOARD_STALE: 文案、配音或素材范围已经变化")
        try:
            existing_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("STORYBOARD_PARTIAL: 广告分镜文件无效") from error
        finalized = finalize_ad_storyboard(
            db,
            task_id=task["task_id"],
            project_root=root,
            plan=existing_plan,
            input_binding=binding,
            input_digest=input_digest,
            runner=runner,
            source="standalone_model",
        )
        return _action_result(
            "recommend_storyboard:complete", "广告分镜与配音已生成",
            finalized,
        )

    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = root / ".venv" / "bin" / "python"
    node = root / "node.exe"
    if not node.is_file():
        found = shutil.which("node")
        node = Path(found) if found else node
    ts_node = root / "node_modules" / "ts-node" / "dist" / "bin.js"
    command = [
        str(python), "-X", "utf8",
        str(root / "scripts" / "ad_pipeline_runner.py"),
        "--project-root", str(root),
        "--node", str(node),
        "--ts-node", str(ts_node),
        "--script", str(script_path),
        "--ratio", str(brief["aspect_ratio"]),
        "--plan-output", str(plan_path),
        "--timing-output", str(timing_path),
        "--voice-output", str(voice_path),
        "--segments-dir", str(ad_dir / "segments"),
    ]
    if general_binding is not None:
        command.extend([
            "--catalog-db", str(Path(db.path).resolve()),
            "--task-id", task["task_id"],
            "--text-capability", text_capability,
        ])
    else:
        command.extend([
            "--general-model", general_model,
            "--voice", voice,
        ])
    _run_checked(runner, command, cwd=root, timeout_seconds=3600)
    try:
        generated_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜文件无效") from error
    finalized = finalize_ad_storyboard(
        db,
        task_id=task["task_id"],
        project_root=root,
        plan=generated_plan,
        input_binding=binding,
        input_digest=input_digest,
        runner=runner,
        source="standalone_model",
    )
    return _action_result(
        "recommend_storyboard:complete", "广告分镜与配音已生成",
        finalized,
    )

def _file_signature(path: Path) -> dict:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": _file_sha256(resolved),
    }


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(float(seconds) * 100)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def _write_ad_qa_subtitles(
    target: Path,
    segments: list[dict],
    preset: dict,
) -> Path:
    style_name = str(preset.get("id") or "AdSubtitle")
    style = preset.get("ass_style")
    if not isinstance(style, str) or not style.startswith("Style:"):
        style = (
            f"Style: {style_name},{preset.get('fontname_guess') or 'Arial'},"
            f"{int(preset.get('size') or 48)},&H00FFFFFF,&H000000FF,&H00000000,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,3,1,"
            f"{int(preset.get('alignment') or 2)},20,20,"
            f"{int(preset.get('margin_v') or 60)},1"
        )
    dialogues = []
    for segment in segments:
        text = str(segment.get("displayText", segment.get("text")) or "")
        if not text.strip():
            continue
        text = r"\N".join(line.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
                           for line in text.replace("\r", "").split("\n"))
        dialogues.append(
            "Dialogue: 0,{},{},{},,0,0,0,,{}".format(
                _ass_time(segment.get("subtitleStart", segment["start"])),
                _ass_time(segment.get("subtitleEnd", segment["end"])),
                style_name,
                text,
            )
        )
    content = "\n".join((
        "[Script Info]",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style,
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *dialogues,
        "",
    ))
    target.write_text(content, encoding="utf-8")
    return target


def _ad_qa_evidence(db, render_snapshot: dict) -> dict:
    row = db.connection.execute(
        "SELECT params_json FROM render_snapshots WHERE snapshot_id=?",
        (render_snapshot["mix_snapshot_id"],),
    ).fetchone()
    try:
        params = json.loads(row["params_json"]) if row is not None else {}
    except (TypeError, json.JSONDecodeError):
        params = {}
    dependency = params.get("dependency_snapshot")
    render_plan = dependency.get("render_plan") if isinstance(dependency, dict) else None
    # Older candidates keep the immutable input plan as a signed dependency file.
    if render_plan is None and params.get("plan_path") and isinstance(dependency, dict):
        plan_path = Path(params["plan_path"])
        signature = next((f for f in dependency.get("files", []) if f.get("path") == str(plan_path)), None)
        if signature and plan_path.is_file() and _file_signature(plan_path) == signature:
            render_plan = json.loads(plan_path.read_text("utf-8"))
    return {
        "dependency_snapshot": dependency,
        "subtitle_path": params.get("ass_path"),
        "render_plan": render_plan,
        "shot_authorization": (
            dependency.get("shot_authorization", {})
            if isinstance(dependency, dict) else {}
        ),
    }


def _confirmed_packaging(db, task_id: str, root: Path) -> tuple[dict, Path | None]:
    gate = latest_gate(db, task_id, "packaging")
    try:
        details = json.loads(gate["details_json"]) if gate is not None else {}
    except (TypeError, json.JSONDecodeError):
        details = {}
    plan_id = details.get("plan_id")
    plan = get_plan(db, plan_id) if isinstance(plan_id, str) else None
    if (
        plan is None
        or plan.get("task_id") != task_id
        or plan.get("status") != "confirmed"
    ):
        raise RuntimeError("PACKAGING_PLAN_MISSING: 最新包装门禁未绑定有效确认方案")
    bgm = None
    for item in plan["items"]:
        if item.get("slot") != "bgm" or not item.get("asset_file"):
            continue
        candidate = Path(item["asset_file"])
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise RuntimeError("PACKAGING_ASSET_MISSING: 已确认的 BGM 文件不存在")
        bgm = candidate
        break
    return plan, bgm


def _resolved_asset_paths(db, task_id: str) -> dict[str, Path]:
    result = {}
    for asset in list_eligible_assets(db, task_id):
        row = db.connection.execute(
            "SELECT mount_path FROM libraries WHERE library_id=?",
            (asset["library_id"],),
        ).fetchone()
        if row is None:
            continue
        path = (Path(row["mount_path"]) / asset["relative_path"]).resolve()
        if path.is_file():
            result[asset["asset_id"]] = path
    return result


def _ad_runtime_dependencies(root: Path, subtitle_preset: dict) -> list[dict]:
    """Bind the actual renderer, style configuration, fonts and executable paths."""
    from . import packaging_plan

    node = root / "node.exe"
    if not node.is_file():
        node_path = shutil.which("node")
        node = Path(node_path) if node_path else node
    font_name = subtitle_preset.get("font")
    if not isinstance(font_name, str) or Path(font_name).name != font_name:
        raise RuntimeError("AD_RENDER_DEPENDENCY_MISSING: 字幕预设缺少有效字体文件")
    required = [
        root / "scripts" / "render-ad.ts",
        root / "src" / "ad-subtitle-layout.js",
        root / "media_catalog" / "ad_subtitles.py",
        root / "media_catalog" / "reference_editing.py",
        root / "media_catalog" / "emphasis_packaging.py",
        root / "media_catalog" / "music_matching.py",
        root / "media_catalog" / "camera_motion.py",
        root / "scripts" / "ad-style.ts",
        root / "scripts" / "load-env.ts",
        root / "tsconfig.json",
        packaging_plan.MANIFEST_DIR / "subtitle-presets.json",
        root / "assets" / "fonts" / font_name,
        node,
        root / "node_modules" / "ts-node" / "dist" / "bin.js",
    ]
    for tool in ("ffmpeg", "ffprobe"):
        resolved = shutil.which(tool)
        if resolved is None:
            raise RuntimeError(f"AD_RENDER_DEPENDENCY_MISSING: 缺少 {tool}")
        required.append(Path(resolved))
    # render-ad copies every bundled font; additions can change libass fallback.
    fonts = root / "assets" / "fonts"
    required.extend(sorted(path for path in fonts.glob("*")
                           if path.suffix.lower() in {".otf", ".ttf", ".ttc"}))
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"AD_RENDER_DEPENDENCY_MISSING: {path}")
    return [_file_signature(path) for path in dict.fromkeys(required)]


def build_ad_packaging_preview_job(
    db, current, project_root: Path, preview_id: str
) -> dict:
    """Build a bounded ad preview from locked media and already-generated voice."""
    root = Path(project_root).resolve()
    task_id = current.identity.task_id
    task_root, task, brief, script_path, _script_sha = _task_context(
        db,
        SimpleNamespace(task_id=task_id, payload={"task_id": task_id}),
        root,
    )
    ad_dir = task_root / "artifacts" / "ad"
    plan_path = ad_dir / "adplan.json"
    timing_path = ad_dir / "adtiming.json"
    voice_path = ad_dir / "voice.mp3"
    _plan, timing = _validate_ad_outputs(
        script_path.read_text(encoding="utf-8"),
        str(brief["aspect_ratio"]),
        plan_path,
        timing_path,
        voice_path,
    )
    if not voice_path.is_file():
        raise RuntimeError(
            "AD_PREVIEW_VOICE_MISSING: 包装预览只使用已生成配音，不会发起付费 TTS"
        )
    cards = list_scene_cards(db, task_id)
    if len(cards) != len(timing["segments"]):
        raise RuntimeError("AD_SCENE_COUNT_INVALID: 锁定画面与逐句配音数量不一致")
    if any(not card.get("confirm_status") for card in cards):
        raise RuntimeError("SCENE_CONFIRMATION_REQUIRED: 广告镜头尚未全部确认")
    asset_paths = _resolved_asset_paths(db, task_id)
    duration = float(current.duration_seconds)
    render_segments = []
    for card, timed in zip(cards, timing["segments"], strict=True):
        start = float(timed["start"])
        if start >= duration:
            break
        end = min(float(timed["end"]), duration)
        segment_duration = end - start
        if segment_duration <= 0:
            continue
        asset_id = card.get("asset_id")
        if not asset_id or asset_id not in asset_paths:
            raise RuntimeError("AD_SCENE_ASSET_OFFLINE: 已锁定广告素材当前离线")
        segment = {
            "i": int(timed["i"]),
            "text": card["narrative_text"],
            "start": start,
            "end": end,
            "dur": segment_duration,
            "src": str(asset_paths[asset_id]),
            "matched": True,
            "segmentId": card.get("segment_id"),
        }
        locked_start = card.get("enter_tc")
        locked_end = card.get("exit_tc")
        if locked_start is not None or locked_end is not None:
            source_start = float(locked_start or 0.0)
            source_end = source_start + segment_duration
            if locked_end is not None:
                source_end = min(source_end, float(locked_end))
            segment["sourceStart"] = source_start
            segment["sourceEnd"] = source_end
        render_segments.append(segment)
    if not render_segments:
        raise RuntimeError("PACKAGING_PREVIEW_TIMELINE_EMPTY: 没有可渲染广告片段")

    title = str(task.get("title") or task_id)
    compiled = compile_packaging_layers(
        db,
        current.identity.plan_id,
        PackagingTimingContext(
            duration_seconds=duration,
            title=title,
            keyword=render_segments[0]["text"],
            voice_mode=f"cached:{voice_path.resolve()}",
        ),
    )
    subtitle_ids = [
        item["asset_ref"]
        for item in current.plan["items"]
        if item["slot"] == "subtitle" and not item["is_backup"]
    ]
    if len(subtitle_ids) != 1:
        raise RuntimeError("AD_SUBTITLE_PRESET_REQUIRED: 必须选择唯一主字幕预设")
    subtitle_preset = find_item("subtitle", subtitle_ids[0])
    if subtitle_preset is None:
        raise RuntimeError("AD_SUBTITLE_PRESET_MISSING: 字幕预设不存在")

    output = (
        root
        / "runtime/workbench/cache/packaging-previews"
        / task_id
        / f"{preview_id}.mp4"
    ).resolve()
    work_dir = output.parent / f"{preview_id}-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    render_plan = work_dir / "adrender.json"
    reference = apply_reference_policy(root, task_id, current.identity.plan_id, render_segments, subtitle_preset, asset_paths, db=db)
    if reference:
        render_segments = reference["segments"]
        subtitle_preset = reference["subtitlePreset"]
    else:
        render_segments = layout_ad_subtitles(render_segments, subtitle_preset, str(brief["aspect_ratio"]))
    render_payload = {
        "name": task_id,
        "ratio": brief["aspect_ratio"],
        "total": duration,
        "voice": str(voice_path.resolve()),
        "segments": render_segments,
        "subtitlePreset": subtitle_preset,
        "output": {
            "maxDuration": duration,
            "width": 540,
            "height": 960,
            "crf": 28,
        },
        **compiled.plan_fields,
    }
    if reference:
        render_payload.update({k:v for k,v in reference.items() if k != "policy_path"})
        music = reference["referenceMatch"]["policy"].get("music_match")
        bgm = render_payload.get("bgm")
        if music and bgm and str(Path(bgm["src"]).resolve()) == str(Path(music["selected"]["source"]).resolve()):
            bgm.update(start=music["selected"]["start"], fadeIn=music["fade_in"], fadeOut=music["fade_out"])
            render_payload["musicMatch"] = music
    render_plan.write_text(
        json.dumps(render_payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    node = root / "node.exe"
    if not node.is_file():
        found = shutil.which("node")
        node = Path(found) if found else node
    ts_node = root / "node_modules" / "ts-node" / "dist" / "bin.js"
    command = [
        str(node),
        str(ts_node),
        str(root / "scripts" / "render-ad.ts"),
        "--plan",
        str(render_plan),
        "--output",
        str(output),
        "--max-duration",
        str(duration),
        "--width",
        "540",
        "--height",
        "960",
        "--crf",
        "28",
    ]
    bgm = compiled.plan_fields.get("bgm")
    if isinstance(bgm, dict) and bgm.get("src"):
        command.extend(["--bgm", str(bgm["src"])])
    return {
        "command": command,
        "output_path": output,
        "plan_path": render_plan,
        "coverage": list(compiled.coverage),
        "duration_seconds": duration,
    }


def render_ad_packaging_preview(
    db, current, project_root: Path, preview_id: str, runner
) -> dict:
    job = build_ad_packaging_preview_job(
        db, current, Path(project_root), preview_id
    )
    _run_checked(
        runner,
        job["command"],
        cwd=Path(project_root).resolve(),
        timeout_seconds=1800,
        max_output_bytes=1024 * 1024,
    )
    output = Path(job["output_path"])
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("RENDER_OUTPUT_MISSING: 广告包装预览没有产生视频")
    return {
        "output_path": output,
        "duration_seconds": job["duration_seconds"],
        "coverage": job["coverage"],
    }


def render_ad_and_qa(db, job, project_root, runner):
    """Render a locked ad candidate, record immutable dependencies, then run QA."""

    from .render_mix_adapter import verify_dependency_snapshot

    root = Path(project_root).resolve()
    task_root, task, brief, script_path, _script_sha = _task_context(
        db, job, root
    )
    if latest_gate(db, task["task_id"], "picture_lock") is None:
        raise RuntimeError("PICTURE_LOCK_MISSING: 必须先锁定广告画面")
    if not project_gates(db, task["task_id"])["picture_lock"]:
        raise RuntimeError("PICTURE_LOCK_MISSING: 广告画面锁已失效，必须重新确认")
    if latest_gate(db, task["task_id"], "packaging") is None:
        raise RuntimeError("PACKAGING_PLAN_MISSING: 必须先确认包装方案")
    packaging, _legacy_bgm = _confirmed_packaging(db, task["task_id"], root)
    subtitle_items = [item for item in packaging["items"]
                      if item["slot"] == "subtitle" and not item["is_backup"]]
    if len(subtitle_items) != 1:
        raise RuntimeError("AD_SUBTITLE_PRESET_REQUIRED: 必须确认唯一主字幕预设")
    subtitle_preset = find_item("subtitle", subtitle_items[0]["asset_ref"])
    if subtitle_preset is None:
        raise RuntimeError("AD_SUBTITLE_PRESET_MISSING: 已确认字幕预设不存在")
    runtime_files = _ad_runtime_dependencies(root, subtitle_preset)

    action_input_digest = _render_input_digest(db, task["task_id"])
    candidate = latest_review_candidate(db, task["task_id"])
    retry = latest_retryable_render_snapshot(db, task["task_id"])
    ad_dir = task_root / "artifacts" / "ad"
    plan_path = ad_dir / "adplan.json"
    timing_path = ad_dir / "adtiming.json"
    voice_path = ad_dir / "voice.mp3"
    plan, timing = _validate_ad_outputs(
        script_path.read_text(encoding="utf-8"),
        str(brief["aspect_ratio"]),
        plan_path,
        timing_path,
        voice_path,
    )
    cards = list_scene_cards(db, task["task_id"])
    if len(cards) != len(timing["segments"]):
        raise RuntimeError("AD_SCENE_COUNT_INVALID: 锁定画面与逐句配音数量不一致")
    if any(not card.get("confirm_status") for card in cards):
        raise RuntimeError("SCENE_CONFIRMATION_REQUIRED: 广告镜头尚未全部确认")
    if any(not card.get("asset_id") for card in cards):
        raise RuntimeError("AD_SCENE_ASSET_REQUIRED: 每句广告文案都必须绑定真实素材")

    asset_paths = _resolved_asset_paths(db, task["task_id"])
    missing = [
        card["asset_id"] for card in cards if card["asset_id"] not in asset_paths
    ]
    if missing:
        raise RuntimeError("AD_SCENE_ASSET_OFFLINE: 已锁定广告素材当前离线")
    version = db.connection.execute(
        "SELECT COUNT(*) + 1 FROM render_snapshots WHERE task_id=? AND stage='mix'",
        (task["task_id"],),
    ).fetchone()[0]
    output_path = (
        root / "out" / task["task_id"] / f"candidate-v{version}" / "video-final.mp4"
    ).resolve()
    if output_path.exists():
        raise RuntimeError("RENDER_OUTPUT_EXISTS: 未绑定广告候选文件已存在")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = task_root / "render" / f"candidate-v{version}"
    work_dir.mkdir(parents=True, exist_ok=True)
    render_plan = work_dir / "adrender.json"
    render_segments = []
    for card, timed in zip(cards, timing["segments"], strict=True):
        segment = {
            "i": int(timed["i"]),
            "text": card["narrative_text"],
            "start": float(timed["start"]),
            "end": float(timed["end"]),
            "dur": float(timed["dur"]),
            "src": str(asset_paths[card["asset_id"]]),
            "matched": True,
            "segmentId": card.get("segment_id"),
        }
        locked_start = card.get("enter_tc")
        locked_end = card.get("exit_tc")
        if locked_start is not None or locked_end is not None:
            source_start = float(locked_start or 0)
            segment["sourceStart"] = source_start
            segment["sourceEnd"] = (
                float(locked_end) if locked_end is not None
                else source_start + float(timed["dur"])
            )
        render_segments.append(segment)
    compiled = compile_packaging_layers(
        db,
        packaging["plan_id"],
        PackagingTimingContext(
            duration_seconds=float(timing["total"]),
            title=str(task.get("title") or task["task_id"]),
            keyword=str(render_segments[0]["text"]),
            voice_mode=f"cached:{voice_path.resolve()}",
        ),
    )
    authorization_attestation = get_material_authorization(db, task["task_id"])
    current_shot_authorization = shot_authorization(db, task["task_id"])
    for previous, has_qa in ((candidate, True), (retry, False)):
        if previous is None or previous.get("producer") != "assemble-ad-v1":
            continue
        dependencies = previous.get("dependency_snapshot") or {}
        if dependencies.get("action_input_digest") != action_input_digest:
            continue
        if dependencies.get("packaging") != packaging:
            raise RuntimeError("PACKAGING_PLAN_CHANGED: 当前包装与已渲染候选不一致")
        if dependencies.get("packaging_compiled") != compiled.snapshot:
            raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 包装素材或授权已变化")
        if (
            dependencies.get("shot_authorization") != current_shot_authorization
            or dependencies.get("material_authorization_attestation")
            != authorization_attestation
        ):
            continue
        if dependencies.get("runtime_files") != runtime_files:
            raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 广告渲染运行依赖已变化")
        if not verify_dependency_snapshot(dependencies):
            raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 广告依赖在同输入重试前变化")
        if file_content_signature(previous["output_path"]) != previous["output_signature"]:
            raise RuntimeError("ACTION_OUTPUT_CHANGED: 广告候选文件与快照不一致")
        if has_qa:
            return _action_result("render_and_qa:complete", "广告精剪成片与自动质检完成", {
                "status": "awaiting_human_review", "output_path": previous["output_path"],
                "snapshots": previous, "reused": True,
            })
        return _finish_ad_render_qa(db, task["task_id"], previous, reused=True)
    reference = apply_reference_policy(root, task["task_id"], packaging["plan_id"], render_segments, subtitle_preset, asset_paths, db=db)
    if reference:
        render_segments = reference["segments"]
        subtitle_preset = reference["subtitlePreset"]
    else:
        render_segments = layout_ad_subtitles(render_segments, subtitle_preset, str(brief["aspect_ratio"]))
    render_payload = {
        "name": task["task_id"],
        "ratio": brief["aspect_ratio"],
        "total": float(timing["total"]),
        "voice": str(voice_path.resolve()),
        "segments": render_segments,
        "subtitlePreset": subtitle_preset,
        **compiled.plan_fields,
    }
    if reference:
        render_payload.update({k:v for k,v in reference.items() if k != "policy_path"})
        music = reference["referenceMatch"]["policy"].get("music_match")
        bgm = render_payload.get("bgm")
        if music and bgm and str(Path(bgm["src"]).resolve()) == str(Path(music["selected"]["source"]).resolve()):
            bgm.update(start=music["selected"]["start"], fadeIn=music["fade_in"], fadeOut=music["fade_out"])
            render_payload["musicMatch"] = music
    render_plan.write_text(
        json.dumps(render_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    subtitle_evidence_path = _write_ad_qa_subtitles(
        work_dir / "qa-subtitles.ass", render_payload.get("subtitleEvents",render_segments), subtitle_preset
    )
    dependency_paths = [
        script_path, plan_path, timing_path, voice_path, render_plan,
        subtitle_evidence_path,
        *asset_paths.values(),
    ]
    if reference:
        dependency_paths.append(Path(reference["policy_path"]))
        dependency_paths.extend(Path(row["src"]) for row in reference.get("emphasisOverlays",[]))
    dependency_paths.extend(
        Path(item["file"])
        for item in compiled.snapshot["items"]
        if item.get("file")
    )
    files = [_file_signature(path) for path in dict.fromkeys(dependency_paths)] + runtime_files
    dependency_snapshot = {
        "route": "ad_mix",
        "task_id": task["task_id"],
        "action_input_digest": action_input_digest,
        "runtime_files": runtime_files,
        "files": files,
        "render_plan": render_payload,
        "scene_cards": [
            {
                "card_id": card["card_id"],
                "asset_id": card["asset_id"],
                "text": card["narrative_text"],
                "start": card["narrative_start"],
                "end": card["narrative_end"],
                "sourceStart": card["enter_tc"],
                "sourceEnd": card["exit_tc"],
                "segmentId": card.get("segment_id"),
            }
            for card in cards
        ],
        "packaging": packaging,
        "packaging_compiled": compiled.snapshot,
        "shot_authorization": current_shot_authorization,
        "material_authorization_attestation": authorization_attestation,
        "subtitle_evidence": _file_signature(subtitle_evidence_path),
    }
    dependency_digest = _canonical_digest(dependency_snapshot)
    if not verify_dependency_snapshot(dependency_snapshot):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 渲染前广告依赖发生变化")

    node = root / "node.exe"
    if not node.is_file():
        found = shutil.which("node")
        node = Path(found) if found else node
    ts_node = root / "node_modules" / "ts-node" / "dist" / "bin.js"
    command = [
        str(node), str(ts_node), str(root / "scripts" / "render-ad.ts"),
        "--plan", str(render_plan),
        "--output", str(output_path),
    ]
    bgm = compiled.plan_fields.get("bgm")
    if isinstance(bgm, dict) and bgm.get("src"):
        command.extend(["--bgm", str(bgm["src"])])
    _run_checked(
        runner, command, cwd=root, timeout_seconds=7200,
        max_output_bytes=1024 * 1024,
    )
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("RENDER_OUTPUT_MISSING: 广告渲染没有产生候选成片")
    if not verify_dependency_snapshot(dependency_snapshot):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 渲染期间广告依赖发生变化")

    render_job = {
        "output_path": str(output_path),
        "dependency_digest": dependency_digest,
        "dependency_snapshot": dependency_snapshot,
        "plan_path": str(render_plan),
        "ass_path": str(subtitle_evidence_path),
        "packaging": packaging,
    }
    render_snapshot = record_render_dependency_snapshot(
        db, task["task_id"], render_job, producer="assemble-ad-v1"
    )
    return _finish_ad_render_qa(db, task["task_id"], render_snapshot)


def _finish_ad_render_qa(db, task_id: str, render_snapshot: dict, *, reused=False):
    output_path = Path(render_snapshot["output_path"])
    dependency_digest = render_snapshot["dependency_digest"]
    complete_stage(
        db, task_id, "fine_render", input_digest=dependency_digest,
        details={"output_path": str(output_path), **render_snapshot},
    )
    qa_result = run_qa(
        db,
        task_id,
        str(output_path),
        evidence=_ad_qa_evidence(db, render_snapshot),
    )
    complete_stage(
        db, task_id, "auto_qa",
        input_digest=_canonical_digest({
            "dependency_digest": dependency_digest,
            "qa_report_id": qa_result["report_id"],
        }),
        details={
            "qa_report_id": qa_result["report_id"],
            "status": qa_result["status"],
            "blocking_count": qa_result["blocking_count"],
        },
    )
    if qa_result["status"] != "passed" or not qa_evidence_complete(qa_result):
        candidate = record_candidate_qa(
            db,
            task_id,
            render_snapshot,
            qa_result,
            eligible_for_review=False,
        )
        return _action_result(
            "render_and_qa:qa_blocked", "广告成片质检发现阻断项",
            {
                "status": "qa_blocked", "output_path": str(output_path),
                "qa": qa_result, "snapshots": render_snapshot,
                "candidate": candidate, "reused": reused,
            },
        )
    snapshots = record_qa_review_snapshot(
        db, task_id, render_snapshot, qa_result,
        producer="assemble-ad-v1",
    )
    candidate = record_candidate_qa(
        db,
        task_id,
        render_snapshot,
        qa_result,
        qa_snapshot_id=snapshots["qa_snapshot_id"],
    )
    return _action_result(
        "render_and_qa:complete", "广告精剪成片与自动质检完成",
        {
            "status": "awaiting_human_review",
            "output_path": str(output_path),
            "qa": qa_result,
            "snapshots": {**snapshots, **candidate},
            "candidate": candidate,
            "reused": reused,
        },
    )


def build_ad_publish_package(db, job, project_root, runner):
    """Build the fixed release package from the approved ad review candidate."""

    from .render_mix_adapter import verify_dependency_snapshot

    _ = runner
    root = Path(project_root).resolve()
    _task_root, task, _brief, _script, _digest = _task_context(db, job, root)
    candidate = latest_review_candidate(db, task["task_id"])
    if candidate is None or candidate.get("producer") != "assemble-ad-v1":
        raise RuntimeError("REVIEW_CANDIDATE_MISSING: 没有可信的广告审片候选")
    expected_candidate_id = str(
        job.payload.get("candidate_id") or candidate.get("candidate_id") or ""
    )
    if candidate.get("candidate_id") != expected_candidate_id:
        raise RuntimeError("PUBLISH_CANDIDATE_STALE: 发布任务绑定的候选已不是当前版本")
    dependencies = candidate.get("dependency_snapshot") or {}
    if (
        not project_gates(db, task["task_id"])["picture_lock"]
        or dependencies.get("action_input_digest") != _render_input_digest(db, task["task_id"])
    ):
        raise RuntimeError("AD_RENDER_INPUT_CHANGED: 广告画面或包装已变化，必须重新渲染并审片")
    packaging, _bgm = _confirmed_packaging(db, task["task_id"], root)
    if dependencies.get("packaging") != packaging:
        raise RuntimeError("AD_RENDER_INPUT_CHANGED: 已确认包装与广告候选不一致，必须重新渲染并审片")
    if not verify_dependency_snapshot(dependencies):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 审片后广告依赖发生变化")
    row = db.connection.execute(
        "SELECT report_json FROM qa_reports WHERE report_id=? AND task_id=?",
        (candidate["qa_report_id"], task["task_id"]),
    ).fetchone()
    if row is None:
        raise RuntimeError("QA_REPORT_MISSING: 广告候选缺少 QA 报告")
    report = json.loads(row["report_json"])
    result = create_publish_package(
        db,
        task["task_id"],
        str(job.payload.get("platform") or "默认"),
        job.payload.get("title"),
        job.payload.get("description"),
        list(job.payload.get("tags") or []),
        cover_choice=str(job.payload.get("cover_choice") or "candidate-1"),
        external_cover_path=job.payload.get("external_cover_path"),
        delivery_type=str(job.payload.get("delivery_type") or "standard"),
        trusted_roots=[root, Path(candidate["output_path"]).resolve().parent],
        video_path=candidate["output_path"],
        qa_report={"report_id": report["report_id"]},
        candidate_id=expected_candidate_id,
    )
    complete_stage(
        db, task["task_id"], "publish_package",
        input_digest=_canonical_digest({
            "qa_snapshot_id": candidate["qa_snapshot_id"],
            "record_id": result["record_id"],
            "publish": {
                "platform": str(job.payload.get("platform") or "默认"),
                "title": job.payload.get("title"),
                "description": job.payload.get("description"),
                "tags": list(job.payload.get("tags") or []),
                "cover_choice": str(job.payload.get("cover_choice") or "candidate-1"),
                "external_cover_path": job.payload.get("external_cover_path"),
                "delivery_type": str(job.payload.get("delivery_type") or "standard"),
            },
        }),
        details={"record_id": result["record_id"]},
    )
    return _action_result(
        "build_publish_package:complete", "广告发布包生成完成",
        {**result, "reused": result.get("reused", False)},
    )
