"""Task-scoped information-flow ad planning and rendering actions."""

from __future__ import annotations

import hashlib
from .runtime_artifacts import compiled_signature, node_script_command
import json
from pathlib import Path
from .path_utils import filesystem_path
import re
import shutil
import uuid
from types import SimpleNamespace

from .agent_artifacts import get_agent_artifact
from .agent_context import current_agent_input_digest
from .ad_artifact_binding import ad_artifact_hashes_match, capture_ad_artifact_hashes
from .ad_matching import load_observed_ad_windows, match_ad_shots, match_task_ad_shots
from .candidate_versions import record_candidate_qa
from .material_authorization import get_material_authorization, shot_authorization
from .packaging_plan import find_item, get_plan
from .packaging_layers import PackagingTimingContext, compile_packaging_layers
from .model_profiles import get_task_profile_binding_for_usage
from .provenance import latest_gate
from .publish import build_publish_package as create_publish_package
from .qa import run_qa
from .qa_evidence import qa_evidence_complete
from .render_output_profile import packaging_preview_output
from .render_paths import reserve_candidate_paths
from .render_recipe import ad_plan_from_recipe, compile_ad_recipe, preview_receipt
from .rough_cut import resolved_ad_source_windows, validate_ad_source_windows
from .scene_cards import add_scene_card, list_scene_cards
from .script_segments import create_script_segment
from .timeline_contract import seconds_to_us
from .snapshots import (
    file_content_signature,
    latest_review_candidate,
    latest_retryable_render_snapshot,
    latest_qa_only_rework,
    record_qa_review_snapshot,
    record_render_dependency_snapshot,
)
from .task_sources import list_eligible_assets, require_bound_source_readiness, require_source_readiness
from .tasks import add_asset, get_task
from .workflow import complete_stage, project_gates
from .visual_uniqueness import validate_task_scene_uniqueness
from .workbench_actions import (
    _action_result,
    _canonical_digest,
    _contained,
    _file_sha256,
    _run_checked,
    _render_input_digest,
    _qa_rework_input_matches,
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
    if not project_gates(db, task_id)["brief_locked"]:
        raise RuntimeError("BRIEF_GATE_MISSING: 当前广告文案尚未确认")
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
    planned_text = "".join(str(item.get("text") or "") for item in plan_segments)
    if planned_text != script:
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
        highlight = segment.get("highlight")
        if highlight is not None and (
            not isinstance(highlight, str)
            or (highlight and (highlight not in segment["text"] or len(highlight) > 8))
        ):
            raise RuntimeError("AD_HIGHLIGHT_INVALID: 重点词必须取自本句原文")
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
    metadata_path = plan_path.with_name("recommendation.json")
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("AD_OUTPUT_BINDING_CHANGED: 广告产物绑定记录无效") from error
        if not isinstance(metadata, dict) or not ad_artifact_hashes_match(plan_path.parent, metadata):
            raise RuntimeError("AD_OUTPUT_BINDING_CHANGED: 广告规划、时间表或配音文件已变化")
    return plan, timing


def _validate_ad_plan(script: str, ratio: str, plan: dict) -> None:
    segments = plan.get("segments") if isinstance(plan, dict) else None
    if plan.get("ratio") != ratio or not isinstance(segments, list) or not segments:
        raise RuntimeError("AD_OUTPUT_INVALID: 广告分镜结构无效")
    planned_text = "".join(str(item.get("text") or "") for item in segments)
    if planned_text != script:
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
        highlight = segment.get("highlight")
        if highlight is not None and (
            not isinstance(highlight, str)
            or (highlight and (highlight not in segment["text"] or len(highlight) > 8))
        ):
            raise RuntimeError("AD_HIGHLIGHT_INVALID: 重点词必须取自本句原文")


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


def _persist_ad_cards(db, task_id: str, plan: dict, timing: dict, script_sha: str) -> list[dict]:
    assets = list_eligible_assets(db, task_id)
    candidates = load_observed_ad_windows(db, assets)
    matches = match_task_ad_shots(
        db, task_id, plan["segments"], timing["segments"], candidates, plan["ratio"]
    )
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
                None if asset is None else asset["source_start"],
                None if asset is None else asset["source_end"],
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
                card["enter_tc"],
                card["exit_tc"],
            )
            for card in existing
        ]
        if actual != expected:
            raise RuntimeError(
                "STORYBOARD_EXISTS: 任务已有不同广告分镜，不能静默覆盖"
            )
        return existing
    with db.transaction(immediate=True):
        for index, (segment, timed, asset) in enumerate(
            zip(plan["segments"], timing["segments"], matches, strict=True)
        ):
            start = float(timed["start"])
            end = float(timed["end"])
            script_segment = create_script_segment(
                db, task_id, script_sha, index, segment["text"],
                seconds_to_us(start), seconds_to_us(end),
            )
            shot_id = "shot-" + hashlib.sha256(
                f"{task_id}\0{script_sha}\0{index}".encode("utf-8")
            ).hexdigest()[:24]
            add_scene_card(
                db,
                task_id,
                index,
                shot_id=shot_id,
                script_segment_id=script_segment["segment_id"],
                narrative_text=segment["text"],
                narrative_start=start,
                narrative_end=end,
                asset_id=None if asset is None else asset["asset_id"],
                segment_id=None if asset is None else asset["segment_id"],
                mix_form="broll全屏",
                enter_tc=None if asset is None else asset["source_start"],
                exit_tc=None if asset is None else asset["source_end"],
                audio_policy_draft="replace",
                source_type="缺口替代" if asset is None or asset.get("match_evidence", {}).get("tier") == "park_secondary" else "系统推荐",
                system_suggestion=(
                    f"画面需求：{segment['query']}；"
                    + ("当前任务范围没有可分配的内容、方向和时长均合格的已观察素材"
                       if asset is None else
                       f"画面依据：{asset['title']} {asset['description']}；方向和时长已核对；"
                        + asset.get("match_evidence", {}).get("reason", "精准内容匹配"))
                ),
            )
    from .creative_assistance import register_reshoot_need
    with db.transaction(immediate=True):
        for card, segment in zip(list_scene_cards(db, task_id), plan['segments'], strict=True):
            if card['asset_id'] is None:
                register_reshoot_need(db, task_id, script_sha, card['script_segment_id'],
                    'card:' + card['card_id'], segment['query'], segment['type'],
                    round(card['narrative_start'] * 1e6), round(card['narrative_end'] * 1e6), card['card_id'])
    return list_scene_cards(db, task_id)


def _complete_storyboard_generation(db, task_id, *, input_digest, details):
    """A replay can preserve a real same-content approval, never invent one."""
    from .script_segments import storyboard_digest
    row = db.connection.execute(
        "SELECT * FROM task_stage_states WHERE task_id=? AND stage_key='ai_storyboard'",
        (task_id,)).fetchone()
    if row is not None and row['status'] == 'completed':
        saved = json.loads(row['detail_json'] or '{}')
        accepted = (saved.get('details') or {}).get('storyboard_accepted_digest')
        if (accepted and saved.get('input_digest') in (input_digest, accepted)
                and accepted == storyboard_digest(db, task_id)):
            return dict(row)
    return complete_stage(db, task_id, 'ai_storyboard', input_digest=input_digest, details=details)


def finalize_registered_ad_storyboard(db, *, task_id, project_root, artifact):
    """Bind an accepted Agent plan to current registered audio without paid work.

    Return None only for tasks that have never activated managed audio. Existing
    managed ownership must verify; corrupt or stale audio never falls back to TTS.
    """
    from .voice_ownership import read_voice_ownership
    from .voice_bundle_service import _service, read_registered_voice, preview_voice_timing
    from .voice_task_context import resolve_voice_task_context
    from .registered_ad_storyboard import _JoinedTransaction

    ownership = read_voice_ownership(db, task_id)
    if ownership.branch == 'legacy':
        return None
    if (not isinstance(artifact, dict) or artifact.get('task_id') != task_id
            or artifact.get('artifact_type') != 'ad_storyboard'
            or artifact.get('status') != 'accepted'
            or get_agent_artifact(db, artifact.get('artifact_id')) != artifact):
        raise RuntimeError('AGENT_ARTIFACT_REQUIRED: 必须使用本任务已接受的广告规划')
    root = Path(project_root).resolve()
    require_source_readiness(db, task_id)
    digest = current_agent_input_digest(db, task_id, root)
    if artifact.get('input_digest') != digest:
        raise RuntimeError('AGENT_INPUT_STALE: 广告规划已不匹配当前输入')
    task_root, task, brief, script_path, script_sha = _task_context(
        db, SimpleNamespace(task_id=task_id, payload={'task_id': task_id}), root)
    plan = artifact['content']['plan']
    _validate_ad_plan(script_path.read_text(encoding='utf-8'), brief['aspect_ratio'], plan)
    context = resolve_voice_task_context(db, root, task_id)
    head = _service(db, root, task_id).current(task_id)
    checked = read_registered_voice(db, root, task_id, head['audio'], head['config'], context.context_digest)
    projection = preview_voice_timing(db, root, task_id, head['audio'], head['config'], context.context_digest)
    allowed_gaps = {'SCENE_ASSET_REQUIRED', 'SOURCE_WINDOW_INVALID', 'SOURCE_WINDOW_TOO_SHORT', 'SHOT_COVERAGE_MISSING'}
    if any(row['code'] not in allowed_gaps for row in projection['gaps']):
        raise RuntimeError('VOICE_TIMING_REVIEW_REQUIRED: 当前声音与镜头时间线尚未完整对应')
    timing = projection['timing']
    if [r['text'] for r in plan['segments']] != [r['text'] for r in timing['segments']]:
        raise RuntimeError('VOICE_PLAN_TIMING_MISMATCH: 规划分句必须与已登记配音实测分句一致')
    cards = list_scene_cards(db, task_id)
    by_id = {r['card_id']: r for r in cards}
    for update in projection.get('scene_updates', []):
        current = by_id.get(update['card_id'])
        if current is None or any(current.get(k) != update[k] for k in
                ('narrative_start', 'narrative_end', 'enter_tc', 'exit_tc') if k in update):
            raise RuntimeError('VOICE_TIMING_REVIEW_REQUIRED: 不可在规划提交时静默修改现有镜头')
    voice_binding = dict(revision=head['revision'], context_digest=context.context_digest,
        bundle_id=checked['bundle_id'], audio_sha256=head['audio']['sha256'],
        timeline_digest=_canonical_digest(checked['timeline']))
    ad_dir = task_root / 'artifacts' / 'ad'
    plan_path = ad_dir / 'adplan.json'
    metadata_path = ad_dir / 'recommendation.json'
    reused = False
    written = []
    def write_bound(path, value):
        before = path.read_bytes() if path.exists() else None
        _write_json_atomically(path, value)
        written.append((path, before, path.read_bytes()))
    with db.transaction(immediate=True):
        try:
            require_source_readiness(db, task_id)
            # Recheck authoritative identity before creating the first scenes.
            if (_service(db, root, task_id).current(task_id) != head
                    or list_scene_cards(db, task_id) != cards
                    or resolve_voice_task_context(db, root, task_id).context_digest != context.context_digest
                    or current_agent_input_digest(db, task_id, root) != digest):
                raise RuntimeError('AGENT_INPUT_STALE: 配音或规划输入已变化')
            if plan_path.exists() and json.loads(plan_path.read_text(encoding='utf-8')) != plan:
                raise RuntimeError('STORYBOARD_EXISTS: 已有不同广告规划，不能覆盖')
            writer = _JoinedTransaction(db)
            cards = cards or _persist_ad_cards(writer, task_id, plan, timing, script_sha)
            metadata = dict(task_id=task_id, script_sha256=script_sha, input_digest=digest,
                artifact_id=artifact['artifact_id'], source='agent_artifact',
                voice_source='registered', registered_voice=voice_binding,
                scene_card_count=len(cards), missing_count=sum(not r.get('asset_id') for r in cards))
            if metadata_path.exists():
                old = json.loads(metadata_path.read_text(encoding='utf-8'))
                if any(old.get(k) != v for k, v in metadata.items()):
                    raise RuntimeError('STORYBOARD_STALE: 原规划绑定与当前声音不同')
                if old.get('artifact_sha256') != {'adplan.json': _file_signature(plan_path)['sha256']}:
                    raise RuntimeError('STORYBOARD_STALE: 原规划文件已变化')
                reused = True
            else:
                ad_dir.mkdir(parents=True, exist_ok=True)
                if not plan_path.exists(): write_bound(plan_path, plan)
                metadata['artifact_sha256'] = {'adplan.json': _file_signature(plan_path)['sha256']}
                write_bound(metadata_path, metadata)
            _complete_storyboard_generation(writer, task_id, input_digest=digest,
                details=dict(source='agent_artifact', artifact_id=artifact['artifact_id'],
                             voice_source='registered',
                             scene_card_count=len(cards), missing_count=metadata['missing_count']))
        except BaseException:
            for path, before, after in reversed(written):
                if path.is_file() and path.read_bytes() == after:
                    if before is None: path.unlink()
                    else: path.write_bytes(before)
            raise
    return dict(reused=reused, source='agent_artifact', voice_source='registered',
        artifact_id=artifact['artifact_id'], voice_revision=head['revision'],
        scene_card_count=len(cards), missing_count=metadata['missing_count'],
        plan_path=str(plan_path), timing_path=str(checked['path'].parent / 'timeline.json'),
        voice_path=str(checked['path']), provider_calls=0)

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
        if not cards:
            # A COMMIT failure can roll back SQLite after the atomic metadata
            # file was written. Recover only signed generated output; never
            # mistake a partial/edited nonempty storyboard for this case.
            try:
                generated = json.loads((ad_dir / 'generated-output.json').read_text(encoding='utf-8'))
                if (generated.get('input_digest') != input_digest
                        or generated.get('source') != source
                        or generated.get('artifact_sha256') != capture_ad_artifact_hashes(ad_dir)):
                    raise ValueError('generated binding changed')
            except (OSError, ValueError, TypeError) as error:
                raise RuntimeError('STORYBOARD_STALE: 分镜落地记录缺失且生成缓存无法验证') from error
            from .registered_ad_storyboard import _JoinedTransaction
            with db.transaction(immediate=True):
                if list_scene_cards(db, task_id):
                    raise RuntimeError('STORYBOARD_CHANGED: 恢复期间分镜已变化，请刷新重试')
                writer = _JoinedTransaction(db)
                cards = _persist_ad_cards(writer, task_id, plan, timing, script_sha)
                _complete_storyboard_generation(writer, task_id, input_digest=input_digest,
                    details={'scene_card_count': len(cards),
                             'missing_count': sum(card['asset_id'] is None for card in cards),
                             'source': source})
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
        _complete_storyboard_generation(db, task_id,
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
        # Without a finalized binding, existing files may be leftovers from a
        # different voice or an interrupted attempt. The TTS runner verifies
        # each segment's cache signature before deciding whether to call its provider.
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
    # Paid output is independently recoverable before any local database writes.
    _write_json_atomically(ad_dir / 'generated-output.json', dict(
        input_digest=input_digest, source=source,
        artifact_sha256=capture_ad_artifact_hashes(ad_dir), status='generated'))
    from .registered_ad_storyboard import _JoinedTransaction
    with db.transaction(immediate=True):
        writer = _JoinedTransaction(db)
        before = metadata_path.read_bytes() if metadata_path.exists() else None
        written = None
        try:
            cards = _persist_ad_cards(writer, task_id, plan, timing, script_sha)
            metadata = {
                **input_binding, 'input_digest': input_digest, 'source': source,
                'artifact_sha256': capture_ad_artifact_hashes(ad_dir),
                'scene_card_count': len(cards),
                'missing_count': sum(card['asset_id'] is None for card in cards),
            }
            _write_json_atomically(metadata_path, metadata)
            written = metadata_path.read_bytes()
            _complete_storyboard_generation(writer, task_id, input_digest=input_digest,
                details={'scene_card_count': len(cards), 'missing_count': metadata['missing_count'], 'source': source})
        except BaseException:
            # Revert the bound completion record while the writer lock is held.
            if written is not None and metadata_path.is_file() and metadata_path.read_bytes() == written:
                if before is None: metadata_path.unlink()
                else: metadata_path.write_bytes(before)
            raise
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
    require_source_readiness(db, task["task_id"])
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
    from .voice_ownership import read_voice_ownership
    if read_voice_ownership(db, task['task_id']).branch != 'legacy':
        from .registered_ad_storyboard import recommend_registered_storyboard
        return recommend_registered_storyboard(db, job, root, runner)
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

    generated_path = ad_dir / 'generated-output.json'
    if not metadata_path.exists() and generated_path.exists():
        try:
            generated = json.loads(generated_path.read_text(encoding='utf-8'))
            if (generated.get('input_digest') != input_digest
                    or generated.get('source') != 'standalone_model'
                    or generated.get('artifact_sha256') != capture_ad_artifact_hashes(ad_dir)):
                raise ValueError('generated inputs changed')
            existing_plan = json.loads(plan_path.read_text(encoding='utf-8'))
        except (ValueError, OSError, TypeError) as error:
            raise RuntimeError('STORYBOARD_STALE: 已生成规划、声音或输入已变化，未再次调用模型') from error
        finalized = finalize_ad_storyboard(db, task_id=task['task_id'], project_root=root,
            plan=existing_plan, input_binding=binding, input_digest=input_digest,
            runner=runner, source='standalone_model')
        finalized['reused'] = True
        return _action_result('recommend_storyboard:complete', '已恢复分镜，复用已生成规划与配音', finalized)

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
    compiled = compiled_signature(resolved)
    if compiled is not None:
        return {"path": str(resolved), "size_bytes": compiled["size_bytes"], "sha256": compiled["sha256"]}
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
        text = str(segment.get("displayText", segment.get("text")) or "").replace("\r", "").replace("\n", r"\N")
        if not text:
            continue
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
    from .qa_authorization import reviewed_qa_dependency
    dependency = reviewed_qa_dependency(
        params.get("dependency_snapshot"), render_snapshot.get("qa_authorization"),
    )
    return {
        "dependency_snapshot": dependency,
        "subtitle_path": params.get("ass_path"),
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
        if (item.get("slot") != "bgm" or not item.get("enabled", 1)
                or not item.get("asset_file")):
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


def _ad_runtime_dependencies(root: Path, subtitle_preset: dict, *, fancy_enabled: bool = False) -> list[dict]:
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
        root / "scripts" / "ad-preview-window.ts",
        root / "src" / "mix-sfx.ts",
        root / "src" / "subtitle-ass.ts",
        root / "src" / "subtitle-alignment.ts",
        root / "src" / "audio-mix-policy.ts",
        root / "src" / "audio-mix-policy-data.json",
        root / "scripts" / "render-full-audio.ts",
        Path(__file__).with_name("audio_mix_policy.py").resolve(),
        root / "src" / "types.ts",
        root / "scripts" / "render-recipe.ts",
        root / "scripts" / "media-display-geometry.ts",
        root / "scripts" / "ad-style.ts",
        root / "scripts" / "load-env.ts",
        Path(__file__).resolve(),
        Path(__file__).with_name("render_recipe.py").resolve(),
        Path(__file__).with_name("reference_editing.py").resolve(),
        Path(__file__).with_name("ad_subtitles.py").resolve(),
        Path(__file__).with_name("emphasis_packaging.py").resolve(),
        root / "src" / "ad-subtitle-layout.js",
        Path(__file__).with_name("rough_cut.py").resolve(),
        Path(__file__).with_name("timeline_contract.py").resolve(),
        Path(__file__).with_name("packaging_layers.py").resolve(),
        Path(__file__).with_name("packaging_layout.py").resolve(),
        root / "tsconfig.json",
        packaging_plan.MANIFEST_DIR / "subtitle-presets.json",
        root / "assets" / "fonts" / font_name,
        node,
        # Match the real launcher: compiled distributions run their bundled
        # loader directly; only source checkouts need the TypeScript runner.
        *(Path(part) for part in node_script_command(node, root, "scripts/render-ad.ts")[1:]),
    ]
    if fancy_enabled:
        required.extend((root / "scripts" / "gen-fancy-text.py",
                         root / "assets" / "manifests" / "fancy-text-presets.json",
                         root / ".venv" / "Scripts" / "python.exe"))
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


def _attach_ad_highlights(plan: dict, segments: list[dict], enabled: bool) -> None:
    """Use only original-script substrings; old ad plans fall back to their query."""
    if not enabled:
        return
    planned_segments = plan.get("segments", [])
    for index, segment in enumerate(segments):
        speech_index = (segment.get("speech") or {}).get("index", index)
        if not 0 <= speech_index < len(planned_segments):
            continue
        planned = planned_segments[speech_index]
        text = segment["text"]
        candidates = (planned.get("highlight"), planned.get("query"))
        term = next((value for value in candidates
                     if isinstance(value, str) and 2 <= len(value) <= 8
                     and value in text), None)
        if term:
            segment["highlightTerms"] = [term]


def _compile_ad_render_segments(
    cards: list[dict], timing_segments: list[dict],
    asset_paths: dict[str, Path], duration_seconds: float,
    *, resolved_sources: dict[str, dict] | None = None,
) -> list[dict]:
    """Project locked ad shots onto either the full or a bounded output timeline."""
    from .timeline_contract import TimeWindow, validate_coverage

    legacy = all(card.get("narrative_start") is None and card.get("narrative_end") is None for card in cards)
    if legacy and len(cards) != len(timing_segments):
        raise RuntimeError("AD_SCENE_COUNT_INVALID: 旧镜头缺少时间轴，无法对应逐句配音")
    pairs = []
    for index, card in enumerate(cards):
        if legacy:
            timed = timing_segments[index]
            start, end = timed["start"], timed["end"]
        else:
            if card.get("narrative_start") is None or card.get("narrative_end") is None:
                raise RuntimeError("AD_SHOT_TIMELINE_MISSING: 广告镜头缺少完整时间轴")
            start, end = card["narrative_start"], card["narrative_end"]
            matches = [item for item in timing_segments
                       if seconds_to_us(item["start"]) <= seconds_to_us(start)
                       and seconds_to_us(end) <= seconds_to_us(item["end"])]
            if len(matches) != 1:
                raise RuntimeError("AD_SHOT_SPEECH_MISMATCH: 广告镜头必须位于对应文案段内")
            timed = matches[0]
        if timed.get("text") is not None and card["narrative_text"] != timed["text"]:
            raise RuntimeError("AD_SHOT_SPEECH_MISMATCH: 镜头文案与配音正文不一致")
        pairs.append((card, timed, start, end))
    duration_us = seconds_to_us(duration_seconds)
    if duration_us <= 0:
        raise RuntimeError("AD_PREVIEW_TIMELINE_EMPTY: 没有可渲染广告片段")
    segments: list[dict] = []
    windows: list[TimeWindow] = []
    for index, (card, timed, shot_start, shot_end) in enumerate(pairs):
        start_us = seconds_to_us(shot_start)
        if start_us >= duration_us:
            break
        end_us = min(seconds_to_us(shot_end), duration_us)
        if end_us <= start_us:
            raise RuntimeError("INVALID_TIME_WINDOW: 广告镜头结束时间必须晚于开始时间")
        window = TimeWindow(start_us, end_us)
        windows.append(window)
        asset_id = card.get("asset_id")
        if not asset_id or asset_id not in asset_paths:
            raise RuntimeError("AD_SCENE_ASSET_OFFLINE: 已锁定广告素材当前离线")
        source = Path(asset_paths[asset_id])
        segment = {
            "i": index,
            "cardId": card.get("card_id"),
            "assetId": asset_id,
            "text": card["narrative_text"],
            "start": start_us / 1_000_000,
            "end": end_us / 1_000_000,
            "dur": window.duration_us / 1_000_000,
            "src": str(source),
            "matched": True,
            "segmentId": card.get("segment_id"),
        }
        if not legacy:
            segment["speech"] = {"index": int(timed["i"]), "text": card["narrative_text"],
                                 "start_us": seconds_to_us(timed["start"]),
                                 "end_us": min(seconds_to_us(timed["end"]), duration_us)}
        locked_start, locked_end = card.get("enter_tc"), card.get("exit_tc")
        resolved = (resolved_sources or {}).get(card.get("card_id"))
        is_image = (
            resolved["media_kind"] == "image" if resolved is not None
            else source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )
        segment["mediaKind"] = "image" if is_image else "video"
        if is_image:
            if locked_start not in (None, 0) or locked_end not in (None, 0):
                raise RuntimeError("IMAGE_HAS_SOURCE_WINDOW: 图片不能设置视频源入出点")
        elif resolved is not None and resolved["media_kind"] == "video":
            source_start_us = seconds_to_us(resolved["source_start"])
            source_end_us = seconds_to_us(resolved["source_end"])
            if source_end_us - source_start_us < window.duration_us:
                raise RuntimeError("SOURCE_WINDOW_TOO_SHORT: 视频源区间不足以覆盖广告镜头")
            if source_start_us or locked_start not in (None, 0) or locked_end not in (None, 0):
                segment["sourceStart"] = source_start_us / 1_000_000
                segment["sourceEnd"] = (
                    source_end_us if end_us == seconds_to_us(shot_end)
                    else min(source_end_us, source_start_us + window.duration_us)
                ) / 1_000_000
        elif locked_start in (None, 0) and locked_end in (None, 0):
            # Older picture locks use 0/0 for the default full-video source.
            # The renderer checks real source duration including transition handles.
            pass
        elif locked_start is not None or locked_end is not None:
            if locked_start is None or locked_end is None:
                raise RuntimeError("SOURCE_WINDOW_REQUIRED: 视频源入出点必须同时提供")
            source_start_us = seconds_to_us(locked_start)
            source_end_us = seconds_to_us(locked_end)
            if source_end_us - source_start_us < window.duration_us:
                raise RuntimeError("SOURCE_WINDOW_TOO_SHORT: 视频源区间不足以覆盖广告镜头")
            segment["sourceStart"] = source_start_us / 1_000_000
            segment["sourceEnd"] = (
                source_end_us if end_us == seconds_to_us(shot_end)
                else min(source_end_us, source_start_us + window.duration_us)
            ) / 1_000_000
        segments.append(segment)
    try:
        validate_coverage(windows, TimeWindow(0, duration_us))
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    return segments


def _reference_display(db, root, task_id, plan_id, segments, preset, ratio):
    from .packaging_plan import reference_policy_for_plan
    path = root / 'runtime/workbench/tasks' / task_id / 'reference-policies' / f'{plan_id}.json'
    policy = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else reference_policy_for_plan(db, plan_id)
    if not policy:
        return preset, None
    if policy.get('ratio') != ratio:
        raise RuntimeError('REFERENCE_RATIO_MISMATCH: 参考包装的画幅与任务不一致')
    if path.is_file() and (policy.get('task_id') != task_id or policy.get('plan_id') != plan_id):
        raise RuntimeError('REFERENCE_POLICY_BINDING_INVALID: 参考包装与任务不一致')
    from .reference_editing import reference_display_cues
    style = {**preset, **policy.get('subtitle', {})}
    style.pop('ass_style', None)
    style.pop('design_version', None)
    return style, reference_display_cues(segments, style, ratio, emphasis=bool(policy.get('emphasis', {}).get('enabled')))


def _reference_packaging(db, root, task_id, plan_id, compiled, display_cues):
    if display_cues is None:
        return compiled
    from dataclasses import replace
    from .packaging_plan import reference_policy_for_plan
    from .emphasis_packaging import make_emphasis
    path = root / 'runtime/workbench/tasks' / task_id / 'reference-policies' / f'{plan_id}.json'
    policy = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else reference_policy_for_plan(db, plan_id)
    overlays, report = make_emphasis(root, task_id, plan_id, display_cues, policy)
    fields = {**compiled.plan_fields}
    if overlays:
        fields['stickers'] = [*fields.get('stickers', []), *overlays]
    snapshot = {**compiled.snapshot, 'suojian_reference': policy, 'suojian_emphasis': report}
    return replace(compiled, plan_fields=fields, snapshot=snapshot)


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
    from .voice_bundle_service import resolve_ad_render_inputs
    voice_inputs=resolve_ad_render_inputs(db,root,task["task_id"],
        script_path.read_text(encoding="utf-8"),str(brief["aspect_ratio"]))
    ad_plan,timing=voice_inputs['plan'],voice_inputs['timing']
    timing_path,voice_path=voice_inputs['timing_path'],voice_inputs['voice_path']
    if not voice_path.is_file():
        raise RuntimeError(
            "AD_PREVIEW_VOICE_MISSING: 包装预览只使用已生成配音，不会发起付费 TTS"
        )
    preview_profile = packaging_preview_output("ad_mix", brief.get("aspect_ratio"))
    cards = list_scene_cards(db, task_id)
    if any(not card.get("confirm_status") for card in cards):
        raise RuntimeError("SCENE_CONFIRMATION_REQUIRED: 广告镜头尚未全部确认")
    asset_paths = _resolved_asset_paths(db, task_id)
    duration = float(current.duration_seconds)
    render_segments = _compile_ad_render_segments(
        cards, timing["segments"], asset_paths, duration,
        resolved_sources=resolved_ad_source_windows(db.connection, task_id),
    )

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
    _attach_ad_highlights(ad_plan, render_segments,
                          compiled.plan_fields.get("subtitleHighlight", False))
    subtitle_ids = [
        item["asset_ref"]
        for item in current.plan["items"]
        if item["slot"] == "subtitle" and not item["is_backup"]
    ]
    if len(subtitle_ids) != 1:
        raise RuntimeError("AD_SUBTITLE_PRESET_REQUIRED: 必须选择唯一主字幕预设")
    subtitle_preset = find_item("subtitle", subtitle_ids[0], db)
    if subtitle_preset is None:
        raise RuntimeError("AD_SUBTITLE_PRESET_MISSING: 字幕预设不存在")

    subtitle_preset, display_cues = _reference_display(db, root, task_id, current.identity.plan_id,
                                                      timing['segments'], subtitle_preset, brief['aspect_ratio'])
    compiled = _reference_packaging(db, root, task_id, current.identity.plan_id, compiled, display_cues)

    output = (
        root
        / "runtime/workbench/cache/packaging-previews"
        / task_id
        / f"{preview_id}.mp4"
    ).resolve()
    work_dir = output.parent / f"{preview_id}-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    timeline_map = getattr(current, "timeline_map", ())
    montage = bool(timeline_map and timeline_map[-1]["previewEnd"] < duration - .01)
    render_output = work_dir / "window-0.mp4" if montage else output
    render_plan = work_dir / "adrender.json"
    recipe = compile_ad_recipe(
        task_id=task_id, ratio=brief["aspect_ratio"], segments=render_segments,
        voice_path=voice_path, subtitle_preset=subtitle_preset,
        packaging_snapshot=compiled.snapshot,
        packaging_fields=compiled.plan_fields,
        picture_lock_digest=getattr(current.identity, "picture_lock_digest", None),
        subtitle_font_path=root / "assets" / "fonts" / subtitle_preset["font"],
        display_cues=display_cues,
    )
    render_payload = ad_plan_from_recipe(recipe, preview_output={
        **({"maxDuration": duration} if duration <= 12 and not montage else {}),
        **preview_profile.render_fields(),
    })
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
        *node_script_command(node, root, "scripts/render-ad.ts"),
        "--plan",
        str(render_plan),
        "--output",
        str(render_output),
        *(["--max-duration", str(duration)] if duration <= 12 and not montage else []),
        "--width",
        str(preview_profile.width),
        "--height",
        str(preview_profile.height),
        "--crf",
        str(preview_profile.crf),
    ]
    bgm = compiled.plan_fields.get("bgm")
    if isinstance(bgm, dict) and bgm.get("src"):
        command.extend(["--bgm", str(bgm["src"])])
    return {
        "command": command,
        "output_path": render_output,
        "final_output_path": output,
        "plan_path": render_plan,
        "bgm": bgm,
        "coverage": list(compiled.coverage),
        "duration_seconds": duration,
        "recipe_digest": render_payload["recipe_digest"],
        "preview_receipt": preview_receipt(recipe, timeline_map),
        "timeline_map": timeline_map,
    }


def render_ad_packaging_preview(
    db, current, project_root: Path, preview_id: str, runner
) -> dict:
    from .workbench_actions import _default_process_runner

    runner = runner or _default_process_runner
    job = build_ad_packaging_preview_job(
        db, current, Path(project_root), preview_id
    )
    output = Path(job["output_path"])
    timeline_map = job.get("timeline_map") or ()
    montage = output != Path(job["final_output_path"])
    from .packaging_music_review import measure_rendered_music
    stem_path = output.parent / f"audio-review-{uuid.uuid4().hex}.wav"
    clips = []
    try:
        windows = timeline_map if montage else (None,)
        for index, window in enumerate(windows):
            command = list(job["command"])
            render_path = (output.parent / f"window-{index}.mp4"
                           if montage else output)
            command[command.index("--output") + 1] = str(render_path)
            if index == 0:
                command.extend(("--audio-review-output", str(stem_path)))
            if window is not None:
                command.extend(("--preview-start", str(window["fullStart"]),
                                "--preview-end", str(window["fullEnd"])))
            _run_checked(
                runner, command, cwd=Path(project_root).resolve(),
                timeout_seconds=1800, max_output_bytes=1024 * 1024,
            )
            if not filesystem_path(render_path).is_file() or filesystem_path(render_path).stat().st_size <= 0:
                raise RuntimeError("RENDER_OUTPUT_MISSING: 广告包装预览没有产生视频")
            if montage:
                clips.append(render_path)
        if montage:
            from .packaging_preview import join_preview_windows
            output = join_preview_windows(
                clips, Path(job["final_output_path"]), runner=runner, timeline_map=timeline_map)
        audio_advice = measure_rendered_music(
            job.get("bgm"), stem_path, timeline_map, default_gain=.12)
    finally:
        filesystem_path(stem_path).unlink(missing_ok=True)
        for clip in clips:
            filesystem_path(clip).unlink(missing_ok=True)
    return {
        "output_path": output,
        "duration_seconds": (timeline_map[-1]["previewEnd"]
                             if timeline_map else job["duration_seconds"]),
        "coverage": job["coverage"],
        "preview_receipt": job["preview_receipt"],
        "timeline_map": timeline_map,
        "audio_advice": audio_advice,
    }


def render_ad_and_qa(db, job, project_root, runner):
    """Render a locked ad candidate, record immutable dependencies, then run QA."""
    from .snapshots import render_epoch
    input_epoch = render_epoch(db.connection, job.task_id)
    require_bound_source_readiness(db, job.task_id)

    from .render_mix_adapter import verify_dependency_snapshot

    root = Path(project_root).resolve()
    task_root, task, brief, script_path, _script_sha = _task_context(
        db, job, root
    )
    if latest_gate(db, task["task_id"], "picture_lock") is None:
        raise RuntimeError("PICTURE_LOCK_MISSING: 必须先锁定广告画面")
    if not project_gates(db, task["task_id"])["picture_lock"]:
        raise RuntimeError("PICTURE_LOCK_MISSING: 广告画面锁已失效，必须重新确认")
    packaging_gate = latest_gate(db, task["task_id"], "packaging")
    if packaging_gate is None:
        raise RuntimeError("PACKAGING_PLAN_MISSING: 必须先确认包装方案")
    try:
        gate_details = json.loads(packaging_gate["details_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("RECIPE_PREVIEW_MISMATCH: 包装确认凭证无效") from error
    if not isinstance(gate_details, dict):
        raise RuntimeError("RECIPE_PREVIEW_MISMATCH: 包装确认凭证无效")
    confirmation = gate_details.get("confirmation_evidence") or {}
    if not isinstance(confirmation, dict):
        raise RuntimeError("RECIPE_PREVIEW_MISMATCH: 包装确认的预览凭证无效")
    expected_recipe_digest = confirmation.get("recipe_digest")
    if expected_recipe_digest is not None and not re.fullmatch(
        r"[0-9a-f]{64}", str(expected_recipe_digest)
    ):
        raise RuntimeError("RECIPE_PREVIEW_MISMATCH: 包装确认的预览配方摘要无效")
    packaging, _legacy_bgm = _confirmed_packaging(db, task["task_id"], root)
    subtitle_items = [item for item in packaging["items"]
                      if item["slot"] == "subtitle" and not item["is_backup"]]
    if len(subtitle_items) != 1:
        raise RuntimeError("AD_SUBTITLE_PRESET_REQUIRED: 必须确认唯一主字幕预设")
    subtitle_preset = find_item("subtitle", subtitle_items[0]["asset_ref"], db)
    if subtitle_preset is None:
        raise RuntimeError("AD_SUBTITLE_PRESET_MISSING: 已确认字幕预设不存在")

    qa_rework = latest_qa_only_rework(db, task["task_id"])
    action_input_digest = _render_input_digest(db, task["task_id"])
    from .performance import identity
    identity(action_input_digest, "render_action")
    candidate = latest_review_candidate(db, task["task_id"])
    retry = latest_retryable_render_snapshot(db, task["task_id"])
    if retry is not None:
        from .qa_authorization import refresh_qa_authorization
        retry = refresh_qa_authorization(db, task["task_id"], retry)
    ad_dir = task_root / "artifacts" / "ad"
    plan_path = ad_dir / "adplan.json"
    from .voice_bundle_service import resolve_ad_render_inputs
    voice_inputs=resolve_ad_render_inputs(db,root,task["task_id"],
        script_path.read_text(encoding="utf-8"),str(brief["aspect_ratio"]))
    plan,timing=voice_inputs['plan'],voice_inputs['timing']
    timing_path,voice_path=voice_inputs['timing_path'],voice_inputs['voice_path']
    cards = list_scene_cards(db, task["task_id"])
    if any(not card.get("confirm_status") for card in cards):
        raise RuntimeError("SCENE_CONFIRMATION_REQUIRED: 广告镜头尚未全部确认")
    if any(not card.get("asset_id") for card in cards):
        raise RuntimeError("AD_SCENE_ASSET_REQUIRED: 每句广告文案都必须绑定真实素材")
    try:
        validate_task_scene_uniqueness(db.connection, task["task_id"])
        validate_ad_source_windows(db.connection, task["task_id"])
    except ValueError as error:
        raise RuntimeError(str(error)) from error

    asset_paths = _resolved_asset_paths(db, task["task_id"])
    missing = [
        card["asset_id"] for card in cards if card["asset_id"] not in asset_paths
    ]
    if missing:
        raise RuntimeError("AD_SCENE_ASSET_OFFLINE: 已锁定广告素材当前离线")
    render_segments = _compile_ad_render_segments(
        cards, timing["segments"], asset_paths, float(timing["total"]),
        resolved_sources=resolved_ad_source_windows(db.connection, task["task_id"]),
    )
    subtitle_preset, display_cues = _reference_display(db, root, task['task_id'], packaging['plan_id'],
                                                      timing['segments'], subtitle_preset, brief['aspect_ratio'])
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
    compiled = _reference_packaging(db, root, task['task_id'], packaging['plan_id'], compiled, display_cues)
    runtime_files = _ad_runtime_dependencies(root, subtitle_preset,
        fancy_enabled=bool(compiled.plan_fields.get("fancyTexts")))
    _attach_ad_highlights(plan, render_segments,
                          compiled.plan_fields.get("subtitleHighlight", False))
    authorization_attestation = get_material_authorization(db, task["task_id"])
    current_shot_authorization = shot_authorization(db, task["task_id"])
    for previous, has_qa in ((candidate, True), (retry, False)):
        if previous is None or previous.get("producer") != "assemble-ad-v1":
            continue
        dependencies = previous.get("dependency_snapshot") or {}
        if dependencies.get("action_input_digest") != action_input_digest:
            if qa_rework is None or not _qa_rework_input_matches(
                    db, task["task_id"], dependencies.get("action_input_digest")):
                continue
        if (expected_recipe_digest is not None and
                dependencies.get("recipe_digest") != expected_recipe_digest):
            raise RuntimeError("RECIPE_PREVIEW_MISMATCH: 旧候选与已确认预览配方不一致")
        if dependencies.get("packaging") != packaging:
            raise RuntimeError("PACKAGING_PLAN_CHANGED: 当前包装与已渲染候选不一致")
        if dependencies.get("packaging_compiled") != compiled.snapshot:
            raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 包装素材或授权已变化")
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
    if qa_rework is not None and qa_rework["result_candidate_id"] is None:
        raise RuntimeError("QA_RETRY_INPUT_CHANGED: 原广告成片依赖已变化，不能仅重做质检")
    recipe = compile_ad_recipe(
        task_id=task["task_id"], ratio=brief["aspect_ratio"],
        segments=render_segments, voice_path=voice_path,
        subtitle_preset=subtitle_preset,
        packaging_snapshot=compiled.snapshot,
        packaging_fields=compiled.plan_fields,
        picture_lock_digest=latest_gate(db, task["task_id"], "picture_lock")["gate_digest"],
        subtitle_font_path=root / "assets" / "fonts" / subtitle_preset["font"],
        display_cues=display_cues,
    )
    render_payload = ad_plan_from_recipe(recipe)
    if (expected_recipe_digest is not None and
            render_payload["recipe_digest"] != expected_recipe_digest):
        raise RuntimeError("RECIPE_PREVIEW_MISMATCH: 正式配方与已确认预览不一致")
    version = db.connection.execute(
        "SELECT COUNT(*) + 1 FROM render_snapshots WHERE task_id=? AND stage='mix'",
        (task["task_id"],),
    ).fetchone()[0]
    output_path, work_dir = reserve_candidate_paths(root, task_root, task["task_id"], version)
    render_plan = work_dir / "adrender.json"
    render_plan.write_text(
        json.dumps(render_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    subtitle_evidence_path = work_dir / "qa-subtitles.ass"
    dependency_paths = [
        script_path, plan_path, timing_path, voice_path, render_plan,
        *voice_inputs["files"],
        *asset_paths.values(),
    ]
    dependency_paths.extend(
        Path(item["file"])
        for item in compiled.snapshot["items"]
        if item.get("file")
    )
    files = [{**_file_signature(path),**({"kind":"voice"} if Path(path)==voice_path else {})} for path in dict.fromkeys(dependency_paths)] + runtime_files
    dependency_snapshot = {
        "route": "ad_mix",
        "voice_identity": voice_inputs["identity"],
        "task_id": task["task_id"],
        "action_input_digest": action_input_digest,
        "runtime_files": runtime_files,
        "files": files,
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
        "recipe_digest": render_payload["recipe_digest"],
        "shot_authorization": current_shot_authorization,
        "material_authorization_attestation": authorization_attestation,
    }
    if not verify_dependency_snapshot(dependency_snapshot):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 渲染前广告依赖发生变化")

    node = root / "node.exe"
    if not node.is_file():
        found = shutil.which("node")
        node = Path(found) if found else node
    ts_node = root / "node_modules" / "ts-node" / "dist" / "bin.js"
    command = [
        *node_script_command(node, root, "scripts/render-ad.ts"),
        "--plan", str(render_plan),
        "--output", str(output_path),
        "--subtitle-output", str(subtitle_evidence_path),
    ]
    bgm = compiled.plan_fields.get("bgm")
    if isinstance(bgm, dict) and bgm.get("src"):
        command.extend(["--bgm", str(bgm["src"])])
    from .performance import measured, metric
    metric("cache_hits", 0)
    measured("render")(_run_checked)(
        runner, command, cwd=root, timeout_seconds=3600,
        max_output_bytes=1024 * 1024,
    )
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("RENDER_OUTPUT_MISSING: 广告渲染没有产生候选成片")
    if not verify_dependency_snapshot(dependency_snapshot):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 渲染期间广告依赖发生变化")
    if not subtitle_evidence_path.is_file() or subtitle_evidence_path.stat().st_size <= 0:
        raise RuntimeError("AD_SUBTITLE_EVIDENCE_MISSING: 渲染器没有保留实际烧录字幕")
    subtitle_signature = _file_signature(subtitle_evidence_path)
    dependency_snapshot["files"].append(subtitle_signature)
    dependency_snapshot["subtitle_evidence"] = subtitle_signature
    if not verify_dependency_snapshot(dependency_snapshot):
        raise RuntimeError("DEPENDENCY_SNAPSHOT_CHANGED: 广告字幕或输入在证据绑定时变化")
    dependency_digest = _canonical_digest(dependency_snapshot)

    render_job = {
        "render_epoch": input_epoch,
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


from .performance import measured as _performance_measured

@_performance_measured("qa_finalize")
def _finish_ad_render_qa(db, task_id: str, render_snapshot: dict, *, reused=False):
    output_path = Path(render_snapshot["output_path"])
    dependency_digest = render_snapshot["dependency_digest"]
    qa_result = run_qa(db, task_id, str(output_path), evidence=_ad_qa_evidence(db, render_snapshot))
    eligible = qa_result["status"] == "passed" and qa_evidence_complete(qa_result)
    snapshots = (record_qa_review_snapshot(db, task_id, render_snapshot, qa_result,
                 producer="assemble-ad-v1") if eligible else render_snapshot)
    # Candidate, request completion and workflow projection commit together.
    # A late result may retain historical QA evidence but cannot overwrite state.
    with db.transaction(immediate=True):
        candidate = record_candidate_qa(db, task_id, render_snapshot, qa_result,
            qa_snapshot_id=snapshots.get("qa_snapshot_id") if eligible else None,
            eligible_for_review=eligible)
        complete_stage(db, task_id, "fine_render", input_digest=dependency_digest,
                       details={"output_path": str(output_path), **render_snapshot})
        complete_stage(db, task_id, "auto_qa", input_digest=_canonical_digest({
            "dependency_digest": dependency_digest, "qa_report_id": qa_result["report_id"],
        }), details={"qa_report_id": qa_result["report_id"], "status": qa_result["status"],
                     "blocking_count": qa_result["blocking_count"]})
    if not eligible:
        return _action_result("render_and_qa:qa_blocked", "广告成片质检发现阻断项", {
            "status": "qa_blocked", "output_path": str(output_path), "qa": qa_result,
            "snapshots": render_snapshot, "candidate": candidate, "reused": reused,
        })
    return _action_result("render_and_qa:complete", "广告精剪成片与自动质检完成", {
        "status": "awaiting_human_review", "output_path": str(output_path),
        "qa": qa_result, "snapshots": {**snapshots, **candidate},
        "candidate": candidate, "reused": reused,
    })


def build_ad_publish_package(db, job, project_root, runner):
    """Compatibility entry; delivery changes must not live in render logic."""
    from .ad_publish_actions import build_ad_publish_package as build_delivery
    return build_delivery(db, job, project_root, runner)
