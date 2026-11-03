"""Task-bound, nonpaid local audio jobs; measurements never edit a plan."""
import hashlib
import json
from pathlib import Path
import uuid

from . import packaging_plan as plans
from .packaging_audio_evidence import analyze_audio_file, file_sha256, METHOD_VERSION
from .rough_media import bound_digital_source_location, _available_source
from .packaging_semantics import load_bound_ad_evidence
from .tasks import get_task
from .fingerprint import quick_fingerprint
from .workbench_worker import enqueue_job


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def packaging_audio_sources(db, project_root, task_id, *, verify_content=True):
    """Internal trusted resolvers. Paths are never exposed by state or job payload."""
    task = get_task(db, task_id)
    if not task or task["task_type"] not in {"digital_human", "ad_mix"}:
        raise ValueError("AUDIO_TASK_UNSUPPORTED: 当前任务不支持声音分析")
    brief = task.get("brief") or {}
    source = {"ref": "source:voice", "role": "source_voice", "name": "本片原声音轨",
        "path": None, "problem": "当前任务原声音轨不可读取或绑定已失效",
        "binding": _digest([task["task_type"], brief.get("digital_human_source"), brief.get("final_script")])}
    try:
        if task["task_type"] == "digital_human":
            asset_id = (brief.get("digital_human_source") or {}).get("asset_id")
            location = bound_digital_source_location(db, task_id, asset_id)
            if location:
                source["path"], _ = _available_source(db, asset_id, **location)
                recorded = brief["digital_human_source"]
                if verify_content and (recorded.get("quick_fingerprint") != quick_fingerprint(source["path"])
                        or recorded.get("size_bytes") != source["path"].stat().st_size):
                    source["path"] = None
        else:
            task_root = (Path(project_root).resolve()/"runtime/workbench/tasks"/task_id).resolve()
            if not task_root.is_relative_to((Path(project_root).resolve()/"runtime/workbench/tasks").resolve()):
                raise ValueError("invalid task path")
            from .ad_voice_preview import current_ad_voice_source
            from .voice_ownership import read_voice_ownership
            ownership=read_voice_ownership(db,task_id)
            if ownership.branch!='legacy':
                from .voice_task_context import resolve_voice_task_context
                from .voice_bundle_service import _service,registered_voice_cache_snapshot
                context=resolve_voice_task_context(db,project_root,task_id)
                head=_service(db,project_root,task_id).current(task_id)
                if head['context_digest']!=context.context_digest:raise ValueError('CONTEXT_CHANGED')
                cache=registered_voice_cache_snapshot(db,project_root,task_id,head['audio'],head['config'],context.context_digest)
                source['binding']=_digest([source['binding'],head['revision'],head['context_digest'],cache['bundle_id'],cache['stamp_digest']])
                if verify_content:
                    source['path']=current_ad_voice_source(db,Path(project_root),task).path
                else:
                    source['path']=cache['path']
            else:
                current=current_ad_voice_source(db,Path(project_root),task)
                metadata = json.loads((task_root/"artifacts/ad/recommendation.json").read_text(encoding="utf-8"))
                source["binding"] = _digest([source["binding"], metadata])
                if not verify_content or load_bound_ad_evidence(db, Path(project_root), task_id):
                    source['path']=current.path
        if source["path"]:
            source["problem"] = None
    except (OSError, ValueError, RuntimeError, KeyError):
        source["path"] = None
    result = [source]
    route = plans._canonical_route(db.connection, task_id)
    seen = {source["ref"]}
    # Do not call _item_is_recommendable here: it invokes synchronous ffprobe.
    # Decode/readability decisions belong to the worker, including corrupt files.
    for item in plans.load_manifest("bgm", db).get("items", []):
        ref = item.get("id")
        if (not isinstance(ref, str) or not ref.strip() or ref in seen
                or item.get("license") != "commercial-approved"
                or (item.get("default_for") and route not in item["default_for"])):
            continue
        try:
            path = plans._packaging_path(item, None)
            if path is None or not path.is_file():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        seen.add(ref)
        result.append({"ref": ref, "name": item.get("name", ref), "role": "bgm",
            "path": path, "problem": None, "binding": _digest(item)})
    return result


def _file_stamp(path):
    if path is None:
        return None
    stat = path.stat()
    return [_digest(str(path.resolve())), stat.st_size, stat.st_mtime_ns,
            stat.st_ctime_ns, stat.st_ino]


def _snapshot(db, root, task_id, *, cached_items=None):
    light = cached_items is not None
    # A display poll already proven stale by audio metadata cannot authorize
    # anything. Reject it before hashing unrelated selected video files.
    # Queue/worker checks keep their original picture-lock-first ordering.
    lock = None if light else plans._require_picture_lock(db, db.connection, task_id)
    sources = packaging_audio_sources(db, root, task_id, verify_content=not light)
    previous = {x["ref"]: x for x in (cached_items or [])}
    items = []
    for source in sources:
        sha = None
        stamp = None
        problem = source["problem"]
        try:
            stamp = _file_stamp(source["path"])
            if light:
                prior = previous.get(source["ref"])
                if prior is None or prior.get("file_stamp") != stamp:
                    raise RuntimeError("AUDIO_INPUT_STALE: 声音文件信息已变化")
                sha = prior["source_sha256"]
            elif source["path"] is not None:
                sha = file_sha256(source["path"])
                if _file_stamp(source["path"]) != stamp:
                    raise RuntimeError("AUDIO_INPUT_STALE: 声音文件在读取期间变化")
        except OSError:
            if light:
                raise RuntimeError("AUDIO_INPUT_STALE: 声音文件当前不可读取")
            problem = "音频文件当前不可读取"
        items.append({k: source[k] for k in ("ref", "name", "role", "binding")} |
                     {"source_sha256": sha, "file_stamp": stamp, "problem": problem})
    if light:
        lock = plans._require_picture_lock(db, db.connection, task_id)
    return {"digest": _digest({"method": METHOD_VERSION, "lock": lock, "items": items}),
            "items": items, "sources": sources}


def _latest(db, task_id):
    return db.connection.execute("SELECT j.job_id,j.status,j.cancel_requested,r.input_digest,r.items_json "
        "FROM jobs j JOIN packaging_audio_jobs r USING(job_id) WHERE j.task_id=? "
        "AND j.job_type='packaging_audio_analysis' ORDER BY j.rowid DESC LIMIT 1", (task_id,)).fetchone()


class _AlreadyQueued(RuntimeError):
    def __init__(self, job_id):
        self.job_id = job_id


def queue_packaging_audio(db, project_root, task_id):
    snapshot = _snapshot(db, project_root, task_id)
    job_id = "audio-" + uuid.uuid4().hex
    initial = [{**item, "status": "pending", "evidence": None} for item in snapshot["items"]]
    def check(connection):
        plans._require_picture_lock(db, connection, task_id)
        previous = _latest(db, task_id)
        if previous and previous["status"] in {"queued", "running"}:
            if previous["input_digest"] == snapshot["digest"] and not previous["cancel_requested"]:
                raise _AlreadyQueued(previous["job_id"])
            raise RuntimeError("AUDIO_JOB_PENDING: 上一次声音分析尚未结束，请等待或取消后重试")
        connection.execute("INSERT INTO packaging_audio_jobs(job_id,task_id,input_digest,items_json) VALUES(?,?,?,?)",
            (job_id, task_id, snapshot["digest"], json.dumps(initial, ensure_ascii=False)))
    try:
        enqueue_job(db, job_id=job_id, job_type="packaging_audio_analysis", task_id=task_id,
            payload={"task_id": task_id, "input_digest": snapshot["digest"]},
            cost_status="waived", pre_enqueue_check=check)
    except _AlreadyQueued as existing:
        job_id = existing.job_id
    status = db.connection.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
    return {"jobId": job_id, "status": status, "totalCount": len(initial)}


def _store_items(db, job_id, items):
    with db.transaction() as connection:
        connection.execute("UPDATE packaging_audio_jobs SET items_json=?,updated_at=datetime('now') WHERE job_id=?",
            (json.dumps(items, ensure_ascii=False, allow_nan=False), job_id))


def _stale_items(items):
    return [{**item, "status": "stale", "evidence": None,
             "problem": "任务声音、音乐素材或画面锁已变化，请重新分析"} for item in items]


def _public_items(items):
    return [{k: item.get(k) for k in ("ref", "name", "role", "status", "evidence", "problem")} for item in items]


def packaging_audio_state(db, project_root, task_id, *, verify_content=True):
    row = _latest(db, task_id)
    if row is None:
        return {"status": "not_requested", "completedCount": 0, "failedCount": 0, "totalCount": 0, "items": []}
    items = json.loads(row["items_json"])
    status = row["status"]
    try:
        if _snapshot(db, project_root, task_id, cached_items=None if verify_content else items)["digest"] != row["input_digest"]:
            status = "stale"
    except (OSError, RuntimeError, ValueError, KeyError):
        status = "stale"
    if status == "stale" or any(x["status"] == "stale" for x in items):
        status, items = "stale", _stale_items(items)
    completed = sum(x["status"] == "completed" for x in items)
    failed = sum(x["status"] == "failed" for x in items)
    if status == "completed":
        status = "completed" if completed == len(items) else "partial" if completed else "failed"
    return {"status": status, "jobId": row["job_id"],
        "verification": "content_sha256" if verify_content else "file_metadata_only",
        "completedCount": completed,
        "failedCount": failed, "totalCount": len(items), "items": _public_items(items)}


def run_packaging_audio(db, job, project_root):
    if not job.task_id or job.payload.get("task_id") != job.task_id:
        raise ValueError("TASK_ID_MISMATCH: 声音分析任务绑定不一致")
    row = db.connection.execute("SELECT input_digest,items_json FROM packaging_audio_jobs WHERE job_id=? AND task_id=?",
        (job.job_id, job.task_id)).fetchone()
    if row is None or row["input_digest"] != job.payload.get("input_digest"):
        raise RuntimeError("AUDIO_RECEIPT_MISSING: 声音分析请求绑定缺失")
    items = json.loads(row["items_json"])
    try:
        snapshot = _snapshot(db, project_root, job.task_id)
        if snapshot["digest"] != row["input_digest"]:
            raise RuntimeError("STALE_INPUT")
    except (OSError, RuntimeError, ValueError, KeyError):
        _store_items(db, job.job_id, _stale_items(items))
        return {"cursor": "packaging_audio:stale", "progress": {"current": 0, "total": len(items)},
            "result": {"status": "stale"}, "phase_label": "声音分析输入已变化"}
    by_ref = {x["ref"]: x for x in snapshot["sources"]}
    cancelled = False
    for index, item in enumerate(items):
        control = db.connection.execute("SELECT cancel_requested FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
        if not control or control["cancel_requested"]:
            cancelled = True
            break
        source = by_ref[item["ref"]]
        if source["path"] is None or item["source_sha256"] is None:
            items[index] = {**item, "status": "failed", "problem": item["problem"] or "音频文件当前不可读取", "evidence": None}
            _store_items(db, job.job_id, items)
            continue
        items[index] = {**item, "status": "running", "evidence": None}
        _store_items(db, job.job_id, items)
        evidence = analyze_audio_file(db, source["path"])
        if evidence.get("source_sha256") != item["source_sha256"]:
            _store_items(db, job.job_id, _stale_items(items))
            break
        items[index] = {**item, "status": evidence["status"],
            "problem": evidence.get("error"), "evidence": evidence if evidence["status"] == "completed" else None}
        _store_items(db, job.job_id, items)
    # The read model rechecks the entire binding, including sources measured earlier.
    state = packaging_audio_state(db, project_root, job.task_id)
    if state["status"] == "stale":
        _store_items(db, job.job_id, _stale_items(items))
    return {"cursor": "packaging_audio:cancelled" if cancelled else "packaging_audio:complete",
        "progress": {"current": state["completedCount"]+state["failedCount"], "total": len(items)},
        "phase_label": "声音分析已停止" if cancelled else ("声音分析输入已变化" if state["status"] == "stale" else
            "声音分析完成" if not state["failedCount"] else "部分声音分析失败"),
        "result": {"completedCount": state["completedCount"], "failedCount": state["failedCount"], "totalCount": len(items)}}
