"""SuoJian-owned orchestration. A durable record is not a human review record."""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from suo_jian.blueprint.templates import TemplateStore, digest, write_json
from suo_jian.script_parser.semantic_splitter import semantic_split, visible_chars
from .client import call_local_adapter
from .execution_plan import load_bound_plan, file_signature
from .packaging_options import validate_packaging_choices
from .reference_policy import build_reference_policy
from .installation import execution_root

START_LOCK = threading.Lock()
COLLECT_LOCK = threading.Lock()
ACTIVE: set[str] = set()


def match_subtitle_preset(recipe: dict, presets: list[dict]) -> dict | None:
    styles = recipe.get("styles") or {}
    style = styles.get("subtitle_style_01") or next(iter(styles.values()), {})
    fill = style.get("fill", "#FFFFFF")
    def rgb(value):
        value = str(value).lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4)) if re.fullmatch(r"[0-9A-Fa-f]{6}", value) else (255, 255, 255)
    target = rgb(fill)
    return min(presets, key=lambda p: sum((a - b) ** 2 for a, b in zip(target, rgb(p.get("primary_hex"))))) if presets else None


def read_state(result_dir: Path) -> dict:
    path = result_dir / "execution.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_started"}


def plan_storyboard(script: str, materials: list[dict], ratio: str, recipe: dict) -> dict:
    # No unverified provider credentials or external generation here. Filename-based
    # selection is a transparent baseline, not visual understanding.
    candidates = [m for m in materials if m.get("mediaKind") == "video" and m.get("relativePath")]
    oriented = [m for m in candidates if any(s in m["relativePath"] for s in
                (("横", "16-9") if ratio == "16:9" else ("竖", "9-16")))]
    candidates = oriented or candidates
    if not candidates:
        raise ValueError("所选素材库中没有可用视频。请先补充素材。")
    candidates = sorted(candidates, key=lambda m: m["relativePath"])
    groups = [(('层高', '承重', '行车', '一楼', '分割', '车间'), ('内部', '室内', '一楼')),
              (('园区', '国资', '项目'), ('外部', '航拍', '俯拍', '整体')),
              (('考察', '预约', '链接'), ('大门', '门口', '公路'))]
    used, segments = {}, []
    shots = recipe.get("shots") or []
    for index, text in enumerate(semantic_split(script, target_chars=10 if shots else 14, max_chars=15 if shots else 24)):
        terms = [word for triggers, words in groups if any(t in text for t in triggers) for word in words]
        shot = shots[index % len(shots)] if shots else {}
        wanted = shot.get('motion', 'unknown')
        if isinstance(wanted, dict):
            wanted = wanted.get('type', 'unknown')
        def score(m):
            from suo_jian.analyzer.motion_planning import motion_match
            path = m["relativePath"]
            content = sum(6 for term in terms if term in path) + len(set(text) & set(Path(path).stem))
            motion = motion_match(wanted, m.get('motion') or {})[0]
            return (content, motion, -used.get(m['assetId'], 0))
        asset = max(candidates, key=score)
        used[asset["assetId"]] = used.get(asset["assetId"], 0) + 1
        shot = shots[index % len(shots)] if shots else {}
        segments.append({"text": text, "type": "video", "query": asset["relativePath"],
                         "prompt": f"使用本园区真实素材；不凭画面推断商业参数。参考镜头建议：{shot.get('motion', '自然构图')}。"})
    compact = lambda text: re.sub(r"\s+", "", text)
    if compact("".join(s["text"] for s in segments)) != compact(script):
        raise ValueError("分镜未完整覆盖脚本，已停止生成。")
    return {"plan": {"ratio": ratio, "segments": segments}}


class UnifiedExecution:
    def __init__(self, root: Path, result_dir: Path, store: TemplateStore, *, invoke=None):
        self.root, self.result_dir, self.store = Path(root), Path(result_dir), store
        self.invoke = invoke or (lambda operation, args: call_local_adapter(self.root, operation, args))
        self.state = read_state(result_dir)
        self.root = execution_root(self.root, self.result_dir, self.state)

    def save(self, **values):
        self.state.update(values)
        self.state["cutroom_root"] = str(self.root.resolve())
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json(self.result_dir / "execution.json", self.state)

    def call(self, operation, **args):
        if self.state.get("task_id"):
            args.setdefault("task_id", self.state["task_id"])
        return self.invoke(operation, args)

    def start(self, config: dict):
        for field in ("confirm_cost", "owns_materials", "delegate_routine"):
            if config.get(field) is not True:
                raise PermissionError("请确认配音调用费用、素材使用权以及自动分镜与包装委托。")
        script = str(config.get("script", "")).strip()
        if not script or len(script) > 5000:
            raise ValueError("请填写 1～5000 字的完整新脚本。")
        template = self.store.get(config["template_id"])
        catalog = self.call("catalog")
        if catalog.get("adapter", {}).get("automatic_pipeline_supported") is False:
            raise ValueError("当前 Cutroom 新版需要在其工作台确认文案、画面和包装；旧自动制作流程尚未适配完成，不会创建任务或发起配音费用。")
        if config.get("library_id") not in {m["library_id"] for m in catalog["libraries"] if m.get("online")}:
            raise ValueError("请从已连接的 Cutroom 素材库中选择园区。")
        if config.get("tts_profile_id") not in {p["profileId"] for p in catalog["tts_profiles"] if p.get("enabled")}:
            raise ValueError("请选择可用的 Cutroom 配音配置。")
        profile = next(p for p in catalog["tts_profiles"] if p["profileId"] == config["tts_profile_id"])
        voice = str(config.get("tts_voice_id") or "").strip()
        if not voice or voice not in {v["voiceId"] for v in profile.get("voices", []) if v.get("selectable")}:
            raise ValueError("请选择当前配音服务目录中的可用音色。")
        safe = {k: config.get(k) for k in ("template_id", "script", "library_id", "tts_profile_id",
                                          "tts_voice_id", "confirm_cost", "owns_materials", "delegate_routine")}
        safe["tts_voice_id"] = voice
        safe["script"] = script
        if template.get("reference_policy"):
            safe["reference_policy"] = json.loads(json.dumps(template["reference_policy"]))
        elif config.get("match_reference") is True:
            safe["reference_policy"] = build_reference_policy(template["recipe"], self.result_dir.parent.parent / "uploads" / Path(template["recipe"]["source"]["file_name"]).name)
        if template.get("packaging_choices") is not None:
            safe["packaging_choices"] = validate_packaging_choices(template["packaging_choices"], catalog.get("packaging_items", {}))
        if config.get("packaging_choices") is not None:
            safe["packaging_choices"] = validate_packaging_choices(config["packaging_choices"], catalog.get("packaging_items", {}))
        policy = safe.get("reference_policy")
        music = policy.get("music_match") if policy else None
        if music and safe.get("packaging_choices", {}).get("bgm") != music.get("selected", {}).get("id"):
            policy.pop("music_match", None)
        safe["packaging_template_id"] = template.get("cutroom_packaging_template_id")
        safe["auto_bgm"] = config.get("auto_bgm") is True and not safe["packaging_template_id"]
        key = str(self.result_dir.resolve())
        with START_LOCK:
            self.state = read_state(self.result_dir)
            if key in ACTIVE:
                raise ValueError("已有生成任务正在执行，请勿重复提交。")
            if self.state.get("config_digest") == digest(safe) and self.state.get("status") in {"ready", "qa_blocked", "exported"}:
                return self.state
            if self.state.get("status") not in {None, "not_started", "ready", "qa_blocked", "exported"}:
                if not (self.state.get("status") == "failed" and not self.state.get("task_id") and not self.state.get("creation_pending")):
                    raise ValueError("已有未完成任务。请先恢复；为避免重复付费，不会自动创建新任务。")
            if self.state.get("task_id"):
                history = self.result_dir / "execution_history" / self.state["task_id"]
                write_json(history / "execution.json", self.state)
                for filename in ("execution_plan.json", "generation_blueprint.json"):
                    path = self.result_dir / filename
                    if path.exists():
                        write_json(history / filename, json.loads(path.read_text(encoding="utf-8")))
            self.state = {"schema_version": "suojian-cutroom-execution-1", "config": safe,
                          "config_digest": digest(safe), "status": "running", "phase": "准备创建任务",
                          "routine_policy": "自动选镜、锁定画面、选择包装；不代替最终人工审片",
                          "human_reviewed": False, "warnings": ["自动选镜当前依据素材文件名，尚非画面语义识别。"]}
            self.save()
            ACTIVE.add(key)
        threading.Thread(target=self._run_guarded, daemon=True).start()
        return self.state

    def resume(self):
        key = str(self.result_dir.resolve())
        with START_LOCK:
            self.state = read_state(self.result_dir)
            if key in ACTIVE:
                return self.state
            if self.state.get("status") not in {"failed", "running", "waiting_worker"}:
                raise ValueError("该任务无需恢复，或供应商结果不明确，需要先核对。")
            if not self.state.get("task_id"):
                raise ValueError("任务创建结果不明确，请先核对 Cutroom 任务列表，避免重复创建或付费。")
            ACTIVE.add(key)
            self.save(status="running", error=None)
        threading.Thread(target=self._run_guarded, daemon=True).start()
        return self.state

    def repackage(self, config):
        key = str(self.result_dir.resolve())
        with START_LOCK:
            self.state = read_state(self.result_dir)
            if key in ACTIVE or self.state.get("status") in {"running", "waiting_worker"}:
                raise ValueError("已有任务正在执行，请等待完成。")
            if not self.state.get("task_id") or not self.state.get("preview_path"):
                raise ValueError("请先生成一次预览，再调整包装。")
            retrying_preview = self.state.get("status") == "failed" and self.state.get("mode") == "repackage"
            if self.state.get("status") not in {"ready", "qa_blocked", "exported"} and not retrying_preview:
                raise ValueError("请先恢复或核对当前任务，再修改包装。")
            options = self.call("packaging_options")
            choices = validate_packaging_choices(config.get("choices"), options["catalog"])
            reference_policy = self.state.get("config", {}).get("reference_policy")
            previous_policy = reference_policy
            if config.get("match_reference") is True:
                template = self.store.get(self.state["config"]["template_id"])
                reference_policy = build_reference_policy(template["recipe"], self.result_dir.parent.parent / "uploads" / Path(template["recipe"]["source"]["file_name"]).name)
                if previous_policy and previous_policy.get("music_match"):
                    reference_policy["music_match"] = previous_policy["music_match"]
            if choices == options["current"]["choices"] and config.get("refresh_preview") is not True:
                return self.state
            if config.get("expected_plan_id") != options["current"]["plan_id"]:
                raise ValueError("包装已被更新，请重新读取选项后再修改。")
            revision = uuid.uuid4().hex[:12]
            history = self.result_dir / "preview_history" / revision
            write_json(history / "execution.json", self.state)
            for filename in ("execution_plan.json", "generation_blueprint.json"):
                path = self.result_dir / filename
                if path.is_file():
                    write_json(history / filename, json.loads(path.read_text(encoding="utf-8")))
            self.save(mode="repackage", status="running", phase="正在更新包装，复用现有配音",
                      packaging_revision=revision, packaging_request={"choices": choices,
                      "expected_plan_id": options["current"]["plan_id"],
                      "refresh_preview": config.get("refresh_preview") is True,
                      "reference_policy": reference_policy, "auto_bgm":config.get("auto_bgm") is True}, packaging_applied=None,
                      **{"更新包装预览_job": None}, error=None, draft_path=None, export_report=None,
                      human_reviewed=False)
            ACTIVE.add(key)
        threading.Thread(target=self._run_guarded, daemon=True).start()
        return self.state

    def run_repackage(self):
        revision = self.state.get("packaging_revision")
        if not self.state.get("packaging_applied"):
            applied = self.call("repackage", **self.state["packaging_request"])
            self.save(packaging_applied=applied)
        config = {**self.state.get("config", {}), "packaging_choices": self.state["packaging_applied"]["choices"]}
        if self.state["packaging_applied"].get("reference_policy"):
            config["reference_policy"] = self.state["packaging_applied"]["reference_policy"]
        elif self.state["packaging_request"].get("reference_policy"):
            config["reference_policy"] = self.state["packaging_request"]["reference_policy"]
        self.save(config=config, config_digest=digest(config))
        self._wait_job("更新包装预览", "render_and_qa")
        if self.state.get("packaging_revision") != revision:
            return
        self.finish_repackage()

    def finish_repackage(self):
        expected_job = self.state.get("更新包装预览_job")
        with COLLECT_LOCK:
            latest = read_state(self.result_dir)
            if latest.get("更新包装预览_job") != expected_job:
                return self.state
            self.state = latest
            if self.state.get("status") not in {"running", "waiting_worker"}:
                return self.state
            return self.collect()

    def refresh_completed_repackage(self):
        # A browser refresh can reconcile a finished local render even when the
        # original polling thread was interrupted. This never starts another job.
        job_id = self.state.get("更新包装预览_job")
        if self.state.get("mode") == "repackage" and self.state.get("status") in {"running", "waiting_worker"} and job_id:
            if self.call("job", job_id=job_id)["status"] == "completed":
                self.finish_repackage()
        return self.state

    def _wait_job(self, stage, action, **args):
        job_key = stage + "_job"
        if not self.state.get(job_key):
            # Durable intent + deterministic paid request ID. If the call times out,
            # resume repeats the SAME request, never a new provider request.
            self.save(phase=stage)
            identity = (self.state.get("packaging_revision") if self.state.get("mode") == "repackage" else None) or self.state.get("config_digest") or digest(self.state.get("config", {}))
            result = self.call("start", action=action, request_id=identity + stage, **args)
            self.save(**{job_key: result["jobId"]})
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            job = self.call("job", job_id=self.state[job_key])
            if self.state.get("mode") == "repackage":
                latest = read_state(self.result_dir)
                if latest.get("packaging_revision") != self.state.get("packaging_revision") or latest.get("status") not in {"running", "waiting_worker"}:
                    self.state = latest
                    return
            self.save(phase=job.get("phase") or stage, backend_progress=job.get("progress"),
                      queue_position=job.get("queuePosition"), status="running")
            if job["status"] == "completed":
                return
            if job.get("needsManualCheck") or job["status"] in {"failed", "cancelled", "interrupted", "paused"}:
                self.save(status="needs_check" if job.get("needsManualCheck") else "failed")
                raise RuntimeError("Cutroom 执行未完成；请核对任务。不会自动重复收费。")
            if job["status"] == "queued":
                self.save(status="waiting_worker", phase="已排队，等待 Cutroom 工作进程")
            time.sleep(2)
        raise TimeoutError("等待 Cutroom 超时。任务记录已保留，可恢复；请检查 Cutroom 工作进程。")

    def _run_guarded(self):
        try:
            if self.state.get("mode") == "repackage":
                self.run_repackage()
            else:
                self.run()
        except Exception as exc:
            self.save(status="needs_check" if self.state.get("status") == "needs_check" else "failed", error=str(exc))
        finally:
            with START_LOCK:
                ACTIVE.discard(str(self.result_dir.resolve()))

    def run(self):
        config = self.state["config"]
        template = self.store.get(config["template_id"])
        recipe = template["recipe"]
        source = recipe.get("source") or {}
        ratio = "16:9" if source.get("width", 1080) > source.get("height", 1920) else "9:16"
        if not self.state.get("task_id"):
            self.save(phase="检查配音配置、素材和运行条件", creation_pending=False)
            request = {"route": "ad_mix", "orchestration_mode": "agent", "title": "溯剪-" + template["name"],
                       "purpose": "按参考剪法制作园区信息流；本地可编辑草稿，不自动发布",
                       "final_script": config["script"], "aspect_ratio": ratio, "voice_mode": "ai_tts",
                       "target_duration_seconds": max(10, min(180, visible_chars(config["script"]) / 5)),
                       "primary_library_id": config["library_id"], "selected_generic_ids": [],
                       "include_default_generic": True, "model_profile_ids": {"tts": config["tts_profile_id"]}}
            if config.get("tts_voice_id"):
                request["tts_voice_id"] = config["tts_voice_id"]
            preflight = self.call("preflight", request=request)
            expired_tts = any(item.get("code") == "MODEL_PROFILE_UNAVAILABLE" and
                              any(word in str(item.get("basis", "")) for word in ("过期", "尚未通过"))
                              for item in preflight.get("blocking", []))
            if expired_tts:
                self.save(phase="真实检测配音服务（可能产生少量费用）", probe_attempted=True)
                tested = self.call("probe_tts", profile_id=config["tts_profile_id"], confirm_cost=config["confirm_cost"])
                self.save(tts_verification=tested)
                preflight = self.call("preflight", request=request)
            if preflight.get("blocking"):
                raise ValueError("创建前检查未通过：" + "；".join(str(item.get("message", item)) for item in preflight["blocking"]))
            self.save(phase="创建 Cutroom 任务", creation_pending=True, preflight=preflight)
            result = self.call("create", request=request)
            self.save(task_id=result["taskId"], creation_pending=False)
        snapshot = self.call("inspect")
        gates = snapshot["workflow"]["gates"]
        if not gates.get("brief_locked"):
            self.save(phase="锁定本次脚本")
            self.call("lock_brief")
        if not any(s["stage_key"] == "material_sync" and s["status"] == "completed" for s in snapshot["workflow"]["stages"]):
            self.save(phase="检查素材来源")
            preview = self.call("sync_preview")
            if preview.get("impact_token"):
                self._wait_job("同步素材", "sync_sources", payload={"impact_token": preview["impact_token"]})
            else:
                raise RuntimeError("素材同步未完成且没有可用同步令牌，请检查 Cutroom。")
        if self.state.get("配音与分镜_job"):
            self._wait_job("配音与分镜", "recommend_storyboard", confirm_cost=True)
            snapshot = self.call("inspect")
        if not snapshot["cards"]:
            context = self.call("context")
            content = plan_storyboard(context["script"]["text"], context["materials"], ratio, recipe)
            self.save(phase="提交分镜规划", storyboard=content)
            write_json(self.result_dir / "generation_blueprint.json", {
                "schema_version": "draft-generation-1", "template_id": config["template_id"],
                "reference_analysis_digest": template["analysis_digest"], "script": config["script"],
                "status": "planned", "timeline": content["plan"]["segments"],
                "timing_policy": "以真实 TTS 时长为准；参考片时长不作为硬性上限"})
            self.call("submit", input_digest=context["inputDigest"], content=content)
            self._wait_job("配音与分镜", "recommend_storyboard", confirm_cost=True)
            snapshot = self.call("inspect")
            if not snapshot["cards"]:
                raise RuntimeError("Cutroom 未生成可用分镜，已停止后续操作。")
        if not snapshot["workflow"]["gates"].get("picture_lock"):
            self.save(phase="核验镜头并自动锁定画面")
            self.call("lock_picture", owns_materials=config["owns_materials"])
        if not snapshot["workflow"]["gates"].get("packaging_confirmed"):
            self.save(phase="应用包装组合")
            match = match_subtitle_preset(recipe, self.call("catalog").get("subtitle_presets", []))
            self.call("packaging", template_id=config.get("packaging_template_id"), subtitle_id=match["id"] if match else None,
                      choices=config.get("packaging_choices"))
            self.save(style_mapping="复用已保存的 Cutroom 包装组合" if config.get("packaging_template_id") else
                      ("参考字幕颜色近似映射：" + match["name"] if match else "采用 Cutroom 默认字幕组合"))
        if not self.call("inspect").get("candidates"):
            if config.get("reference_policy"):
                bound = self.call("reference_bind", policy=config["reference_policy"], auto_bgm=config.get("auto_bgm") is True)
                if isinstance(bound,dict) and bound.get("policy"):
                    config = {**config,"reference_policy":bound["policy"],"packaging_choices":bound["choices"]}
                    self.save(config=config)
            self._wait_job("渲染预览与质检", "render_and_qa")
        self.collect()

    def collect(self):
        snapshot = self.call("inspect")
        candidates = snapshot.get("candidates") or []
        if not candidates or not snapshot.get("render_plan_path"):
            raise ValueError("当前任务还没有可用预览及对应渲染方案。")
        candidate = max(candidates, key=lambda c: c["version_no"])
        preview = Path(candidate["output_path"]).resolve()
        preview.relative_to((self.root / "out" / self.state["task_id"]).resolve())
        if not preview.is_file():
            raise FileNotFoundError("Cutroom 预览文件缺失。")
        if file_signature(preview) != candidate.get("output_signature"):
            raise ValueError("候选预览文件已变化或损坏，不能当作已生成的版本复用。")
        plan = load_bound_plan(Path(snapshot["render_plan_path"]), self.root, self.state["task_id"], candidate,
                               self.state.get("config", {}).get("template_id"))
        qa = candidate.get("qa_report") or snapshot.get("qa") or {}
        # A local editable draft is not a publish package. Retain QA blockers;
        # never forge qa_passed, full_review or a publication approval.
        if not qa.get("video_spec"):
            raise ValueError("成片解码质检未通过，不能复用该预览。")
        if (qa["video_spec"].get("width"), qa["video_spec"].get("height")) != (plan["canvas"]["width"], plan["canvas"]["height"]):
            raise ValueError("预览画幅与执行方案不一致。")
        plan["source_signatures"] = {row["source"]: file_signature(Path(row["source"]))
                                     for key in ("clips", "audio", "overlays") for row in plan[key]}
        plan["preview_signature"] = candidate["output_signature"]
        write_json(self.result_dir / "execution_plan.json", plan)
        write_json(self.result_dir / "generation_blueprint.json", {
            "schema_version": "draft-generation-1", "template_id": plan.get("template_id"),
            "script": self.state.get("config", {}).get("script", ""), "status": "rendered",
            "canvas": plan["canvas"], "timeline": plan["clips"], "subtitles": plan["texts"],
            "audio": plan["audio"], "overlays": plan["overlays"],
            "execution_digest": digest(plan), "human_reviewed": False})
        self.save(status="qa_blocked" if qa.get("blocking_count") else "ready", phase="预览已生成，等待您审看",
                  preview_path=str(preview), candidate_id=candidate["candidate_id"],
                  qa_issues=[i for i in qa.get("issues", []) if i.get("ok") is False],
                  editability={"clips": len(plan["clips"]), "texts": len(plan["texts"]),
                               "audio": [a["kind"] for a in plan["audio"]], "overlays": len(plan["overlays"])},
                  limitations=plan["limitations"], reference_match=plan.get("reference_match"), music_match=plan.get("music_match"), emphasis_report=plan.get("emphasis_report"), error=None)
        from suo_jian.production_trace import safe_record, trace_feed
        self.save(production_trace=safe_record(self.result_dir.parents[1], trace_feed,
                  self.result_dir.name, self.state, plan))
        return self.state
