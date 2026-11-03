"""P09: bounded product context and immutable, evidence-backed model drafts."""
from __future__ import annotations
import hashlib
import json
import math
import re
from pathlib import Path
import uuid

from . import packaging_plan as plans
from .packaging_instances import _validate_instance, _insert, DEFAULT_POSITION
from .tasks import get_task
from .task_script import read_task_script
from .model_contracts import ModelContext, validate_envelope, validate_result
from .timeline_contract import TimeWindow

METHOD = "packaging-context-v7-refined-excerpts"
KIND_SLOT = {"subtitle": "subtitle", "bgm": "bgm", "opening": "title_card",
             "info": "info_card", "sfx": "sfx", "sticker": "sticker",
             "fancy_text": "fancy_text", "brand": "brand"}
STYLES = {"business": {"subtitle", "bgm", "title_card", "info_card"},
          "focus": set(KIND_SLOT.values()) - {"brand"}}

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def record(ref, kind, text, version):
    return {"ref": ref, "kind": kind, "text": text, "version": version,
            "media_uri": None, "orientation": None, "duration_us": None, "source_window": None}

def available_assets(route, db=None):
    result = []
    for slot in KIND_SLOT.values():
        manifest = plans.load_manifest(plans.SLOT_MANIFEST[slot].removesuffix(".json"), db)
        for item in manifest.get("items", []):
            if (slot == "brand" and item.get("kind") != "watermark") or (slot in {"title_card", "info_card"} and item.get("kind") == "watermark"):
                continue
            if not plans._item_is_recommendable(item, route, slot):
                continue
            source = plans._packaging_path(item, None)
            version = digest(item)
            source_sha = None
            if source is not None:
                if not source.is_file():
                    continue
                from .packaging_audio_evidence import file_sha256
                source_sha = item.get("source_sha256") if item.get("_local_root") else file_sha256(source)
                version = digest([version, source_sha])
            result.append({"ref": item["id"], "slot": slot,
                "name": item.get("name", item["id"]), "mood": item.get("mood"),
                "description": item.get("description", item.get("note")),
                "version": version, "source_sha256": source_sha, "evidenceLevel": "catalog_metadata_only",
                "audioAnalysis": "未提供节拍/听感分析，不能声称已试听" if slot in {"bgm", "sfx"} else None})
    return result

def clauses(script):
    # Keep numbers, units and qualification together. Decimal dots are not delimiters.
    return [part.strip() for part in re.split(r"[。！？；\n]+", script) if part.strip()]


def measured_phrases(db, project_root, task_id, script):
    from .packaging_semantics import load_bound_digital_evidence, load_bound_ad_evidence
    result = []
    digital = load_bound_digital_evidence(db, Path(project_root), task_id)
    if digital and digital[0] == script:
        from .speech_alignment import aligned_phrases
        from .transcript_reconciliation import project_transcript_state
        review = project_transcript_state(Path(project_root), get_task(db, task_id), verify_source_content=True)
        if review and review.get("status") not in {"stale", "pending"}:
            result = aligned_phrases(project_root, script, digital[1], review)
    else:
        ad = load_bound_ad_evidence(db, Path(project_root), task_id)
        if ad and ad[0] == script:
            for sentence in ad[2]["segments"]:
                # The finalized TTS segment is the evidence unit, not a second
                # punctuation split of the full script. Its whole interval is known.
                from .packaging_execution_contract import spoken_text
                text = spoken_text(sentence["text"])
                if text and len(text) <= 40 and script.count(text) == 1:
                    result.append({"text": text, "raw_text": sentence["text"], "start": float(sentence["start"]),
                                   "end": float(sentence["end"]), "basis": "measured TTS sentence",
                                   "timing_precision": "sentence"})
    return result


def request_base_plan(request):
    timeline = next(x for x in request["records"] if x["ref"] == "L1")
    return json.loads(timeline["text"]).get("user_choices", {}).get("base_plan_id")


def request_roles(request):
    timeline = next(x for x in request["records"] if x["ref"] == "L1")
    return json.loads(timeline["text"]).get("user_choices", {}).get("requested_roles")


def validate_requested_roles(requested_roles):
    from .packaging_instances import ROLES
    if requested_roles is not None and (not isinstance(requested_roles, list) or len(requested_roles) != 1
            or not isinstance(requested_roles[0], str) or requested_roles[0] not in ROLES):
        raise ValueError("PACKAGING_REQUESTED_ROLES_INVALID: 每次只能追加一种元素类别")
    return requested_roles


def user_choices(db, task_id, base_plan_id, style):
    choices = {"base_plan_id": base_plan_id, "disabled_categories": [],
               "selected_assets": {}, "instances": [], "settings": {}}
    allowed = set(STYLES[style])
    if base_plan_id is None:
        return choices, allowed
    plan = plans.get_plan(db, base_plan_id)
    if not plan or plan["task_id"] != task_id:
        raise ValueError("PACKAGING_PLAN_MISMATCH: 当前包装方案不属于本任务")
    disabled = set()
    for item in plan["items"]:
        if item["is_backup"]:
            continue
        slot = item["slot"]
        if not item.get("enabled", 1):
            disabled.add(slot)
        else:
            choices["selected_assets"].setdefault(slot, []).append(item["asset_ref"])
            allowed.add(slot)
    choices["disabled_categories"] = sorted(disabled)
    choices["settings"] = plans._plan_settings(plan)
    fields = {"instance_id", "origin", "content_layout_json", "source_start_seconds", "fade_in_seconds", "fade_out_seconds", "follow_event", "role", "asset_ref", "content_text", "anchor_kind", "anchor_ref",
              "start_seconds", "end_seconds", "x", "y", "scale", "gain"}
    choices["instances"] = [{k: v for k, v in item.items() if k in fields}
                             for item in plans.active_instances(plan)]
    return choices, allowed - disabled


def prepare_packaging_request(db, project_root, task_id, request_id, style="focus", *, base_plan_id=None, requested_roles=None):
    validate_requested_roles(requested_roles)
    if requested_roles is not None and not base_plan_id:
        raise ValueError("PACKAGING_BASE_PLAN_REQUIRED: 追加元素需要当前包装方案")
    if style not in STYLES:
        raise ValueError("PACKAGING_STYLE_INVALID: 请选择简洁商务或重点突出")
    task = get_task(db, task_id)
    if task is None or task["task_type"] not in {"digital_human", "ad_mix"}:
        raise ValueError("TASK_NOT_FOUND: 包装任务不存在")
    lock = plans._require_picture_lock(db, db.connection, task_id)
    script = read_task_script(Path(project_root), task)
    sha = hashlib.sha256((script or "").encode()).hexdigest()
    if not script or sha != (task.get("brief", {}).get("final_script") or {}).get("sha256"):
        raise RuntimeError("SCRIPT_CHANGED: 确认原稿不存在或已改变")
    if len(script) > 20000:
        raise ValueError("P09_CONTEXT_TOO_LARGE: 文案超过包装分析范围")
    scenes = [dict(row) for row in db.connection.execute(
        "SELECT card_id,idx,narrative_text,narrative_start,narrative_end FROM scene_cards WHERE task_id=? ORDER BY idx,card_id", (task_id,))]
    if not scenes or len(scenes) > 200:
        raise RuntimeError("PACKAGING_TIMING_MISSING: 需要已确认的本片镜头时间轴")
    cursor = 0
    for scene in scenes:
        start, end = float(scene["narrative_start"]), float(scene["narrative_end"])
        if not math.isfinite(end) or not math.isfinite(start) or abs(start-cursor) > 0.00001 or end <= start:
            raise RuntimeError("PACKAGING_TIMING_INVALID: 镜头时间轴不连续")
        cursor = end
    duration = round(cursor * 1000000)
    route = plans._canonical_route(db.connection, task_id)
    choices, allowed_categories = user_choices(db, task_id, base_plan_id, style)
    if requested_roles is not None:
        if not choices["selected_assets"].get("subtitle"):
            raise ValueError("PACKAGING_SUBTITLE_REQUIRED: 当前方案须先保留字幕")
        if set(requested_roles) & set(choices["disabled_categories"]):
            existing = db.connection.execute("SELECT 1 FROM packaging_plan_instances WHERE plan_id=? AND role=? LIMIT 1", (base_plan_id, requested_roles[0])).fetchone()
            if existing:
                raise ValueError("PACKAGING_CATEGORY_DISABLED: 该类别已有关闭的元素，请使用恢复")
            choices["disabled_categories"] = [r for r in choices["disabled_categories"] if r not in requested_roles]
            choices["enabled_by_request"] = requested_roles
        if requested_roles == ["brand"] and any(i["role"] == "brand" for i in choices["instances"]):
            raise ValueError("PACKAGING_SINGLE_INSTANCE_EXISTS: 当前方案已有品牌元素，请编辑已有元素")
        choices["requested_roles"] = requested_roles
        choices["draft_operation"] = "append_only"
        allowed_categories = set(requested_roles)
        if requested_roles == ["sfx"]:
            openings = [i for i in choices["instances"] if i["role"] == "title_card"
                and abs(float(i["start_seconds"])-float(scenes[0]["narrative_start"])) < .001
                and float(i["end_seconds"]) <= float(scenes[0]["narrative_end"])+.001]
            if len(openings) != 1:
                raise ValueError("PACKAGING_EVENT_REQUIRED: 单独添加音效需要一个明确的现有开场标题入场事件")
            choices["existing_opening"] = openings[0]
    from .packaging_music_review import music_feedback_context, rejected_music_hashes
    feedback = music_feedback_context(db, task_id)
    rejected = rejected_music_hashes(feedback)
    choices["music_feedback"] = feedback
    assets = [a for a in available_assets(route, db) if a["slot"] in allowed_categories
              and not (a["slot"] == "bgm" and a.get("source_sha256") in rejected)]
    music_inventory = [a for a in assets if a["slot"] == "bgm"]
    inventory_digest = digest(music_inventory)
    if len(music_inventory) > 40:
        selected = set(choices.get("selected_assets", {}).get("bgm", []))
        def retrieval_key(asset):
            tags = re.findall(r"[\w]+", asset.get("mood") or "")
            overlap = sum(tag.lower() in script.lower() for tag in tags if len(tag) > 1)
            return (asset["ref"] not in selected, -overlap, asset["ref"])
        shortlist = sorted(music_inventory, key=retrieval_key)[:40]
        included = {a["ref"] for a in shortlist}
        assets = [a for a in assets if a["slot"] != "bgm" or a["ref"] in included]
    choices["music_library_scope"] = {"available_count": len(music_inventory),
        "included_count": sum(a["slot"] == "bgm" for a in assets), "inventory_digest": inventory_digest,
        "basis": "超过40首时按已选曲及用户填写风格与文案字词召回；不是全库听感评估，用户可在本地曲库搜索选曲"}
    from .packaging_audio_evidence import get_audio_evidence
    for asset in assets:
        if asset["slot"] in {"bgm", "sfx"} and asset.get("source_sha256"):
            evidence = get_audio_evidence(db, asset["source_sha256"])
            if evidence:
                asset["audioAnalysis"] = evidence
                asset["evidenceLevel"] = "local_pcm_measurement_and_catalog_metadata"
                asset["version"] = digest([asset["version"], evidence])
    # Shared visual styles can serve multiple roles. Expose one ref with all permitted roles.
    by_ref = {}
    for asset in assets:
        entry = by_ref.setdefault(asset["ref"], {**asset, "slots": []})
        entry["slots"].append(asset["slot"])
    from .packaging_visual import prepare_visual_context
    visual = prepare_visual_context(db, project_root, task, lock)
    records = [record("S1", "script", script, sha)]
    for frame in visual["frames"]:
        metadata = {k:v for k,v in frame.items() if k != "media_uri"}
        item = record(frame["ref"], "evidence", json.dumps(metadata, ensure_ascii=False), digest(metadata))
        item["media_uri"] = frame.get("media_uri")
        records.append(item)
    anchors = {"S1": TimeWindow(0, duration)}
    for scene in scenes:
        ref = "shot:" + scene["card_id"]
        records.append(record(ref, "shot", scene["narrative_text"] or "无旁白", digest(scene)))
        anchors[ref] = TimeWindow(round(scene["narrative_start"]*1000000), round(scene["narrative_end"]*1000000))
    phrases = {}
    for index, phrase in enumerate(measured_phrases(db, project_root, task_id, script)):
        start, end = float(phrase["start"]), float(phrase["end"])
        if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end <= start or end > cursor + .001:
            continue
        from .packaging_semantics import resolve_measured_span
        placement = resolve_measured_span(scenes, start, end)
        if placement is None or not (phrase["text"] in clauses(script) or
                (phrase.get("basis") == "measured TTS sentence" and script.count(phrase["text"]) == 1)):
            continue
        ref = "phrase:" + str(index)
        phrases[ref] = {**phrase, "scene_id": placement["anchor_ref"],
                        "scene_ids": placement["scene_ids"]}
        anchors[ref] = TimeWindow(round(start * 1000000), round(end * 1000000))
        item = record(ref, "word", phrase["text"], digest(phrases[ref]))
        item["duration_us"] = anchors[ref].end_us - anchors[ref].start_us
        records.append(item)
    from .packaging_excerpt_candidates import candidates
    excerpt_candidates = candidates(script, phrases)
    # One explicit event supported by the current renderer: opening-title entry.
    first = scenes[0]
    anchors["event:opening"] = TimeWindow(0, round(min(3.8, cursor) * 1e6))
    if choices.get("existing_opening"):
        opening = choices["existing_opening"]
        anchors["event:opening"] = TimeWindow(round(opening["start_seconds"]*1e6), round(opening["end_seconds"]*1e6))
    records.append(record("event:opening", "event", "音效须跟随本次标题或user_choices.existing_opening指定的现有标题入场", digest([first, choices["existing_opening"]]) if choices.get("existing_opening") else digest(first)))
    timeline = {"shots": scenes, "measured_phrases": phrases, "voice": {"duration_seconds": cursor,
        "characters_per_second": round(len(script)/cursor, 2),
        "basis": "确认文案与本片音轨时轴", "source_music": "unknown"},
        "style": style, "allowed_categories": sorted(allowed_categories), "user_choices": choices,
        "limitations": ["没有听审音乐或检测人物安全区；不得声称已完成", "不改变文案、声音或锁定画面", "信息卡和花字只可选C1.excerpt_candidates有限候选，保留句级时段；禁止将问句、条件或否定改成承诺", "开场标题只能选C1.excerpt_candidates中已核源摘录，展示在event:opening前3.8秒；事实卡沿来源整句时段"]}
    timeline["visual_evidence"] = {**visual, "frames": [{k:v for k,v in f.items() if k != "media_uri"} for f in visual["frames"]]}
    timeline["limitations"].extend(visual["limitations"])
    from .packaging_audio_jobs import packaging_audio_state
    audio_state = packaging_audio_state(db, project_root, task_id)
    voice = next((item.get("evidence") for item in audio_state.get("items", [])
                  if item.get("role") == "source_voice" and item.get("status") == "completed"), None)
    if voice:
        timeline["voice"]["audio_analysis"] = voice
        timeline["voice"]["measurement_scope"] = "source audio before playback speed and mixing"
    records.extend([record("T1", "timing", json.dumps(timeline["voice"],ensure_ascii=False),digest(timeline["voice"])),
                    record("L1", "timeline", json.dumps(timeline,ensure_ascii=False),digest(timeline))])
    for ref,a in by_ref.items():
        records.append(record(ref, "packaging", json.dumps(a,ensure_ascii=False), a["version"]))
    from .packaging_execution_contract import build_capabilities
    capabilities=build_capabilities(scenes,phrases,by_ref,allowed_categories,choices,visual,excerpt_candidates=excerpt_candidates)
    if requested_roles is not None and not any(capabilities["assets_by_kind"].values()):
        raise ValueError("PACKAGING_CATEGORY_UNAVAILABLE: 当前类别没有可用素材或可靠时间锚点，请先补齐素材或手动设置")
    records.append(record("C1","timeline",json.dumps(capabilities,ensure_ascii=False),digest(capabilities)))
    current = [plans.get_plan(db, p["plan_id"]) for p in plans.list_plans(db,task_id)]
    input_digest = digest({"method":METHOD,"lock":lock,"script":sha,"timeline":timeline,"assets":assets,"execution_capabilities":capabilities,"manual_and_existing_plans":current})
    orientation = "portrait"
    if task["task_type"] == "ad_mix":
        aspect=task.get("brief",{}).get("aspect_ratio")
        if aspect not in {"9:16","16:9"}:raise RuntimeError("AD_ASPECT_RATIO_REQUIRED: 广告画幅未确认")
        orientation="landscape" if aspect=="16:9" else "portrait"
    request={"template_id":"P09","schema_version":"1.0.0","request_id":request_id,
        "input_digest":input_digest,"base_revision":0,"route":"digital" if task["task_type"]=="digital_human" else "ad",
        "orientation":orientation,"allowed_operations":[],"locked_refs":[],"records":records,
        "payload":{"script_refs":["S1"],"timing_refs":["T1"],"timeline_refs":["L1","C1"],
        "packaging_asset_refs":list(by_ref),"fact_refs":["S1"],"source_overlay_refs":[],
        "user_request":"为当前风格生成新的建议草案，不修改现有人工方案。仅限允许类别，严禁重开已关闭类别。user_choices是当前用户设置；若建议与已选素材或元素不同，理由中说明差异，原方案保持不变。music_feedback是本片真实试听反馈，音量合适不代表曲风认可；已否定的同一音频已排除。不能只凭题材标签判断适配：结合文案语气、镜头节奏和口播密度说明候选与原曲的差异，缺少听感依据要明说。没有合适音乐可以不选；逐项给理由及未知信息。若提供真实帧，结合构图、可见人物/文字/水印说明具体包装依据，理由中引用frame ref；没有取样的镜头不得臆测。源帧不等于最终裁切/叠加效果，不能认定安全区已检测完成。"}}
    if requested_roles is not None:
        request["payload"]["user_request"] = ("本次仅追加 " + requested_roles[0] + "，只输出这一类新增实例。"
            "现有人工和其他类别全部由程序复制保留，不要重写或重复输出。无需返回字幕；若无合适元素，返回空instances并说明。"
            "若为sfx，仅跟随user_choices.existing_opening；禁止新增标题或其他元素。" + request["payload"]["user_request"])
    context=ModelContext(template_id="P09",request_id=request_id,revision=0,input_digest=input_digest,
        route=request["route"],orientation=orientation,records={x["ref"]:x for x in records},
        approved_fact_refs=frozenset({"S1"}),anchor_windows=anchors,schema_version="1.0.0")
    validate_envelope(request,context)
    return {"request":request,"context":context,"assets":by_ref,"script":script,"style":style,"phrases":phrases,"excerpt_candidates":excerpt_candidates,"scenes":scenes,"allowed_categories":allowed_categories,"base_plan_id":base_plan_id,"user_choices":choices,"visual":visual,"execution_capabilities":capabilities}

def _resolve(prepared, response):
    validate_result(prepared["request"], response, prepared["context"])
    if response["status"] not in {"ok","partial"} or response["result"] is None:
        raise RuntimeError("PACKAGING_NEEDS_INPUT: 模型未提供可用包装建议")
    frame_refs = {f["ref"] for f in prepared["visual"]["frames"]}
    if frame_refs and not frame_refs.intersection(response.get("evidence_refs", [])):
        raise ValueError("PACKAGING_VISUAL_EVIDENCE_MISSING: 画面建议须引用本次真实帧依据")
    resolved=[];single=set()
    for item in response["result"]["instances"]:
        slot=KIND_SLOT[item["kind"]];asset=prepared["assets"].get(item["asset_ref"])
        if asset is None or slot not in asset["slots"] or slot not in prepared["allowed_categories"] or ("excerpt_candidates" in prepared and item["asset_ref"] not in prepared["execution_capabilities"]["assets_by_kind"].get(item["kind"], [])):
            raise ValueError("PACKAGING_ASSET_MISMATCH: 素材不属于允许的包装类别")
        if slot in {"bgm","subtitle","brand"}:
            if slot in single:raise ValueError("PACKAGING_DUPLICATE_SLOT: 单项包装重复")
            single.add(slot)
        bounds=[]
        for field in ["start_anchor","end_anchor"]:
            anchor=item[field];window=prepared["context"].anchor_windows.get(anchor["target_ref"])
            if window is None:raise ValueError("PACKAGING_ANCHOR_UNKNOWN: 锚点没有真实时间")
            bounds.append(window.end_us if anchor["edge"]=="end" or (anchor["edge"]=="span" and field=="end_anchor") else window.start_us)
        if bounds[1]<=bounds[0]:raise ValueError("PACKAGING_ANCHOR_INVALID: 包装区间无效")
        text=(item.get("content") or {}).get("text")
        if slot in {"sticker","sfx","brand"} and item.get("content") is not None:
            raise ValueError("PACKAGING_NON_TEXT_CONTENT_INVALID: 此元素无可编辑文字，content必须为null")
        if slot in {"subtitle","bgm"}:
            if bounds != [0,prepared["context"].anchor_windows["S1"].end_us] or text is not None:
                raise ValueError("PACKAGING_GLOBAL_LAYER_INVALID: 字幕与BGM沿全片，字幕不可被模型改写")
        if slot in {"title_card", "info_card", "fancy_text"}:
            if "excerpt_candidates" not in prepared:
                legal_texts=set(clauses(prepared["script"])) | {p["text"] for p in prepared["phrases"].values()}
                if not text or text not in legal_texts:
                    raise ValueError("PACKAGING_FACT_UNVERIFIED: 包装文字必须使用确认原文完整短句，保留数字、单位和限定词")
                start_ref, end_ref = item["start_anchor"]["target_ref"], item["end_anchor"]["target_ref"]
                if slot == "title_card":
                    first = prepared["scenes"][0]
                    if start_ref != "shot:" + first["card_id"] or end_ref != start_ref or text not in (prepared["execution_capabilities"]["opening"]["texts"] if "execution_capabilities" in prepared else clauses(first["narrative_text"] or "")):
                        raise ValueError("PACKAGING_FACT_ANCHOR: 开场标题须绑定开场镜头的已有事实")
                else:
                    phrase = prepared["phrases"].get(start_ref)
                    if start_ref != end_ref or not phrase or text != phrase["text"]:
                        raise ValueError("PACKAGING_FACT_ANCHOR: 信息卡和花字须绑定对应口播短句的真实时间")
            else:
                start_ref, end_ref = item["start_anchor"]["target_ref"], item["end_anchor"]["target_ref"]
                candidates_by_ref = prepared.get("excerpt_candidates", {})
                permitted = [c for values in candidates_by_ref.values() for c in values] if slot == "title_card" else candidates_by_ref.get(start_ref, [])
                match = next((c for c in permitted if c["text"] == text), None)
                if not match:
                    raise ValueError("PACKAGING_FACT_UNVERIFIED: 文字不属于本次已核源的安全摘录候选")
                if start_ref != end_ref or (slot == "title_card" and start_ref != "event:opening"):
                    raise ValueError("PACKAGING_FACT_ANCHOR: 文字须绑定本次候选对应的真实时间")
                item = {**item, "_content_layout": {**match["layout"], "source": {
                    k: match[k] for k in ("source_ref", "script_start", "script_end")}}}
        if slot == "sticker":
            if item["start_anchor"]["target_ref"] != item["end_anchor"]["target_ref"] or not item["start_anchor"]["target_ref"].startswith("shot:"):
                raise ValueError("PACKAGING_ANCHOR_INVALID: 贴纸须绑定实际镜头")
        if slot in {"title_card","info_card","fancy_text"} and (not text or len(text)>40):
            raise ValueError("PACKAGING_TEXT_INVALID: 包装文字缺失或过长")
        if slot=="sfx":
            if any(item[field]["target_ref"] != "event:opening" for field in ("start_anchor", "end_anchor")):
                raise ValueError("PACKAGING_EVENT_REQUIRED: 音效须绑定支持的可见入场事件")
            path=plans.resolve_manifest_asset_path(slot,item["asset_ref"])
            length=plans.audio_duration_seconds(path) if path else None
            if not length:raise ValueError("PACKAGING_AUDIO_MISSING: 音效不可用")
            bounds[1]=min(bounds[1],bounds[0]+round(length*1000000))
        if slot not in {"subtitle","bgm","sfx"} and (bounds[1]-bounds[0])/1000000 < max(.5,len(text or "")*.08):
            raise ValueError("PACKAGING_READING_TIME_SHORT: 包装文字停留时间不足")
        resolved.append((slot,item,bounds,text))
    if any(x[0] == "sfx" for x in resolved) and not any(x[0] == "title_card" for x in resolved) and not prepared["user_choices"].get("existing_opening"):
        raise ValueError("PACKAGING_EVENT_REQUIRED: 没有标题入场，不能附加标题音效")
    if "subtitle" not in single and not prepared["user_choices"].get("requested_roles"):raise ValueError("PACKAGING_SUBTITLE_REQUIRED: 方案必须保留字幕")
    if "excerpt_candidates" not in prepared: return resolved
    limits = prepared.get("execution_capabilities", {}).get("element_budget", {"opening": 1, "info": 2})
    for kind, limit in limits.items():
        if sum(i[0] == KIND_SLOT[kind] for i in resolved) > limit:
            raise ValueError("PACKAGING_ELEMENT_BUDGET: 新增元素超过本片数量预算")
    decorated = [i for i in resolved if i[0] in {"title_card", "info_card", "fancy_text"}]
    for index, left in enumerate(decorated):
        for right in decorated[index+1:]:
            if left[3] == right[3] and max(left[2][0],right[2][0]) < min(left[2][1],right[2][1]):
                raise ValueError("PACKAGING_DUPLICATE_EMPHASIS: 同一文字不能同时重复强调")
    return resolved

def materialize_packaging_suggestion(db, project_root, task_id, receipt_id, style, *, connection=None):
    from .model_requests import _write_scope
    with _write_scope(db, connection) as conn:
        existing=conn.execute("SELECT plan_id FROM packaging_recommendation_links WHERE receipt_id=? AND task_id=?",(receipt_id,task_id)).fetchone()
        if existing:return {"planId":existing["plan_id"],"receiptId":receipt_id}
        row=conn.execute("SELECT r.request_json,d.response_json,d.state FROM model_result_drafts d JOIN model_contract_requests r USING(request_id) WHERE d.receipt_id=? AND r.task_id=? AND r.template_id='P09'",(receipt_id,task_id)).fetchone()
        if row is None or row["state"]!="draft":raise RuntimeError("PACKAGING_SUGGESTION_UNAVAILABLE: 建议不存在或已过期")
        request=json.loads(row["request_json"]);response=json.loads(row["response_json"])
        prepared=prepare_packaging_request(db,project_root,task_id,request["request_id"],style,base_plan_id=request_base_plan(request),requested_roles=request_roles(request))
        if prepared["request"]!=request:raise RuntimeError("STALE_INPUT: 稿件、镜头、素材或人工方案已改变")
        return _materialize_verified_suggestion(db, task_id, receipt_id, style, prepared, connection=conn)


def _fit_new_text(db, plan, value):
    """Lay out only a newly generated text element; preserve manual instances.

    Measure with the actual renderer fonts, wrap exact text rather than making
    it unreadably small, and prefer free space outside subtitle/element bounds.
    Composition hints remain conservative; this is not face detection.
    """
    if value["role"] not in {"title_card", "info_card", "fancy_text"}:
        return value
    from .packaging_layout import inspect_packaging_layout
    value = dict(value)
    context = inspect_packaging_layout(db, plan)
    width, height = context["frameSize"]
    limit = 14 if width < height else 24
    text = value.get("content_text") or ""
    # Numbers, decimals, units and Latin words stay together across line breaks.
    lines, line = [], ""
    for token in re.findall(r"\d+(?:[.,]\d+)*(?:[%A-Za-z]+|[多余万亿千百个度秒米元毛分吨年月日]+)?|[一二三四五六七八九十百]+分之[一二三四五六七八九十百]+|[A-Za-z]+|[^\n]", text):
        if line and len(line) + len(token) > limit and token not in "，。！？；、,.!?;":
            cut = max((i+1 for i, c in enumerate(line) if c in "，。！？；、,.!?;" and i+1 >= limit/2), default=len(line))
            lines.append(line[:cut])
            line = line[cut:]
        line += token
    if line: lines.append(line)
    value["content_text"] = "\n".join(lines)
    minimum_scale = .5 if width < height else .3
    value["scale"] = max(value["scale"], .84 if value["role"] == "fancy_text" else minimum_scale)
    existing = [i for i in plan.get("instances", []) if i.get("instance_id") != value.get("instance_id")]
    def inspect(candidate):
        trial = {**plan, "instances": existing + [{**candidate, "instance_id": "new-text"}]}
        report = inspect_packaging_layout(db, trial)
        bound = next((b for b in report["bounds"] if b["instanceId"] == "new-text"), None)
        return report, bound
    # Fit to the frame without going below the readable font threshold.
    for _ in range(4):
        report, bound = inspect(value)
        if not bound: return value
        left, top, right, bottom = bound["box"]
        span, tall = right-left, bottom-top
        if span <= .92 and tall <= .92: break
        next_scale = value["scale"] * min(.92/span, .92/tall, 1) * .98
        if next_scale < minimum_scale and value["role"] != "fancy_text": break
        value["scale"] = next_scale
    left, top, right, bottom = bound["box"]
    value["x"] += max(.04-left, 0) - max(right-.96, 0)
    value["y"] += max(.04-top, 0) - max(bottom-.96, 0)
    tall = bottom-top
    subtitle = context.get("subtitleBox")
    locations = [value["y"], .04, .96-tall]
    if subtitle:
        locations.extend([subtitle[3]+.025, subtitle[1]-tall-.025])
    weights = {"PACKAGING_SAFE_AREA": 100, "PACKAGING_SUBTITLE_OVERLAP": 40,
               "PACKAGING_OVERLAP": 30, "PACKAGING_PERSON_REGION": 10,
               "PACKAGING_TEXT_TOO_SMALL": 100}
    best, best_score = value, float("inf")
    for y in locations:
        candidate = {**value, "y": max(.04, min(.96-tall, y))}
        report, _ = inspect(candidate)
        score = sum(weights.get(i["code"], 0) for i in report["issues"] if "new-text" in i["instanceIds"])
        if score < best_score:
            best, best_score = candidate, score
        if score == 0: break
    return best


def _inherit_existing_instances(db, connection, base_plan_id, plan_id, *, replaced_roles=()):
    """Retain edits and omitted roles; replace only unedited AI roles supplied anew."""
    if not base_plan_id:
        return []
    base = plans.get_plan(db, base_plan_id)
    target = dict(connection.execute("SELECT * FROM packaging_plans WHERE plan_id=?", (plan_id,)).fetchone())
    if base["task_id"] != target["task_id"]:
        raise ValueError("PACKAGING_PLAN_MISMATCH: 当前包装方案不属于本任务")
    source = {i["instance_id"]: i for i in base["instances"]}
    keep = {key for key, value in source.items() if value["origin"] == "manual" or value["role"] not in replaced_roles}
    # Preserve both an edited sound's parent and sounds following a kept title.
    while True:
        expanded = keep | {i["anchor_ref"] for key, i in source.items()
            if key in keep and i["anchor_kind"] == "instance"} | {key for key, i in source.items()
            if i["anchor_kind"] == "instance" and i["anchor_ref"] in keep}
        if expanded == keep:
            break
        if not expanded <= source.keys():
            raise ValueError("PACKAGING_ANCHOR_INVALID: 已有元素的关联对象不存在")
        keep = expanded
    configured = {i[0] for i in connection.execute(
        "SELECT slot FROM packaging_plan_items WHERE plan_id=? AND is_backup=0", (plan_id,))}
    roles = {source[key]["role"] for key in keep}
    for item in base["items"]:
        if item["is_backup"] or item["slot"] not in roles or item["slot"] in configured:
            continue
        connection.execute("INSERT INTO packaging_plan_items(plan_id,slot,asset_ref,enabled,notes) VALUES(?,?,?,?,?)",
            (plan_id, item["slot"], item["asset_ref"], item["enabled"], "保留本片已调整元素"))
        configured.add(item["slot"])
    mapping = {}
    for key in sorted(keep):
        item = source[key]
        mapping[key] = _insert(connection, plan_id, item, origin=item["origin"], follow_event=bool(item["follow_event"]))
    for key, new_id in mapping.items():
        item = source[key]
        if item["anchor_kind"] == "instance":
            connection.execute("UPDATE packaging_plan_instances SET anchor_ref=? WHERE instance_id=?",
                (mapping[item["anchor_ref"]], new_id))
    inherited = [dict(i) for i in connection.execute("SELECT * FROM packaging_plan_instances WHERE plan_id=?", (plan_id,))]
    disabled = {i["slot"] for i in base["items"] if not i["is_backup"] and not i["enabled"]}
    for item in inherited:
        if item["role"] not in disabled:
            _validate_instance(connection, target, item)
    connection.execute("UPDATE packaging_plans SET instance_revision=instance_revision+? WHERE plan_id=?", (len(mapping), plan_id))
    return [i for i in inherited if i["role"] not in disabled]


def _materialize_verified_suggestion(db, task_id, receipt_id, style, prepared, *, connection):
    """Worker-only fast path after full input revalidation in this write transaction."""
    from .model_requests import _write_scope
    with _write_scope(db, connection) as conn:
        row=conn.execute("SELECT r.request_json,d.response_json,d.state FROM model_result_drafts d JOIN model_contract_requests r USING(request_id) WHERE d.receipt_id=? AND r.task_id=? AND r.template_id='P09'",(receipt_id,task_id)).fetchone()
        if row is None or row["state"] != "draft" or json.loads(row["request_json"]) != prepared["request"]:
            raise RuntimeError("STALE_INPUT: 建议与本次已校验输入不一致")
        response=json.loads(row["response_json"])
        resolved=_resolve(prepared,response)
        scoped = bool(prepared["user_choices"].get("requested_roles"))
        title_id = None
        inherited = []
        if scoped:
            cloned = plans.clone_plan(db, prepared["base_plan_id"], plan_name="追加元素 · 本片建议", include_instance_map=True, connection=conn)
            plan_id = cloned["plan_id"]
            inherited = plans.active_instances(plans.get_plan(db, plan_id))
            conn.execute("UPDATE packaging_plans SET origin_type='system_recommendation' WHERE plan_id=?",(plan_id,))
            for role in prepared["user_choices"].get("enabled_by_request", []):
                conn.execute("UPDATE packaging_plan_items SET enabled=1 WHERE plan_id=? AND slot=? AND is_backup=0",(plan_id,role))
            existing_opening = prepared["user_choices"].get("existing_opening")
            if existing_opening:
                title_id = cloned["instance_map"][existing_opening["instance_id"]]
        else:
            plan_id="pp-"+uuid.uuid4().hex[:12]
            route=plans._canonical_route(conn,task_id)
            conn.execute("INSERT INTO packaging_plans(plan_id,task_id,route,plan_name,status,origin_type,schema_version,settings_schema_version,bgm_gain) VALUES(?,?,?,?, 'draft','system_recommendation',2,1,0.18)",
                (plan_id,task_id,route,"克制精致 · 本片建议" if style=="business" else "重点突出 · 本片建议"))
            settings = prepared["user_choices"]["settings"]
            if settings:
                conn.execute("UPDATE packaging_plans SET bgm_gain=?,subtitle_highlight=? WHERE plan_id=?",
                             (settings["bgm_gain"], int(settings["subtitle_highlight"]), plan_id))
                conn.execute("UPDATE packaging_plans SET subtitle_appearance_json=? WHERE plan_id=?", (json.dumps(settings.get("subtitle_appearance", {}), sort_keys=True), plan_id))
        if not scoped:
            inherited = _inherit_existing_instances(db, conn, prepared["base_plan_id"], plan_id, replaced_roles={x[0] for x in resolved})
            title_id = next((i["instance_id"] for i in inherited if i["role"] == "title_card"), None)
        plan=dict(conn.execute("SELECT * FROM packaging_plans WHERE plan_id=?",(plan_id,)).fetchone())
        configured={r["slot"] for r in conn.execute("SELECT slot FROM packaging_plan_items WHERE plan_id=? AND is_backup=0",(plan_id,))}
        for slot,item,bounds,text in sorted(resolved, key=lambda x: x[0] == "sfx"):
            # Preserve human work, suppress only new overlapping decoration.
            if slot in {"title_card", "info_card", "fancy_text"} and any(
                i["role"] in {"title_card", "info_card", "fancy_text"} and i["content_text"] == text
                and max(i["start_seconds"], bounds[0]/1e6) < min(i["end_seconds"], bounds[1]/1e6)
                for i in inherited):
                continue
            limit = {"title_card": 1, "info_card": 2}.get(slot)
            if limit is not None and conn.execute("SELECT COUNT(*) FROM packaging_plan_instances WHERE plan_id=? AND role=?", (plan_id,slot)).fetchone()[0] >= limit:
                continue
            # A generated alternative must not cover an element the user already edited.
            if any(i["role"] == slot and (slot in {"title_card", "brand"} or
                   max(i["start_seconds"], bounds[0]/1000000) < min(i["end_seconds"], bounds[1]/1000000)) for i in inherited):
                continue
            if slot not in configured:
                conn.execute("INSERT INTO packaging_plan_items(plan_id,slot,asset_ref,rank,is_backup,enabled,notes) VALUES(?,?,?,0,0,1,?)",(plan_id,slot,item["asset_ref"],item["reason"]))
                configured.add(slot)
            if slot in {"bgm","subtitle"}:continue
            x,y,scale=DEFAULT_POSITION[slot]
            if slot!="sfx" and item["position"]=="top":y=.08
            elif slot!="sfx" and item["position"]=="bottom":y=.82
            elif slot!="sfx" and item["position"] in {"middle","left","right"}:
                # Draft placement, never evidence of a safe final composition.
                # Bounds and the existing rendered-preview/watch gates still apply.
                y = .42
                x = ((1 - scale) / 2 if item["position"] == "middle"
                     else .04 if item["position"] == "left" else .96 - scale)
            ref = item["start_anchor"]["target_ref"]
            scene_id = prepared["phrases"].get(ref, {}).get("scene_id") or (ref[5:] if ref.startswith("shot:") else None)
            anchor_kind, anchor_ref = ("scene", scene_id) if scene_id and slot != "title_card" else ("timeline", None)
            if slot == "sfx":
                anchor_kind, anchor_ref = "instance", title_id
            value=_validate_instance(conn,plan,{"role":slot,"asset_ref":item["asset_ref"],"content_text":text,
                "anchor_kind":anchor_kind,"anchor_ref":anchor_ref,"start_seconds":bounds[0]/1000000,"end_seconds":bounds[1]/1000000,
                "x":x,"y":y,"scale":scale,"gain":.25 if slot=="sfx" else None})
            if (item["asset_ref"].startswith("card-refined-") or item["asset_ref"] == "ft-refined-v1"):
                value["content_layout_json"] = json.dumps(item.get("_content_layout") or {}, ensure_ascii=False)
                value["x"],value["y"],value["scale"] = ((.075,.09,.60) if slot == "title_card" else (.063,.575,.44))
            else:
                value = _fit_new_text(db, plans.get_plan(db, plan_id), value)
            value = _validate_instance(conn, plan, value)
            instance_id = _insert(conn,plan_id,value,origin="recommendation",follow_event=slot == "sfx")
            if slot == "title_card": title_id = instance_id
            conn.execute("UPDATE packaging_plans SET instance_revision=instance_revision+1 WHERE plan_id=?",(plan_id,))
        if prepared["base_plan_id"] and not scoped:
            base = plans.get_plan(db, prepared["base_plan_id"])
            for item in base["items"]:
                if not item["is_backup"] and item["slot"] in prepared["user_choices"]["disabled_categories"] and item["slot"] not in configured:
                    conn.execute("INSERT INTO packaging_plan_items(plan_id,slot,asset_ref,rank,is_backup,enabled,notes) "
                                 "VALUES(?,?,?,0,0,0,?)", (plan_id,item["slot"],item["asset_ref"],"保留用户关闭状态"))
        conn.execute("INSERT INTO packaging_recommendation_links(receipt_id,plan_id,task_id,method_version) VALUES(?,?,?,?)",(receipt_id,plan_id,task_id,METHOD))
    return {"planId":plan_id,"receiptId":receipt_id}
