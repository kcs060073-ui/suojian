"""Single local UI boundary. Old endpoints remain for compatibility only."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse
from flask import jsonify, request, send_file

from suo_jian.blueprint.templates import TemplateStore, digest, write_json
from suo_jian.integrations.cutroom.client import call_local_adapter
from suo_jian.integrations.cutroom.unified import UnifiedExecution, read_state
from suo_jian.jianying.execution_export import build_execution_draft, editable_emphasis_plan
from suo_jian.jianying.text_geometry import prepare_export_plan
from suo_jian.jianying.layered_emphasis import layered_emphasis_plan

EXPORT_LOCK = threading.Lock()


def analysis_audio_path(blueprint: dict, result_dir: Path, kind: str) -> Path:
    folder = (result_dir / "audio_stems").resolve()
    if kind == "original":
        path = folder / "source.wav"
    else:
        track = next((row for row in blueprint.get("audio_stems", {}).get("tracks", []) if row.get("kind") == kind), None)
        if not track or not track.get("path"):
            raise FileNotFoundError("该分析音轨尚未生成。")
        path = Path(track["path"]).resolve()
    path.relative_to(folder)
    if not path.is_file():
        raise FileNotFoundError("分析音轨文件已移动或尚未生成。")
    return path


def register_unified_routes(app, workspace: Path, reference_for):
    store = TemplateStore(workspace / "templates" / "editing")
    def root():
        return Path(os.environ.get("SUOJIAN_CUTROOM_ROOT", str(Path.home() / "CUTROOM"))).resolve()
    def execution(job):
        _, _, result_dir = reference_for(job)
        return UnifiedExecution(root(), result_dir, store)
    def payload():
        if request.host.split(":")[0] not in {"127.0.0.1", "localhost"}:
            raise PermissionError("只接受本机工作台请求。")
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            raise PermissionError("不接受其他网站提交的生成请求。")
        if request.headers.get("Sec-Fetch-Site") == "cross-site" or not request.is_json:
            raise PermissionError("请从溯剪本机工作台提交。")
        return request.get_json() or {}
    def public(state):
        value = {k: v for k, v in state.items() if k not in {"preview_path", "storyboard"}}
        value["has_preview"] = bool(state.get("preview_path"))
        if state.get("config"):
            value["script"] = state["config"].get("script", "")
        return value
    def guarded(function):
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except PermissionError as exc:
                return jsonify(error=str(exc)), 403
            except (ValueError, KeyError, FileNotFoundError, FileExistsError) as exc:
                return jsonify(error=str(exc)), 400
            except Exception as exc:
                return jsonify(error=str(exc)), 500
        wrapped.__name__ = function.__name__
        return wrapped

    @app.get("/api/unified/catalog")
    @guarded
    def unified_catalog():
        return jsonify(**call_local_adapter(root(), "catalog"), editing_templates=store.list())

    @app.get("/api/workbench/tasks")
    @guarded
    def workbench_tasks():
        """Local analysis history only: no provider request and no secret settings."""
        tasks = []
        results = workspace / "results"
        labels = {"running": "正在生成", "waiting_worker": "排队中", "ready": "待审看",
                  "qa_blocked": "有质检提示", "exported": "草稿已保存", "failed": "需要处理",
                  "needs_check": "需要核对"}
        if not results.is_dir():
            return jsonify(tasks=[])
        directories = sorted((p for p in results.iterdir() if p.is_dir() and re.fullmatch(r"[0-9a-f]{8}", p.name)),
                             key=lambda p: p.stat().st_mtime, reverse=True)[:60]
        for directory in directories:
            try:
                def read(filename):
                    path = directory / filename
                    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                analysis, blueprint, execution_state = read("analysis_status.json"), read("edit_blueprint.json"), read("execution.json")
                if not analysis and not blueprint:
                    continue
                status = execution_state.get("status", "not_started")
                done = analysis.get("status") == "completed" or bool(blueprint)
                label = labels.get(status, "分析已完成" if done else {"running":"正在分析", "failed":"分析失败", "interrupted":"分析中断"}.get(analysis.get("status"), "等待分析"))
                filename = blueprint.get("source", {}).get("file_name") or analysis.get("file_name") or "参考视频 · " + directory.name
                filename = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
                if filename.startswith(directory.name + "_"):
                    filename = filename[9:]
                tasks.append({"job": directory.name, "title": filename, "status_label": label,
                              "stage": execution_state.get("phase") or analysis.get("stage") or "参考视频分析",
                              "needs_attention": status in {"ready", "qa_blocked", "failed", "needs_check"} or analysis.get("status") in {"failed", "interrupted"},
                              "updated_at": execution_state.get("updated_at")})
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return jsonify(tasks=tasks)

    @app.get("/api/workbench/audio/<job>/<kind>")
    @guarded
    def workbench_audio(job, kind):
        blueprint, _, result_dir = reference_for(job)
        return send_file(analysis_audio_path(blueprint, result_dir, kind), conditional=True)

    @app.post("/api/unified/templates/<job>")
    @guarded
    def unified_save_template(job):
        data = payload()
        reference, _, _ = reference_for(job)
        return jsonify(template=store.save(reference, str(data.get("name", "")), job))

    @app.post("/api/unified/generate/<job>")
    @guarded
    def unified_generate(job):
        return jsonify(public(execution(job).start(payload())))

    @app.post("/api/unified/resume/<job>")
    @guarded
    def unified_resume(job):
        payload()
        return jsonify(public(execution(job).resume()))

    @app.get("/api/unified/packaging/<job>")
    @guarded
    def unified_packaging(job):
        runner = execution(job)
        if not runner.state.get("task_id"):
            return jsonify(catalog=call_local_adapter(root(), "catalog")["packaging_items"], current=None)
        return jsonify(runner.call("packaging_options"))

    @app.post("/api/unified/repackage/<job>")
    @guarded
    def unified_repackage(job):
        return jsonify(public(execution(job).repackage(payload())))

    @app.get("/api/unified/status/<job>")
    @guarded
    def unified_status(job):
        return jsonify(public(execution(job).refresh_completed_repackage()))

    @app.post("/api/unified/adopt/<job>")
    @guarded
    def unified_adopt(job):
        data = payload()
        if data.get("confirm_reuse") is not True:
            raise PermissionError("请确认复用当前已绑定的任务；此操作不重新配音。")
        runner = execution(job)
        if runner.state.get("status") in {"running", "waiting_worker"}:
            raise ValueError("生成任务正在运行，不能切换绑定。")
        binding = json.loads((runner.result_dir / "cutroom_binding.json").read_text(encoding="utf-8"))
        runner.state["task_id"] = binding["task_id"]
        context = runner.call("context")
        runner.save(task_id=binding["task_id"], config={"template_id": data.get("template_id"),
                    "script": context["script"]["text"], "library_id": binding.get("library_id"),
                    "tts_profile_id": binding.get("tts_profile_id")}, human_reviewed=False)
        return jsonify(public(runner.collect()))

    @app.post("/api/unified/save-packaging/<job>")
    @guarded
    def unified_save_packaging(job):
        data, runner = payload(), execution(job)
        template = store.get(data["template_id"])
        if runner.state.get("status") in {"running", "waiting_worker"}:
            raise ValueError("请等待当前预览完成后再保存模板。")
        task = runner.call("inspect")
        plans = [p for p in task["plans"] if p["status"] == "confirmed"]
        if not plans:
            raise ValueError("请先生成一次预览，才能保存实际采用的 Cutroom 包装组合。")
        candidates = task.get("candidates") or []
        candidate = next((c for c in candidates if c.get("candidate_id") == runner.state.get("candidate_id")), None)
        if candidate is None:
            raise ValueError("请先读取已完成的预览，再保存对应模板。")
        policy_path = runner.result_dir / "execution_plan.json"
        actual = json.loads(policy_path.read_text("utf-8"))
        bound_plan = (actual.get("reference_match") or {}).get("policy", {}).get("plan_id")
        if bound_plan and bound_plan != plans[-1]["plan_id"]:
            raise ValueError("包装配置与当前预览版本不同，请先更新预览再保存模板。")
        saved = runner.call("save_packaging", plan_id=plans[-1]["plan_id"], name="溯剪 · " + template["name"])
        return jsonify(template=store.link_packaging(template["template_id"], saved["template_id"], choices=saved.get("choices"), reference_policy=saved.get("reference_policy")))

    @app.get("/api/unified/preview/<job>")
    @guarded
    def unified_preview(job):
        runner = execution(job)
        path = Path(runner.state.get("preview_path", "")).resolve()
        if not runner.state.get("task_id"):
            raise ValueError("尚未生成预览。")
        path.relative_to((runner.root / "out" / runner.state["task_id"]).resolve())
        return send_file(path, conditional=True, mimetype="video/mp4")

    @app.post("/api/unified/export/<job>")
    @guarded
    def unified_export(job):
        data, runner = payload(), execution(job)
        if data.get("confirm_save") is not True:
            raise PermissionError("请确认保存本地可编辑草稿。")
        if runner.state.get("status") not in {"ready", "qa_blocked", "exported"}:
            raise ValueError("请先生成可用预览，再保存草稿。")
        if runner.state.get("qa_issues") and data.get("acknowledge_qa") is not True:
            raise PermissionError("请先确认已知质检提示；保存草稿不代表可以直接发布。")
        plan = json.loads((runner.result_dir / "execution_plan.json").read_text(encoding="utf-8"))
        rendered_plan = json.loads(json.dumps(plan))
        mode = data.get("emphasis_mode", "text" if data.get("editable_emphasis") is True else "layered")
        if mode not in {"layered","appearance","text"}:
            raise ValueError("请选择有效的花字导出方式。")
        if mode == "text":
            plan = editable_emphasis_plan(plan)
        elif mode == "layered":
            registry_path = workspace / "templates/jianying-emphasis-bindings.json"
            registry = json.loads(registry_path.read_text("utf-8")) if registry_path.is_file() else None
            plan = layered_emphasis_plan(plan,runner.root,runner.result_dir/"draft-assets/emphasis",registry=registry,
                                         editor_version=os.environ.get("SUOJIAN_JIANYING_VERSION"))
        plan = prepare_export_plan(plan)
        native_root = Path(os.environ.get("SUOJIAN_JIANYING_DRAFT_ROOT", str(
            Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft")))
        if not native_root.is_dir():
            raise FileNotFoundError("未找到剪映本地草稿库。请先启动剪映，或配置 SUOJIAN_JIANYING_DRAFT_ROOT。")
        if not EXPORT_LOCK.acquire(blocking=False):
            raise ValueError("正在保存草稿，请勿重复点击。")
        try:
            suffix = "分层花字-" if mode == "layered" else "可改字-" if mode == "text" else ""
            name = "溯剪-" + job + "-" + suffix + digest(plan)[:8]
            output, report = build_execution_draft(plan, native_root, name)
            runner.save(status="exported", phase="已写入剪映草稿库", draft_path=str(output), export_report=report)
            from suo_jian.production_trace import safe_record, trace_feed
            runner.save(production_trace=safe_record(workspace, trace_feed, job, runner.state, rendered_plan,
                draft_observation={"observer":"unified_export/build_execution_draft returned", "report":report}))
            return jsonify(public(runner.state))
        finally:
            EXPORT_LOCK.release()
