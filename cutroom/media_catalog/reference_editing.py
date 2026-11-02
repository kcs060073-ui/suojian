"""Version-bound reference arrangements over already authorized task assets."""
import json
import hashlib
import re
import subprocess
from pathlib import Path

from .camera_motion import detect_camera_motion
from .ad_subtitles import layout_ad_subtitles


def freeze_reference_policy(policy):
    value = json.loads(json.dumps(policy))
    for key in ("task_id", "plan_id", "request_digest"):
        value.pop(key, None)
    if not value.get("shots") or any(not shot.get("motion") for shot in value["shots"]):
        raise ValueError("请先完成参考匹配，再保存包含运镜的模板。")
    value["frozen_reference"] = True
    return value


def bind_reference_policy(root, task_id, plan_id, policy):
    if not isinstance(policy, dict) or policy.get("schema") != 1:
        raise ValueError("REFERENCE_POLICY_INVALID: 缺少参考规则")
    shots = policy.get("shots")
    if not isinstance(shots, list) or not 1 <= len(shots) <= 40 or any(float(x.get("duration",0)) <= 0 for x in shots):
        raise ValueError("REFERENCE_POLICY_INVALID: 参考镜头时间无效")
    if policy.get("subtitle",{}).get("max_lines") != 1:
        raise ValueError("REFERENCE_POLICY_INVALID: 当前参考模式要求单行字幕")
    path = Path(root)/"runtime/workbench/tasks"/task_id/"reference-policies"/f"{plan_id}.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    request_digest = hashlib.sha256(json.dumps(policy,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    if path.exists():
        stored=json.loads(path.read_text(encoding="utf-8"))
        if stored.get("request_digest") == request_digest and stored.get("task_id") == task_id and stored.get("plan_id") == plan_id:
            return str(path)
        raise ValueError("REFERENCE_POLICY_IMMUTABLE: 已绑定的参考规则不能覆盖")
    value = {**policy, "task_id":task_id, "plan_id":plan_id, "request_digest":request_digest}
    if policy.get("frozen_reference"):
        freeze_reference_policy(policy)  # Validate measured motion; reuse without reopening the reference.
    else:
        source = Path(policy["reference_source"])
        with source.open("rb") as handle:
            if hashlib.file_digest(handle,"sha256").hexdigest() != policy["reference_sha256"]:
                raise ValueError("REFERENCE_SOURCE_CHANGED: 参考视频已发生变化")
        value["shots"] = [{**shot,"motion":detect_camera_motion(source,shot["start"],shot["end"])} for shot in policy["shots"]]
    content = json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n"
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise ValueError("REFERENCE_POLICY_IMMUTABLE: 已绑定的参考规则不能覆盖")
    path.write_text(content,encoding="utf-8")
    return str(path)


def split_intro_cues(original, voice_path=None):
    """Display-only intro/list splitting; retain locked copy and whole-cue audio bounds.

    Pause-assisted boundaries are estimates, not recognized word timestamps.
    """
    cues = []
    for row in original:
        intro = re.search(r"(?<!\d)[：:](?![/\\])", row["text"])
        if not intro:
            cues.append(dict(row))
            continue
        pieces = [row["text"][:intro.end()]] + [x for x in re.split(r"(?<=[、，,])",row["text"][intro.end():]) if x.strip()]
        if len(pieces) < 2:
            cues.append(dict(row))
            continue
        weights = [max(1,len(re.sub(r"[\s，。！？、；：,.!?;:]","",x))) for x in pieces]
        duration = row["end"]-row["start"]
        pauses = []
        if voice_path:
            result = subprocess.run(["ffmpeg","-hide_banner","-ss",str(row["start"]),"-t",str(duration),
                                     "-i",str(voice_path),"-af","silencedetect=noise=-32dB:d=0.16","-f","null","-"],
                                    capture_output=True,text=True,timeout=30)
            silence_start = None
            for line in result.stderr.splitlines():
                match = re.search(r"silence_start: ([0-9.]+)",line)
                if match: silence_start = float(match[1])
                match = re.search(r"silence_end: ([0-9.]+)",line)
                if match and silence_start is not None:
                    pauses.append((silence_start+float(match[1]))/2)
                    silence_start = None
        boundaries = [0.0]
        cumulative = 0
        used_pause = False
        for index, weight in enumerate(weights[:-1]):
            cumulative += weight
            expected = duration*cumulative/sum(weights)
            lower, upper = boundaries[-1]+.12, duration-.12*(len(pieces)-index-1)
            available = [x for x in pauses if lower < x < upper and abs(x-expected) <= .8]
            cut = min(available,key=lambda x:abs(x-expected)) if available else max(lower,min(expected,upper))
            used_pause = used_pause or bool(available)
            boundaries.append(cut)
        boundaries.append(duration)
        for index, text in enumerate(pieces):
            start, end = row["start"]+boundaries[index],row["start"]+boundaries[index+1]
            cues.append({**row,"text":text,"start":start,"end":end,"dur":end-start,
                         "sourceCueText":row["text"],"sourceCueIndex":row.get("i"),"subtitleSemanticBoundary":True,
                         "subtitleTimingMode":"audio-pause-assisted" if used_pause else "phrase-proportional"})
    return cues


def motion_windows(root, asset_path, desired_duration, probe, *, allowed_start=0.0, allowed_end=None):
    duration = float(probe.get("duration", 0))
    explicit = allowed_end is not None
    if not explicit and (duration <= desired_duration or not asset_path.is_file()):
        return [(0.0, probe)]
    # Whole-file authorization does not mean every scene has the same subject.
    # Without a reviewed segment, stay near the originally used opening.
    limit = min(duration, float(allowed_end) if explicit else max(12.0, desired_duration))
    start = max(0.0, float(allowed_start))
    if limit-start < desired_duration-.001:
        return []
    last = max(start, limit-desired_duration)
    starts = sorted({round(start,3), round((start+last)/2,3), round(last,3)})
    stat = asset_path.stat()
    cache = Path(root)/"runtime/workbench/motion-cache"
    cache.mkdir(parents=True, exist_ok=True)
    result=[]
    for start in starts:
        key=hashlib.sha256(f"v1|{asset_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{start}|{desired_duration:.3f}".encode()).hexdigest()
        path=cache/(key+".json")
        try:
            motion=json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            motion=detect_camera_motion(asset_path,start,start+desired_duration)
            temporary=path.with_suffix(".tmp")
            temporary.write_text(json.dumps(motion),encoding="utf-8")
            temporary.replace(path)
        result.append((start,motion))
    return result


def apply_reference_policy(root, task_id, plan_id, original, preset, asset_paths, *, db=None):
    path = Path(root)/"runtime/workbench/tasks"/task_id/"reference-policies"/f"{plan_id}.json"
    if not path.is_file() and db is not None:
        from .packaging_plan import reference_policy_for_plan
        saved = reference_policy_for_plan(db, plan_id)
        if saved:
            bind_reference_policy(root, task_id, plan_id, saved)
    if not path.is_file():
        return None
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("task_id") != task_id or policy.get("plan_id") != plan_id:
        raise ValueError("REFERENCE_POLICY_BINDING_INVALID")
    style = {**preset, **policy["subtitle"]}
    style.pop("ass_style",None)
    def color(hex):
        value=hex.lstrip("#")
        return "&H00"+value[4:6]+value[2:4]+value[0:2]
    style["ass_style"] = f"Style: {style['id']},{style['fontname_guess']},{style['size']},{color(style['primary_hex'])},{color(style['primary_hex'])},{color(style['outline_hex'])},&H80000000,-1,0,0,0,100,100,0,0,1,{style['outline']},{style['shadow']},2,60,60,{style['margin_v']},1"
    from .packaging_plan import find_item
    number_template = find_item("fancy_text",policy.get("number_preset"))
    if not number_template or number_template.get("license") != "commercial-approved":
        raise ValueError("REFERENCE_NUMBER_TEMPLATE_UNAVAILABLE")
    timing_path = Path(root)/"runtime/workbench/tasks"/task_id/"artifacts/ad/voice.mp3"
    cues = split_intro_cues(original, timing_path if timing_path.is_file() else None)
    from .emphasis_packaging import split_emphasis_cues, make_emphasis
    if policy.get("emphasis",{}).get("enabled"):
        cues=split_emphasis_cues(cues,timing_path if timing_path.is_file() else None)
    events = layout_ad_subtitles(cues, style, policy["ratio"])
    # Captions may split an intro/list independently of the unchanged TTS phrase.
    for event in events:
        event["displayText"] = event["text"].strip().strip("，。！？、；：,.!?;:")
        event["subtitleStart"],event["subtitleEnd"] = event["start"],event["end"]
        event["displaySize"] = min(style["size"], round(policy["width"]*.84/max(1,len(event["displayText"]))))
        event["numberPreset"] = policy.get("number_preset")
        event["numberTemplate"] = {"id":number_template["id"],"entrance":number_template["entrance"],"fill":style["primary_hex"],"outline":style["outline_hex"]}
    emphasis_overlays, emphasis_report = make_emphasis(root,task_id,plan_id,events,policy)
    total = original[-1]["end"]
    reference_duration = sum(shot["duration"] for shot in policy["shots"])
    candidates = []
    confirmed_paths = {str(Path(row["src"]).resolve()) for row in original}
    for asset_id, asset_path in asset_paths.items():
        if str(asset_path.resolve()) not in confirmed_paths:
            continue
        if asset_path.suffix.lower() not in {".mp4",".mov",".mkv",".avi",".m4v"}:
            continue
        motion = detect_camera_motion(asset_path,0, min(12,total))
        original_windows = [row for row in original if str(Path(row["src"]).resolve()) == str(asset_path.resolve())]
        bounded = [(float(row.get("sourceStart",0)), float(row["sourceEnd"])) for row in original_windows if row.get("sourceEnd") is not None]
        candidates.append({"asset_id":asset_id,"path":str(asset_path),"motion":motion,"bounds":bounded})
    if not candidates:
        raise ValueError("REFERENCE_NO_AUTHORIZED_VIDEO: 已授权素材中无可用视频")
    used = set()
    start = 0
    clips,report = [],[]
    for index,shot in enumerate(policy["shots"]):
        end = total if index == len(policy["shots"])-1 else start+shot["duration"]/reference_duration*total
        desired = shot.get("motion",{}).get("type","unknown")
        narration = "".join(row["text"] for row in original if row["start"] < end and row["end"] > start)
        def score(item):
            measured = item["motion"]["type"]
            exact = measured == desired and item["motion"]["confidence"] >= .45
            similar = measured.split("_")[0] == desired.split("_")[0]
            duration_ok = item["motion"]["duration"] >= end-start
            overlap = len(set(narration)&set(Path(item["path"]).stem))
            interior = any(term in narration for term in ("层高","承重","行车","一楼","分割"))
            subject = any(term in item["path"] for term in (("内部","室内","一楼") if interior else ("外部","园区","大门","航拍")))
            orientation = (item["motion"].get("width",0) > item["motion"].get("height",0)) == (policy["ratio"] == "16:9")
            return (item["asset_id"] not in used, subject, orientation, duration_ok, exact, similar, overlap, item["asset_id"])
        windows=[]
        for item in candidates:
            bounds = item["bounds"] or [(0.0,None)]
            for allowed_start,allowed_end in bounds:
                for offset,motion in motion_windows(root,Path(item["path"]),end-start,item["motion"],allowed_start=allowed_start,allowed_end=allowed_end):
                    windows.append({**item,"motion":motion,"offset":offset})
        if not windows:
            raise ValueError("REFERENCE_WINDOW_TOO_SHORT: 已确认素材片段不足以覆盖此镜头，请补充或调整选材范围。")
        selected = max(windows,key=score)
        measured = selected["motion"]["type"]
        substitution = "exact" if measured == desired and selected["motion"]["confidence"] >= .45 else "same-motion-family" if measured.split("_")[0] == desired.split("_")[0] else "available-visual"
        loop = selected["motion"]["duration"] < end-start
        repeated = selected["asset_id"] in used
        used.add(selected["asset_id"])
        clips.append({"i":index,"text":narration,"start":start,"end":end,"dur":end-start,"src":selected["path"],"matched":True,
                      "referenceMotion":desired,"measuredMotion":selected["motion"],"substitution":substitution})
        if not loop:
            clips[-1].update(sourceStart=selected["offset"],sourceEnd=selected["offset"]+end-start)
        report.append({"source_start":selected["offset"],"reason":"在已确认片段内比较画幅、时长与粗运镜；未标注片段时仅检查素材开头12秒（至少覆盖本镜时长），避免跳入未知场景", "shot":index+1,"asset_id":selected["asset_id"],"desired":desired,"measured":measured,
                       "confidence":selected["motion"]["confidence"],"substitution":substitution,"short_material_loop":loop,"repeated_asset":repeated})
        start = end
    return {"ratio":policy["ratio"],"emphasisOverlays":emphasis_overlays,"emphasisReport":emphasis_report,"segments":clips,"subtitleEvents":events,"subtitlePreset":style,"transitionMode":"cut",
            "referenceMatch":{"policy":policy,"shots":report,"sourceShotCount":len(policy["shots"]),"selectedUniqueAssets":len(used)},
            "policy_path":str(path)}
