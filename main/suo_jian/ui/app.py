from __future__ import annotations

import shutil
import uuid
import json
import re
import os
from threading import Thread, Lock
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename
from suo_jian.pipeline import analyze
from suo_jian.ui.video_link import validate_url, download_video
from suo_jian.ui.platform_video import extract_input_url, import_video_link
from urllib.parse import urlsplit
from suo_jian.blueprint import create_draft_blueprint, enrich_blueprint
from suo_jian.blueprint.retime import extend_for_speech
from suo_jian.jianying import JianyingBuilder
from suo_jian.jianying.cutroom_import import build_from_cutroom_delivery
from suo_jian.integrations.cutroom.capability import inspect_local_workbench
from suo_jian.integrations.cutroom.client import call_cutroom_mcp, call_readonly_mcp
from suo_jian.integrations.cutroom.mapper import make_cutroom_job, make_ad_task_request, make_ad_storyboard

ROOT = Path(__file__).resolve().parents[2]
UPLOADS, RESULTS = ROOT / "uploads", ROOT / "results"
JIAN_YING_PROJECTS = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "User Data" / "Projects"
UPLOADS.mkdir(exist_ok=True); RESULTS.mkdir(exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
from suo_jian.ui.production_methods import register_production_methods
register_production_methods(app, ROOT)
from suo_jian.ui.native_motion import register_native_motion
register_native_motion(app)
from suo_jian.ui.motion_explainer import bp as motion_explainer_bp
app.register_blueprint(motion_explainer_bp)
from suo_jian.ui.model_settings import register_model_settings
register_model_settings(app)
from suo_jian.ui.park_vibe import register_park_vibe
register_park_vibe(app)
JOBS: dict[str, dict] = {}
ANALYSIS_START_LOCK = Lock()


def _safe_job(job: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{8}", job):
        raise ValueError("无效任务编号。")
    return job


def _save_job_state(job: str, state: dict) -> None:
    """Persist lightweight progress so a browser or service restart is recoverable."""
    result_dir = RESULTS / _safe_job(job)
    result_dir.mkdir(parents=True, exist_ok=True)
    state_path = result_dir / "analysis_status.json"
    temporary = state_path.with_suffix(".tmp")
    persisted = {key: value for key, value in state.items() if key != "blueprint"}
    temporary.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(state_path)


def _reference_for(job: str) -> tuple[dict, Path, Path]:
    job = _safe_job(job)
    result_dir = RESULTS / job
    blueprint_path = result_dir / "edit_blueprint.json"
    if not blueprint_path.exists():
        raise FileNotFoundError("找不到该任务的分析蓝图。")
    videos = list(UPLOADS.glob(f"{job}_*"))
    if not videos:
        raise FileNotFoundError("找不到该任务的参考视频。")
    blueprint = enrich_blueprint(json.loads(blueprint_path.read_text(encoding="utf-8")))
    return blueprint, videos[0], result_dir


def _validated_preview_draft(result_dir: Path, value: object) -> dict:
    if not isinstance(value, dict) or value.get("schema_version") != "draft-0.1":
        raise ValueError("草稿格式无效，请重新生成预览。")
    if not isinstance(value.get("timeline"), list) or not value["timeline"]:
        raise ValueError("草稿没有可用时间线，请重新生成预览。")
    preview_path = result_dir / "draft_blueprint.json"
    if not preview_path.is_file():
        raise ValueError("请先生成草稿预览，再确认生成。")
    saved = json.loads(preview_path.read_text(encoding="utf-8"))
    submitted_for_compare = dict(value)
    saved_for_compare = dict(saved)
    submitted_for_compare.pop("draft_generation", None)
    saved_for_compare.pop("draft_generation", None)
    if submitted_for_compare != saved_for_compare:
        raise ValueError("当前方案与已展示的预览不一致，请重新预览后再继续。")
    return saved

@app.get("/")
def index():
    if request.args.get('job') or request.args.get('studio') in {'feed', 'presenter'}:
        return render_template('index.html')
    return render_template('studio_home.html')

@app.post("/api/analyze")
def api_analyze():
    uploaded = request.files.get("video")
    if not uploaded or not uploaded.filename: return jsonify(error="请选择一个 MP4 文件。"), 400
    if Path(uploaded.filename).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}: return jsonify(error="当前 Demo 接受 MP4 / MOV / MKV / WebM。"), 400
    job = uuid.uuid4().hex[:8]
    filename = secure_filename(uploaded.filename) or "video.mp4"
    video = UPLOADS / f"{job}_{filename}"
    uploaded.save(video)
    _start_analysis(job, video)
    return jsonify(job=job, status="running")


@app.post("/api/analyze/link")
def api_analyze_link():
    if request.host.split(":")[0] not in {"127.0.0.1","localhost"} or request.headers.get("Sec-Fetch-Site")=="cross-site":
        return jsonify(error="请从本机工作台导入视频链接。"),403
    origin=request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        return jsonify(error="请从本机工作台导入视频链接。"),403
    data=request.get_json(silent=True)
    if not isinstance(data,dict):return jsonify(error="请填写视频链接。"),400
    try:url=extract_input_url(data.get("url"))
    except ValueError as error:return jsonify(error=str(error)),400
    job=uuid.uuid4().hex[:8]
    _start_analysis(job,UPLOADS/f"{job}_linked_video.mp4",download_url=url)
    return jsonify(job=job,status="running")


def _start_analysis(job, video, download_url=None):
    JOBS[job] = {"progress": 1, "stage": "正在下载视频到本机" if download_url else "准备解析／读取已完成步骤", "status": "running"}
    if download_url:JOBS[job]["source_url"]=download_url
    _save_job_state(job, JOBS[job])
    def run():
        try:
            def update(progress, stage):
                JOBS[job].update(progress=progress, stage=stage)
                _save_job_state(job, JOBS[job])
            if download_url:
                def download_progress(size,total):
                    detail=f"{size/1048576:.1f} MB" + (f" / {total/1048576:.1f} MB" if total else "")
                    update(min(4,1+3*size/total) if total else 2,"正在下载视频（"+detail+"）")
                import_video_link(download_url,video,download_progress,lambda message:update(2,message))
                update(4,"视频已下载，准备解析")
            blueprint, _, _ = analyze(video, RESULTS / job, with_subtitles=True, with_stems=True, progress=update)
            JOBS[job].update(status="completed", progress=100, stage="分析完成", blueprint=blueprint, video_url=f"/api/video/{video.name}", json_url=f"/api/result/{job}")
            _save_job_state(job, JOBS[job])
        except Exception as exc:
            JOBS[job].update(status="failed", error=str(exc), stage="解析失败")
            _save_job_state(job, JOBS[job])
    Thread(target=run, daemon=True).start()


@app.post("/api/analyze/<job>/resume")
def resume_analysis(job):
    job = _safe_job(job)
    if request.host.split(":")[0] not in {"127.0.0.1", "localhost"} or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify(error="请从本机工作台恢复解析。"), 403
    with ANALYSIS_START_LOCK:
        if JOBS.get(job, {}).get("status") == "running":
            return jsonify(job=job,status="running")
        video = next(UPLOADS.glob(f"{job}_*"), None)
        state_path=RESULTS/job/"analysis_status.json"
        if not state_path.is_file():
            return jsonify(error="未找到解析记录，请重新拖入视频或粘贴链接。"),404
        saved=json.loads(state_path.read_text("utf-8"))
        source_url=saved.get("source_url") if video is None else None
        if video is None and not source_url:
            return jsonify(error="未找到原视频，请重新拖入视频。"),404
        if video is None:video=UPLOADS/f"{job}_linked_video.mp4"
        if (RESULTS/job/"edit_blueprint.json").is_file():
            return jsonify(job=job,status="completed")
        _start_analysis(job,video,download_url=source_url)
    return jsonify(job=job,status="running")

@app.get("/api/analyze/<job>/progress")
def analyze_progress(job):
    job = _safe_job(job)
    data = JOBS.get(job)
    if data is None:
        state_path = RESULTS / job / "analysis_status.json"
        if not state_path.is_file():
            return jsonify(error="找不到解析任务"), 404
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return jsonify(error="任务状态文件无法读取，请重新解析。"), 500
        if data.get("status") == "running":
            blueprint_path = RESULTS / job / "edit_blueprint.json"
            if blueprint_path.is_file():
                video_name = next((p.name for p in UPLOADS.glob(f"{job}_*")), None)
                data.update(status="completed", progress=100, stage="分析完成",
                            blueprint=json.loads(blueprint_path.read_text(encoding="utf-8")),
                            video_url=f"/api/video/{video_name}" if video_name else None,
                            json_url=f"/api/result/{job}")
            else:
                data.update(status="interrupted", stage="解析已中断，可从已完成步骤继续，无需重新上传。")
            _save_job_state(job, data)
        elif data.get("status") == "completed":
            blueprint_path = RESULTS / job / "edit_blueprint.json"
            if blueprint_path.is_file():
                data["blueprint"] = json.loads(blueprint_path.read_text(encoding="utf-8"))
            video_name = next((p.name for p in UPLOADS.glob(f"{job}_*")), None)
            data["video_url"] = f"/api/video/{video_name}" if video_name else None
            data["json_url"] = f"/api/result/{job}"
    if data.get("status") == "completed":
        draft_path = RESULTS / job / "draft_blueprint.json"
        if draft_path.is_file():
            data["draft_blueprint"] = json.loads(draft_path.read_text(encoding="utf-8"))
        from suo_jian.ui.unified_api import analysis_audio_path
        blueprint = data.get("blueprint", {})
        audio_urls = {}
        for kind in ["original"] + [row.get("kind") for row in blueprint.get("audio_stems", {}).get("tracks", [])]:
            if not kind or not re.fullmatch(r"[a-z_]+", kind):
                continue
            try:
                analysis_audio_path(blueprint, RESULTS / job, kind)
                audio_urls[kind] = f"/api/workbench/audio/{job}/{kind}"
            except (ValueError, FileNotFoundError):
                continue
        data["source_audio_url"] = audio_urls.pop("original", "")
        data["stem_urls"] = audio_urls
    return jsonify(data)

@app.get("/api/video/<path:name>")
def video(name): return send_from_directory(UPLOADS, name)

@app.get("/api/result/<job>")
def result(job): return send_from_directory(RESULTS / _safe_job(job), "edit_blueprint.json", as_attachment=True)


@app.post("/api/draft/preview/<job>")
def draft_preview(job):
    try:
        reference, _, result_dir = _reference_for(job)
        payload = request.get_json(silent=True) or {}
        adaptation = payload.get("adaptation", "smart")
        if adaptation not in {"smart", "strict"}:
            return jsonify(error="adaptation 必须是 smart 或 strict。"), 400
        draft = create_draft_blueprint(
            reference,
            script_text=str(payload.get("script", "")),
            adaptation=adaptation,
            keep_original=bool(payload.get("keep_original")),
        )
        (result_dir / "draft_blueprint.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify(draft_blueprint=draft)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/cutroom/capabilities")
def cutroom_capabilities():
    root = os.environ.get("SUOJIAN_CUTROOM_ROOT")
    audit = inspect_local_workbench(root)
    if audit.get("mcp_source_available"):
        try:
            audit["live_status"] = call_readonly_mcp(audit["project_path"], "cutroom_get_system_status")
        except Exception as exc:
            audit["live_error"] = str(exc)
    return jsonify(audit)


@app.post("/api/cutroom/preflight/<job>")
def cutroom_preflight(job):
    """Read-only 剪辑工作台 validation. Never creates a task or starts a paid job."""
    try:
        reference, _, result_dir = _reference_for(job)
        payload = request.get_json(silent=True) or {}
        draft = _validated_preview_draft(result_dir, payload.get("draft_blueprint"))
        audit = inspect_local_workbench(os.environ.get("SUOJIAN_CUTROOM_ROOT"))
        if not audit.get("mcp_source_available"):
            return jsonify(error="本机 剪辑工作台 尚未部署 Agent MCP。"), 503
        task_request = make_ad_task_request(
            draft, title=payload.get("title", f"溯剪-{job}"),
            purpose=payload.get("purpose", "参考视频风格复刻"),
            primary_library_id=payload.get("primary_library_id", ""),
            aspect_ratio=payload.get("aspect_ratio") or (
                "16:9" if reference["source"]["width"] >= reference["source"]["height"] else "9:16"),
            selected_generic_ids=payload.get("selected_generic_ids"),
        )
        result = call_readonly_mcp(audit["project_path"], "cutroom_preflight_task",
                                   {"request": task_request})
        return jsonify(result=result, task_request=task_request, creates_task=False)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/cutroom/create/<job>")
def cutroom_create(job):
    """User-confirmed task creation; no paid action or render is started."""
    try:
        reference, _, result_dir = _reference_for(job)
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm_create") is not True:
            return jsonify(error="请先确认创建 剪辑工作台 任务。"), 400
        draft = _validated_preview_draft(result_dir, payload.get("draft_blueprint"))
        binding_path = result_dir / "cutroom_binding.json"
        if binding_path.is_file():
            return jsonify(error="该溯剪方案已提交 剪辑工作台，请先查看原任务，避免重复创建。"), 409
        audit = inspect_local_workbench(os.environ.get("SUOJIAN_CUTROOM_ROOT"))
        if not audit.get("mcp_source_available"):
            return jsonify(error="剪辑工作台 Agent MCP 尚未就绪。"), 503
        task_request = make_ad_task_request(
            draft, title=payload.get("title", f"溯剪-{job}"),
            purpose=payload.get("purpose", "参考视频风格复刻"),
            primary_library_id=payload.get("primary_library_id", os.environ.get(
                "SUOJIAN_CUTROOM_LIBRARY_ID", "suojian-jiaxing-park")),
            aspect_ratio=payload.get("aspect_ratio") or (
                "16:9" if reference["source"]["width"] >= reference["source"]["height"] else "9:16"),
        )
        root = audit["project_path"]
        preflight = call_readonly_mcp(root, "cutroom_preflight_task", {"request": task_request})
        if not preflight.get("ok") or preflight.get("data", {}).get("blocking"):
            return jsonify(error="剪辑工作台 创建前检查未通过。", preflight=preflight), 409
        created = call_cutroom_mcp(root, "cutroom_create_task", {"request": task_request})
        if not created.get("ok"):
            return jsonify(error="剪辑工作台 未能创建任务。", result=created), 502
        task_id = created["data"]["taskId"]
        binding = {"schema_version": "suojian-cutroom-binding-1", "task_id": task_id,
                   "library_id": task_request["primary_library_id"],
                   "aspect_ratio": task_request["aspect_ratio"],
                   "status": "created", "cutroom_root": root}
        binding_path.write_text(json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
        links = call_readonly_mcp(root, "cutroom_get_result_links", {"task_id": task_id})
        return jsonify(binding=binding, task=created.get("data"),
                       task_url=(links.get("data") or {}).get("taskUrl"),
                       message="剪辑工作台 任务已创建；请在 剪辑工作台 确认文案并继续生成预览。尚未调用付费模型。")
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/cutroom/extend/<job>")
def cutroom_extend(job):
    """Create a new, longer unbilled task; keep the original task recoverable."""
    try:
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm_extend") is not True:
            return jsonify(error="请先确认延长视频并创建新版 剪辑工作台 任务。"), 400
        reference, _, result_dir = _reference_for(job)
        binding_path = result_dir / "cutroom_binding.json"
        if not binding_path.is_file():
            return jsonify(error="尚无可延长的 剪辑工作台 任务。"), 404
        previous = json.loads(binding_path.read_text(encoding="utf-8"))
        old_task = call_readonly_mcp(previous["cutroom_root"], "cutroom_get_task",
                                     {"task_id": previous["task_id"]})
        if not old_task.get("ok"):
            return jsonify(error="无法核实原任务状态，不创建新版。"), 409
        draft_path = result_dir / "draft_blueprint.json"
        original = json.loads(draft_path.read_text(encoding="utf-8"))
        if original.get("retiming"):
            return jsonify(error="当前方案已经延长；请先查看新版任务。"), 409
        target = float(payload.get("target_duration_seconds", 36.0))
        extended = extend_for_speech(original, target_seconds=target)
        audit = inspect_local_workbench(previous["cutroom_root"])
        root = audit["project_path"]
        task_request = make_ad_task_request(
            extended, title=payload.get("title", f"溯剪-嘉兴平湖新仓智谷天地-{target:g}秒"),
            purpose=payload.get("purpose", "信息流项目招商"),
            primary_library_id=previous["library_id"],
            aspect_ratio=previous["aspect_ratio"],
        )
        preflight = call_readonly_mcp(root, "cutroom_preflight_task", {"request": task_request})
        if not preflight.get("ok") or preflight.get("data", {}).get("blocking"):
            return jsonify(error="新版任务预检未通过。", preflight=preflight), 409
        created = call_cutroom_mcp(root, "cutroom_create_task", {"request": task_request})
        if not created.get("ok"):
            return jsonify(error="剪辑工作台 未能创建新版任务。", result=created), 502
        task_id = created["data"]["taskId"]
        (result_dir / "draft_blueprint_original.json").write_text(
            json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
        draft_path.write_text(json.dumps(extended, ensure_ascii=False, indent=2), encoding="utf-8")
        updated = {**previous, "task_id": task_id, "target_duration_seconds": target,
                   "previous_task_ids": [*previous.get("previous_task_ids", []), previous["task_id"]]}
        binding_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        links = call_readonly_mcp(root, "cutroom_get_result_links", {"task_id": task_id})
        return jsonify(binding=updated, draft_blueprint=extended,
                       task_url=(links.get("data") or {}).get("taskUrl"),
                       message="已创建延长版任务；仍需在 剪辑工作台 核对并锁定文案，未启动付费生成。")
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/cutroom/task/<job>")
def cutroom_task(job):
    try:
        binding_path = RESULTS / _safe_job(job) / "cutroom_binding.json"
        if not binding_path.is_file():
            return jsonify(error="此方案尚未提交 剪辑工作台。"), 404
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        root = binding["cutroom_root"]
        task_id = binding["task_id"]
        task = call_readonly_mcp(root, "cutroom_get_task", {"task_id": task_id})
        links = call_readonly_mcp(root, "cutroom_get_result_links", {"task_id": task_id})
        return jsonify(binding=binding, task=task, links=links)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/cutroom/submit-plan/<job>")
def cutroom_submit_plan(job):
    """Submit a reviewed SuoJian storyboard only after 剪辑工作台 gates allow it."""
    try:
        if (request.get_json(silent=True) or {}).get("confirm_submit") is not True:
            return jsonify(error="请确认向 剪辑工作台 提交溯剪分镜。"), 400
        result_dir = RESULTS / _safe_job(job)
        binding_path = result_dir / "cutroom_binding.json"
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        draft = json.loads((result_dir / "draft_blueprint.json").read_text(encoding="utf-8"))
        root, task_id = binding["cutroom_root"], binding["task_id"]
        context = call_readonly_mcp(root, "cutroom_get_agent_context", {"task_id": task_id})
        if not context.get("ok"):
            return jsonify(error="剪辑工作台 尚未允许提交分镜；请先锁定文案并同步素材。",
                           result=context), 409
        data = context["data"]
        guidance_path = result_dir / "storyboard_guidance.json"
        visual_guidance = (json.loads(guidance_path.read_text(encoding="utf-8"))
                           if guidance_path.is_file() else None)
        content = make_ad_storyboard(
            draft, data["script"]["text"], binding["aspect_ratio"], visual_guidance,
        )
        result = call_cutroom_mcp(root, "cutroom_submit_planning_artifact", {
            "task_id": task_id, "artifact_type": "ad_storyboard", "schema_version": 1,
            "input_digest": data["inputDigest"], "content": content,
            "source_model_label": "SuoJian slot mapper",
        })
        if not result.get("ok"):
            return jsonify(error="剪辑工作台 拒绝了分镜方案。", result=result), 409
        binding.update(status="storyboard_submitted")
        binding_path.write_text(json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify(result=result, message="溯剪分镜已提交 剪辑工作台。请在 剪辑工作台 审核并确认后续 TTS 费用与预览生成。")
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/cutroom/import/<job>")
def cutroom_import(job):
    """Import a reviewed 剪辑工作台 editable package after explicit user approval."""
    try:
        if (request.get_json(silent=True) or {}).get("confirm_import") is not True:
            return jsonify(error="请先确认 剪辑工作台 预览并同意生成剪映草稿。"), 400
        reference, _, result_dir = _reference_for(job)
        binding_path = result_dir / "cutroom_binding.json"
        if not binding_path.is_file():
            return jsonify(error="此方案还没有 剪辑工作台 任务。"), 404
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        task_id = binding["task_id"]
        if not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
            raise ValueError("剪辑工作台 任务编号无效。")
        package_root = Path(binding["cutroom_root"]).resolve() / "out"
        candidates = sorted(
            (p for p in package_root.glob(f"{task_id}*")
             if p.is_dir() and (p / "delivery-manifest.json").is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            return jsonify(error="剪辑工作台 尚未生成可编辑交付包。请先完成预览、人工确认和发布包生成。"), 409
        package = candidates[0]
        check_path = package / "publish-check.json"
        if not check_path.is_file() or json.loads(check_path.read_text(encoding="utf-8")).get("task_id") != task_id:
            raise ValueError("剪辑工作台 发布包与当前任务不匹配。")
        draft = json.loads((result_dir / "draft_blueprint.json").read_text(encoding="utf-8"))
        draft_root = JIAN_YING_PROJECTS if JIAN_YING_PROJECTS.is_dir() else result_dir / "jianying_draft"
        draft_name = f"溯剪-剪辑工作台-{job}"
        output, report = build_from_cutroom_delivery(package, reference, draft, draft_root, draft_name)
        archive = shutil.make_archive(str(result_dir / "cutroom_jianying_draft"), "zip",
                                      root_dir=output.parent, base_dir=output.name)
        binding.update(status="jianying_draft_generated", package_dir=str(package),
                       jianying_draft_dir=str(output), import_report=report)
        binding_path.write_text(json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify(status="completed", report=report, output=str(output),
                       archive_url=f"/api/cutroom/archive/{job}",
                       message="剪辑工作台 交付包已转换为剪映草稿。请在剪映中打开检查轨道。")
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/cutroom/archive/<job>")
def cutroom_archive(job):
    return send_from_directory(RESULTS / _safe_job(job), "cutroom_jianying_draft.zip", as_attachment=True)


@app.post("/api/cutroom/prepare/<job>")
def cutroom_prepare(job):
    try:
        reference, video_path, result_dir = _reference_for(job)
        payload = request.get_json(silent=True) or {}
        draft = _validated_preview_draft(result_dir, payload.get("draft_blueprint"))
        cutroom_job = make_cutroom_job(reference, draft, video_path, job_id=job,
                                       options=payload.get("options"))
        capability = inspect_local_workbench(os.environ.get("SUOJIAN_CUTROOM_ROOT"))
        cutroom_job["status"] = "waiting_for_cutroom_preflight"
        cutroom_job["capability_audit"] = capability
        (result_dir / "cutroom_job.json").write_text(
            json.dumps(cutroom_job, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify(job=cutroom_job, executable=False,
                       message="已准备方案；请先选择 剪辑工作台 素材库并进行创建前检查。此步未创建任务，也未触发付费服务或渲染。")
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/draft/approve/<job>")
def draft_approve(job):
    try:
        reference, video_path, result_dir = _reference_for(job)
        payload = request.get_json(silent=True) or {}
        draft = _validated_preview_draft(result_dir, payload.get("draft_blueprint"))
        draft["draft_generation"] = {"status": "approved", "approved": True}
        draft_path = result_dir / "draft_blueprint.json"
        draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            # 浏览器工作台运行在本机时，直接落到剪映的草稿库；离线 preview.html 不具备此能力。
            draft_root = JIAN_YING_PROJECTS if JIAN_YING_PROJECTS.is_dir() else result_dir / "jianying_draft"
            draft_name = f"溯剪-{job}"
            output = JianyingBuilder().build(reference, draft, video_path, draft_root, draft_name)
            draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
            archive = shutil.make_archive(str(result_dir / "jianying_draft"), "zip", root_dir=output.parent, base_dir=output.name)
            return jsonify(message=f"剪映草稿“{draft_name}”已写入本机草稿库；草稿蓝图已保留。", status="completed", draft_blueprint_url=f"/api/draft/result/{job}", archive_url=f"/api/draft/archive/{job}", output=archive)
        except Exception as exc:
            draft["draft_generation"].update({"status": "generation_failed", "completed": False, "error": str(exc)})
            draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
            return jsonify(message=f"剪映草稿生成失败，但真实草稿蓝图已保留：{exc}", status="generation_failed", draft_blueprint_url=f"/api/draft/result/{job}"), 200
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/draft/result/<job>")
def draft_result(job):
    _safe_job(job)
    return send_from_directory(RESULTS / job, "draft_blueprint.json", as_attachment=True)


@app.get("/api/draft/archive/<job>")
def draft_archive(job):
    _safe_job(job)
    return send_from_directory(RESULTS / job, "jianying_draft.zip", as_attachment=True)

from suo_jian.ui.unified_api import register_unified_routes
register_unified_routes(app, ROOT, _reference_for)
from suo_jian.ui.presenter_api import register_presenter_routes
register_presenter_routes(app, ROOT, _reference_for)
from suo_jian.ui.studio_home import register_studio_routes
register_studio_routes(app, ROOT)

if __name__ == "__main__": app.run(host="127.0.0.1", port=8788, debug=False)
