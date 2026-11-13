"""Local compatibility adapter for domain operations not exposed by 剪辑工作台 MCP.

Executed with 剪辑工作台's interpreter. No source copying, SQL writes, credentials,
provider reconfiguration or manufactured watch/review evidence.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path


def match_task_music(root, task_id, policy):
    from media_catalog.music_matching import recommend_bgm
    timing = json.loads((root/"runtime/workbench/tasks"/task_id/"artifacts/ad/adtiming.json").read_text("utf-8"))
    return recommend_bgm(root, policy["reference_source"], float(timing["total"]),
                         script="".join(row["text"] for row in timing["segments"]))


def packaging_catalog(root, packaging):
    from suo_jian.integrations.cutroom.packaging_options import PACKAGING_SLOTS
    result = {}
    for slot in PACKAGING_SLOTS:
        category = "title_card" if slot == "brand" else slot
        items = []
        for row in packaging.load_manifest(category).get("items", []):
            if row.get("license") != "commercial-approved" or (row.get("default_for") and "信息流广告" not in row["default_for"]):
                continue
            if slot == "brand" and row.get("kind") != "watermark":
                continue
            if slot == "title_card" and row.get("kind") == "watermark":
                continue
            if row.get("file"):
                path = Path(row["file"])
                if not (path if path.is_absolute() else root / path).is_file():
                    continue
            items.append({k: row.get(k) for k in ("id", "name", "primary_hex", "outline_hex", "size", "fontname_guess")})
        result[slot] = items
    return result


def current_packaging(db, task_id, packaging):
    from media_catalog.provenance import latest_gate
    gate = latest_gate(db, task_id, "packaging")
    details = json.loads(gate["details_json"]) if gate else {}
    plan = packaging.get_plan(db, details["plan_id"]) if details.get("plan_id") else None
    if not plan or plan["task_id"] != task_id or plan["status"] != "confirmed":
        raise ValueError("该任务尚未绑定有效包装方案。")
    from suo_jian.integrations.cutroom.packaging_options import PACKAGING_SLOTS
    return {"plan_id": plan["plan_id"], "name": plan["plan_name"],
            "choices": {slot: next((item["asset_ref"] for item in plan["items"]
                                   if item["slot"] == slot and not item["is_backup"]), None)
                        for slot in PACKAGING_SLOTS}}


def unwrap(value):
    if not value.get("ok"):
        raise RuntimeError(json.dumps(value.get("error"), ensure_ascii=False))
    return value["data"]


def public_tts_profiles(db):
    from media_catalog.model_profiles import list_model_profiles
    from media_catalog.tts_voice_catalog import list_provider_voices, voice_catalog_state
    profiles = []
    for row in list_model_profiles(db, capability="tts"):
        profile = {k: row.get(k) for k in ("profileId", "displayName", "provider", "model",
                                          "enabled", "isDefault", "availabilityStatus")}
        settings = row.get("settings") or {}
        resource = settings.get("resource_id") or row["model"]
        default = settings.get("default_voice_id") or settings.get("voice") or ""
        profile["defaultVoiceId"] = default
        if row["provider"] == "doubao_tts":
            voices = list_provider_voices(db, row["accountId"], resource, selectable_only=True)
            profile["voices"] = [{k: voice.get(k) for k in ("voiceId", "displayName", "gender",
                                   "languages", "styles", "emotions", "selectable")}
                                 for voice in voices]
            state = voice_catalog_state(db, row["accountId"], resource)
            profile["voiceCatalog"] = {k: state.get(k) for k in ("status", "lastSuccessAt", "lastErrorMessage")}
        else:
            profile["voices"] = ([{"voiceId": default, "displayName": settings.get("voice_name") or default,
                                   "selectable": True}] if default else [])
        profiles.append(profile)
    return profiles


def dispatch(root, db, op, args):
    if op == 'model_settings':
        from workbench.api import ApiError, WorkbenchAPI
        method = args.get('method')
        path = args.get('path', '')
        permitted = (
            method == 'GET' and path == '/api/settings/model-profiles'
            or method == 'GET' and re.fullmatch(r'/api/settings/provider-accounts/[A-Za-z0-9_.-]+/voices', path)
            or method == 'POST' and path in ('/api/settings/provider-accounts', '/api/settings/model-profiles')
            or method == 'POST' and re.fullmatch(r'/api/settings/model-profiles/[A-Za-z0-9_.-]+/default', path)
            or method == 'POST' and re.fullmatch(r'/api/settings/provider-accounts/[A-Za-z0-9_.-]+/voices/refresh', path)
        )
        if not permitted:
            raise ValueError('不支持此模型设置操作')
        try:
            return WorkbenchAPI(db, root).dispatch(method, path, args.get('body') or {}).payload
        except ApiError as exc:
            return {'settingsError': exc.message}
    from media_catalog.agent_gateway import AgentGateway
    from media_catalog import packaging_plan as packaging
    gateway = AgentGateway(db, project_root=root)
    if op == "catalog":
        from media_catalog.libraries import list_libraries
        from media_catalog.model_profiles import list_model_profiles
        return {
            "libraries": [{k: row.get(k) for k in ("library_id", "purpose", "name", "online", "asset_count")}
                          for row in list_libraries(db)],
            "tts_profiles": public_tts_profiles(db),
            "packaging_items": packaging_catalog(root, packaging),
            "packaging_templates": [{k: row.get(k) for k in ("template_id", "template_name")}
                                    for row in packaging.list_personal_templates(db, "信息流广告")],
            "subtitle_presets": [{k: row.get(k) for k in ("id", "name", "primary_hex", "size", "fontname_guess")}
                                 for row in packaging.load_manifest("subtitle").get("items", [])
                                 if row.get("license") == "commercial-approved" and
                                 (not row.get("default_for") or "信息流广告" in row["default_for"])],
        }
    if op == "create":
        return unwrap(gateway.create_task(args["request"]))
    if op == "preflight":
        return unwrap(gateway.preflight_task(args["request"]))
    if op == "probe_tts":
        if args.get("confirm_cost") is not True:
            raise PermissionError("配音连接检测也可能产生少量费用，需明确同意。")
        from media_catalog.model_profile_probe import probe_model_profile
        from media_catalog.model_profiles import get_model_profile
        profile = get_model_profile(db, args["profile_id"])
        if not profile or profile["capability"] != "tts":
            raise ValueError("只能检测已配置的配音服务。")
        tested = probe_model_profile(db, args["profile_id"])
        return {k: tested.get(k) for k in ("profileId", "availabilityStatus", "lastCheckedAt", "verificationLevel")}
    task_id = str(args.get("task_id", ""))
    if not re.fullmatch(r"task_[0-9a-f]{32}", task_id):
        raise ValueError("无效 剪辑工作台 任务编号。")
    unwrap(gateway.get_task(task_id))
    if op == "packaging_options":
        return {"catalog": packaging_catalog(root, packaging), "current": current_packaging(db, task_id, packaging)}
    if op == "reference_bind":
        from media_catalog.reference_editing import bind_reference_policy
        current = current_packaging(db, task_id, packaging)
        policy = dict(args["policy"])
        if args.get("auto_bgm") is True:
            policy["music_match"] = match_task_music(root, task_id, policy)
            plan_id = packaging.clone_plan(db, current["plan_id"], task_id=task_id, plan_name="溯剪 · 参考音乐匹配")
            packaging.replace_item(db, plan_id, "bgm", policy["music_match"]["selected"]["id"])
            packaging.confirm_plan(db, plan_id, evidence={"source":"suojian_auto_music_selection","human_watched":False,"reason":"用户启用自动参考音乐匹配，最终效果待审片"})
            current = current_packaging(db, task_id, packaging)
        return {"path": bind_reference_policy(root, task_id, current["plan_id"], policy), "policy":policy, "choices":current["choices"]}
    if op == "repackage":
        from suo_jian.integrations.cutroom.packaging_options import validate_packaging_choices
        choices = validate_packaging_choices(args.get("choices"), packaging_catalog(root, packaging))
        policy = dict(args.get("reference_policy") or {})
        if args.get("auto_bgm") is True:
            if not policy: raise ValueError("请先启用参考匹配，再匹配背景音乐。")
            policy["music_match"] = match_task_music(root, task_id, policy)
            choices["bgm"] = policy["music_match"]["selected"]["id"]
        elif policy.get("music_match") and choices.get("bgm") != policy["music_match"]["selected"]["id"]:
            policy.pop("music_match")
        current = current_packaging(db, task_id, packaging)
        if current["choices"] == choices and args.get("refresh_preview") is not True:
            return {**current, "reused": True}
        if args.get("expected_plan_id") != current["plan_id"]:
            raise ValueError("剪辑工作台 包装刚被其他操作更新，请重新读取后再修改。")
        plan_id = packaging.clone_plan(db, current["plan_id"], task_id=task_id, plan_name="溯剪 · 自选包装")
        for slot, value in choices.items():
            if value is None:
                packaging.disable_optional_slot(db, plan_id, slot)
            else:
                packaging.replace_item(db, plan_id, slot, value)
        packaging.confirm_plan(db, plan_id, evidence={"source": "suojian_user_packaging_selection",
                               "human_watched": False, "reason": "用户选择包装并重新生成预览，仍待审片"})
        if policy:
            from media_catalog.reference_editing import bind_reference_policy
            bind_reference_policy(root, task_id, plan_id, policy)
        return {"plan_id": plan_id, "choices": choices, "reference_policy":policy}
    if op == "inspect":
        from media_catalog.scene_cards import list_scene_cards
        from media_catalog.snapshots import latest_review_candidate
        from media_catalog.candidate_versions import list_candidates
        from media_catalog.qa import list_reports
        candidates = list_candidates(db, task_id)
        from suo_jian.integrations.cutroom.execution_plan import candidate_render_path
        render_plan = candidate_render_path(root, task_id, candidates)
        reports = list_reports(db, task_id)
        report = db.connection.execute("SELECT report_json FROM qa_reports WHERE report_id=? AND task_id=?",
                                       (reports[0]["report_id"], task_id)).fetchone() if reports else None
        return {**unwrap(gateway.get_task(task_id)), "plans": packaging.list_plans(db, task_id),
                "cards": list_scene_cards(db, task_id), "candidate": latest_review_candidate(db, task_id),
                "candidates": candidates, "qa": json.loads(report[0]) if report else None,
                "render_plan_path": str(render_plan) if render_plan else None}
    if op == "lock_brief":
        from media_catalog.tasks import set_status
        set_status(db, task_id, "brief_locked")
        return {"task_id": task_id}
    if op == "sync_preview":
        from media_catalog.task_sources import preview_source_sync
        return preview_source_sync(db, task_id)
    if op == "context":
        return unwrap(gateway.get_agent_context(task_id))
    if op == "submit":
        return unwrap(gateway.submit_planning_artifact(
            task_id=task_id, artifact_type="ad_storyboard", schema_version=1,
            input_digest=args["input_digest"], content=args["content"], source_agent="suojian"))
    if op == "start":
        return unwrap(gateway.start_task_action(task_id=task_id, action=args["action"],
                      request_id=args.get("request_id"), confirm_cost=args.get("confirm_cost") is True,
                      payload=args.get("payload")))
    if op == "job":
        return unwrap(gateway.get_job(args["job_id"]))
    if op == "lock_picture":
        from media_catalog.scene_cards import list_scene_cards, confirm_scene_card, set_picture_lock
        from media_catalog.rough_media import assert_picture_media_available, describe_rough_media
        from media_catalog.material_authorization import confirm_material_authorization
        cards = list_scene_cards(db, task_id)
        if not cards:
            raise ValueError("没有可锁定的分镜。")
        versions = {c["card_id"]: describe_rough_media(db, task_id, c["asset_id"])["preview_version"] for c in cards}
        assert_picture_media_available(db, task_id, expected_versions=versions)
        for c in cards:
            confirm_scene_card(db, c["card_id"], "溯剪按用户委托自动选镜；尚未人工审片")
        result = set_picture_lock(db, task_id)
        if args.get("owns_materials") is not True:
            raise PermissionError("请确认所选素材的使用权。")
        confirm_material_authorization(db, task_id, confirmed_by="溯剪：用户在生成配置中确认素材使用权")
        return result
    if op == "packaging":
        template = args.get("template_id")
        plan_id = (packaging.clone_plan(db, template, task_id=task_id) if template else
                   packaging.clone_plan(db, packaging.propose(db, task_id, "信息流广告")[0], task_id=task_id))
        if not template and args.get("subtitle_id"):
            packaging.replace_item(db, plan_id, "subtitle", args["subtitle_id"],
                                   notes="溯剪按参考字幕颜色映射到现有目录；近似样式，非原模板识别")
        if args.get("choices") is not None:
            from suo_jian.integrations.cutroom.packaging_options import validate_packaging_choices
            choices = validate_packaging_choices(args["choices"], packaging_catalog(root, packaging))
            for slot, value in choices.items():
                if value is None:
                    packaging.disable_optional_slot(db, plan_id, slot)
                else:
                    packaging.replace_item(db, plan_id, slot, value)
        return packaging.confirm_plan(db, plan_id, evidence={
            "source": "suojian_delegated_policy", "human_watched": False,
            "reason": "用户委托自动确认常规包装，最终预览仍需用户审片"})
    if op == "save_packaging":
        plan = packaging.get_plan(db, args["plan_id"])
        if not plan or plan["task_id"] != task_id:
            raise ValueError("包装方案不属于本次任务。")
        policy_path = root / "runtime/workbench/tasks" / task_id / "reference-policies" / (args["plan_id"] + ".json")
        policy = json.loads(policy_path.read_text("utf-8")) if policy_path.is_file() else None
        saved = packaging.save_personal_template(db, args["plan_id"], args["name"], reference_policy=policy)
        from suo_jian.integrations.cutroom.packaging_options import PACKAGING_SLOTS
        choices = {slot:next((item["asset_ref"] for item in plan["items"] if item["slot"]==slot and not item["is_backup"]),None) for slot in PACKAGING_SLOTS}
        return {**{k: saved[k] for k in ("template_id", "template_name", "reference_policy")}, "choices":choices}
    raise ValueError("未开放的本地适配操作。")


def main():
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from media_catalog.database import CatalogDatabase
    args = json.loads(sys.stdin.read() or "{}")
    # Imported libraries may log; stdout is reserved for the structured response.
    with contextlib.redirect_stdout(sys.stderr):
        with CatalogDatabase.open(root / "runtime" / "workbench" / "catalog.sqlite") as db:
            result = dispatch(root, db, sys.argv[2], args)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"bridge_error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
