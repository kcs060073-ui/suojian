"""Bounded real-source evidence for P09. No implicit provider calls or fallback."""
import base64
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import shutil

from .content_review_jobs import request_visual_json
from .visual_media_samples import _source, _frame, _video_frame, _image_frame, _duration_us

VERSION="packaging-frames-v1"
MAX_FRAMES=8
LIMITATIONS=["最多8张锁定源时间窗内静帧；未取样镜头与连续运动未知", "源帧不是最终裁切/叠加成片；人物安全区和遮挡需人工预览，不自动认定安全", "没有声音输入；不得声称听过音乐或看过完整成片"]


@lru_cache(maxsize=32)
def _source_sha(path,size,mtime,ctime):
    from .fingerprint import full_sha256
    return full_sha256(Path(path))


@lru_cache(maxsize=64)
def _sample(path,sha,kind,start_us,end_us):
    ffmpeg=Path(shutil.which("ffmpeg") or "ffmpeg")
    if kind=="image":return _image_frame(Path(path))
    if not 0<=start_us<end_us<=_duration_us(Path(path),ffmpeg):raise ValueError("SOURCE_WINDOW_INVALID")
    return _video_frame(Path(path),start_us+(end_us-start_us)//2,ffmpeg)


def _windows(db,task):
    from .rough_cut import _snapshot_state, _source_window, _is_digital_anchor
    state=_snapshot_state(db.connection,task["task_id"]);windows=[];missing=[]
    main=[x["asset_id"] for x in state["task_assets"] if x["role"]=="digital_human"]
    for card in state["cards"]:
        digital=task["task_type"]=="digital_human" and _is_digital_anchor(state,card)
        aid=main[0] if digital and len(main)==1 else card["asset_id"]
        if not aid:missing.append({"shotRef":"shot:"+card["card_id"],"reason":"没有可用源画面绑定"});continue
        start,end=(float(card["narrative_start"])*1.25,float(card["narrative_end"])*1.25) if digital else _source_window(db.connection,state,card)
        # The renderer uses only the narrative-length prefix of a longer trim.
        if not digital and end>start:end=min(end,start+float(card["narrative_end"])-float(card["narrative_start"]))
        base={"card":card,"asset_id":aid,"start_us":round(start*1e6),"end_us":round(end*1e6),"layer":"main" if digital else "insert"}
        windows.append(base)
        if task["task_type"]=="digital_human" and card["mix_form"] in {"画中画","信息卡"} and len(main)==1:
            windows.append({**base,"asset_id":main[0],"start_us":round(card["narrative_start"]*1.25e6),"end_us":round(card["narrative_end"]*1.25e6),"layer":"background"})
    return windows,missing,len(state["cards"])


def _authorized_visual_source(db, task_id, asset_id, start_us, end_us):
    from .model_media_access import require_source_access
    return require_source_access(db, task_id, asset_id, start_us, end_us)


def _assert_frame_permissions(db, task_id, prepared):
    for frame in prepared["visual"]["frames"]:
        location = _authorized_visual_source(db, task_id, frame["asset_id"], *frame["source_window_us"])
        _, _, fingerprint = _source(db, frame["asset_id"], location["library_id"], location["relative_path"])
        if fingerprint != frame["asset_fingerprint"]:
            raise RuntimeError("PACKAGING_VISUAL_SOURCE_CHANGED")


def prepare_visual_context(db,project_root,task,lock):
    windows,missing,total=_windows(db,task)
    if len(windows)>MAX_FRAMES:
        windows=[windows[round(i*(len(windows)-1)/(MAX_FRAMES-1))] for i in range(MAX_FRAMES)]
    frames=[]
    for w in windows:
        location = _authorized_visual_source(db, task["task_id"], w["asset_id"], w["start_us"], w["end_us"])
        try:
            source,kind,fp=_source(db,w["asset_id"],location["library_id"],location["relative_path"])
        except ValueError as error:
            raise RuntimeError("PACKAGING_VISUAL_SOURCE_UNAVAILABLE: 锁定源画面离线或已改变，请先重连/重扫素材") from error
        stat=source.stat();sha=_source_sha(str(source),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        sample=_sample(str(source),sha,kind,w["start_us"],w["end_us"])
        after=source.stat()
        if (stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise RuntimeError("PACKAGING_VISUAL_SOURCE_CHANGED")
        card=w["card"];ref="frame:"+card["card_id"]+":"+w["layer"]+":"+sample.sha256[:16]
        frames.append({"ref":ref,"shot_ref":"shot:"+card["card_id"],"asset_id":w["asset_id"],
            "asset_fingerprint":fp,"source_sha256":sha,"source_window_us":[w["start_us"],w["end_us"]],
            "timeline_window_us":[round(card["narrative_start"]*1e6),round(card["narrative_end"]*1e6)],
            "position_seconds":sample.timestamp_us/1e6,"sha256":sample.sha256,"layer":w["layer"],"mix_form":card["mix_form"],
            "media_uri":"data:image/jpeg;base64,"+base64.b64encode(sample.jpeg_bytes).decode("ascii")})
    return {"version":VERSION,"status":"sampled" if frames else "insufficient_evidence","frames":frames,
        "sampledShotCount":len({f["shot_ref"] for f in frames}),"totalShotCount":total,"missing":missing,"limitations":LIMITATIONS}


def load_frames(prepared):
    result=[]
    for f in prepared["visual"]["frames"]:
        try:data=base64.b64decode(f["media_uri"].split(",",1)[1],validate=True)
        except (KeyError,ValueError,IndexError) as e:raise ValueError("PACKAGING_VISUAL_FRAME_INVALID") from e
        sample=_frame(round(f["position_seconds"]*1e6),data)
        if sample.sha256!=f["sha256"]:raise ValueError("PACKAGING_VISUAL_FRAME_CHANGED")
        result.append({**{k:v for k,v in f.items() if k!="media_uri"},"jpeg_bytes":data})
    return result


def verified_binding(db,task_id):
    from .model_profiles import get_task_profile_binding_for_usage, _profile_row, _binding_snapshot
    from .visual_capabilities import require_verified_visual_profile, visual_profile_digest
    binding=get_task_profile_binding_for_usage(db,task_id,"visual_analysis")
    if not binding:
        default=db.connection.execute("SELECT profile_id FROM model_profiles WHERE capability='visual_analysis' AND is_default=1 AND enabled=1").fetchone()
        if default is None:raise RuntimeError("PACKAGING_VISUAL_UNAVAILABLE: 未配置已验证默认视觉模型，无法根据画面生成包装建议")
        binding=_binding_snapshot(_profile_row(db,default["profile_id"]))
    try:require_verified_visual_profile(db,binding["profileId"])
    except ValueError as e:raise RuntimeError("PACKAGING_VISUAL_UNAVAILABLE: 图片、帧序列和结构化协议须通过近期验证") from e
    row=_profile_row(db,binding["profileId"])
    current=_binding_snapshot(row,bound_capability=binding["capability"])
    if current["configDigest"]!=binding["configDigest"]:raise RuntimeError("MODEL_BINDING_CHANGED: 当前视觉配置与任务绑定不一致")
    return {**binding,"visualDigest":visual_profile_digest(row)}



def visual_recovery(db, task_id):
    """Explain existing configuration without selecting, probing or writing it."""
    from .model_profiles import list_model_profiles, get_task_profile_binding_for_usage, _parse_iso, PROFILE_CHECK_MAX_AGE
    from .visual_capabilities import require_verified_visual_profile
    from datetime import datetime, timezone
    candidates = []
    for profile in list_model_profiles(db, capability="visual_analysis"):
        try:
            require_verified_visual_profile(db, profile["profileId"])
            verified = True
        except ValueError:
            verified = False
        checked = _parse_iso(profile.get("lastCheckedAt"))
        expired = checked is not None and datetime.now(timezone.utc) - checked > PROFILE_CHECK_MAX_AGE
        candidates.append({key: profile.get(key) for key in ("profileId", "displayName", "model", "isDefault", "enabled", "lastCheckedAt")} | {
            "verified": verified, "verificationRequired": not verified, "verificationExpired": expired})
    binding = get_task_profile_binding_for_usage(db, task_id, "visual_analysis")
    selected = next((p for p in candidates if p["profileId"] == binding["profileId"]), None) if binding else next((p for p in candidates if p["isDefault"] and p["enabled"]), None)
    if not candidates:
        code, action, message = "PACKAGING_VISUAL_PROFILE_MISSING", "configure_visual_model", "尚未配置视觉模型，请在模型设置中添加并验证。"
    elif selected is None:
        code, action, message = "PACKAGING_VISUAL_SELECTION_REQUIRED", "configure_visual_model", "已有视觉模型，但本任务尚未绑定且未设置默认；请在模型设置中选择并检查可用状态。"
    elif not selected["verified"]:
        code, action = "PACKAGING_VISUAL_VERIFICATION_REQUIRED", "verify_visual_model"
        message = "视觉模型验证已过期，请在模型设置中重新检测。" if selected["verificationExpired"] else "视觉模型未通过当前配置的完整验证，请在模型设置中检查并重新检测。"
    else:
        code, action, message = "MODEL_BINDING_CHANGED", "configure_visual_model", "任务绑定与当前视觉配置不一致，请在模型设置中核对。"
    return {"code": code, "recoveryAction": action, "visualCandidates": candidates, "message": message,
            "visualProfileId": selected["profileId"] if selected else None}


def assert_visual_binding(db,task_id,expected):
    current=verified_binding(db,task_id)
    if any(current.get(key)!=expected.get(key) for key in ("profileId","configDigest","visualDigest")):
        raise RuntimeError("MODEL_BINDING_CHANGED: 本次批准的视觉配置已改变")


def _model_metadata(value):
    """Keep measured summaries and coverage; omit dense PCM windows from prompt.

    Full evidence stays in the digest-bound registered request. Opening/middle/
    ending windows, overall metrics, tempo uncertainty and limitations are kept.
    """
    if isinstance(value, dict):
        return {k: _model_metadata(v) for k, v in value.items()
                if k not in {"energy_segments", "pcm"}}
    if isinstance(value, list):
        return [_model_metadata(v) for v in value]
    return value


def request_visual_suggestion(db,binding,prepared,prompt,schema,*,task_id):
    # Inline bytes go only as image parts; retain metadata in the textual contract.
    from .workbench_worker import ProviderRequestNotSent
    try:
        request=json.loads(json.dumps(prepared["request"]))
        # Storage signatures remain in the registered request covered by input_digest.
        # They are not semantic input to the model; avoid repeating 68 asset hashes,
        # null fields and the schema (request_visual_json supplies it once).
        for item in request["records"]:
            if item["ref"].startswith("frame:"):
                item["media_uri"]=None
                # Full frame source/window/hash metadata is already in L1 and the
                # image parts. Keep its reference without repeating it a third time.
                item["text"]="真实源帧；完整源窗口、帧哈希及镜头映射见同ref图片附件元数据"
            elif item["kind"] in {"timeline","timing"}:
                metadata = _model_metadata(json.loads(item["text"]))
                if item["ref"] == "L1":
                    # Full source/frame metadata accompanies each image part.
                    # Refer to it once rather than repeating all frame hashes.
                    metadata["visual_evidence"]["frames"] = [
                        {"ref": frame["ref"], "metadata": "见同ref图片附件"}
                        for frame in metadata["visual_evidence"].get("frames", [])]
                    if "voice" in metadata:
                        metadata["voice"] = {"ref": "T1", "basis": "声音与测量摘要见T1"}
                item["text"]=json.dumps(metadata,ensure_ascii=False,separators=(",",":"))
            if item["kind"] == "packaging":
                metadata=_model_metadata(json.loads(item["text"]))
                for key in ("ref", "version", "source_sha256"):
                    metadata.pop(key,None)
                item["text"]=json.dumps({k:v for k,v in metadata.items() if v is not None},
                    ensure_ascii=False,separators=(",",":"))
            for key in list(item):
                if key == "version" or item[key] is None:del item[key]
        frames=load_frames(prepared)
        def guard():
            assert_visual_binding(db,task_id,binding)
            _assert_frame_permissions(db,task_id,prepared)
        guard()
    except Exception as error:
        raise ProviderRequestNotSent(str(error)) from error
    return request_visual_json(db,binding["profileId"],system_prompt=prompt,
        user_text=json.dumps({"request":request},ensure_ascii=False,sort_keys=True,separators=(",",":")),
        frames=frames,schema=schema,max_tokens=4000,
        expected_profile_binding=binding,binding_guard=guard,correct_schema_once=True)
