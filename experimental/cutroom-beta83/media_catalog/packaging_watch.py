"""Server-clock preview viewing, bound to immutable picture/plan/output evidence."""
import json
import math
import uuid
import hashlib
from pathlib import Path
from .full_review import (_now, _stamp, _parse_stamp, COVERAGE_THRESHOLD,
    PLAYBACK_RATE_LIMIT, MAX_CONTIGUOUS_TICK_SECONDS,
    END_TOLERANCE_SECONDS)


def install_schema(db):
    # Additive and repeatable for existing installations; old client-only marks
    # are not transferable to the new server-observed gate.
    with db.transaction(immediate=True) as conn:
        exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='packaging_watch_sessions'").fetchone()
        conn.execute("CREATE TABLE IF NOT EXISTS packaging_watch_sessions (session_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), preview_id TEXT NOT NULL, identity TEXT NOT NULL, status TEXT NOT NULL, state_json TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS packaging_watch_preview ON packaging_watch_sessions(task_id,preview_id,status)")
        if not exists and conn.execute("SELECT 1 FROM sqlite_master WHERE name='packaging_preview_artifacts'").fetchone():
            conn.execute("UPDATE packaging_preview_artifacts SET viewed_at=NULL")


def _current(db, task_id, preview_id, root, *, full=True):
    from .packaging_preview import get_preview_artifact, current_preview_matches, resolve_packaging_preview_media, _receipt_assets_current
    p=get_preview_artifact(db,preview_id)
    if not p or p["task_id"]!=task_id or p["status"]!="ready":
        raise RuntimeError("PACKAGING_PREVIEW_NOT_READY: 当前预览尚未完成")
    if full:
        if not current_preview_matches(db,p) or not _receipt_assets_current(p):
            raise RuntimeError("PACKAGING_PREVIEW_STALE: 画面或包装方案已变化")
        resolve_packaging_preview_media(db,preview_id,root)
    return p


def _db_version(db, p):
    """Small current DB values only: no compilation, media reads or hashing files."""
    c=db.connection;task=p["task_id"];plan=p.get("plan_id")
    queries=[("SELECT title,brief_json FROM tasks WHERE task_id=?",(task,)),
        ("SELECT preview_id,canonical_key,status,output_signature_json,duration_seconds,rel_path FROM packaging_preview_artifacts WHERE preview_id=?",(p["preview_id"],)),
        ("SELECT * FROM picture_lock WHERE task_id=?",(task,)),
        ("SELECT * FROM scene_cards WHERE task_id=? ORDER BY card_id",(task,)),
        ("SELECT * FROM packaging_plans WHERE plan_id=?",(plan,)),
        ("SELECT * FROM packaging_plan_items WHERE plan_id=? ORDER BY rowid",(plan,)),
        ("SELECT * FROM packaging_plan_instances WHERE plan_id=? ORDER BY rowid",(plan,)),
        ("SELECT ta.asset_id,ta.role,a.quick_fingerprint,al.relative_path,al.state,l.mount_path,l.online FROM task_assets ta JOIN assets a USING(asset_id) LEFT JOIN asset_locations al USING(asset_id) LEFT JOIN libraries l USING(library_id) WHERE ta.task_id=? ORDER BY ta.asset_id,al.location_id",(task,))]
    values=[[[v for v in row] for row in c.execute(q,args)] for q,args in queries]
    return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _snapshot(db,p,root):
    from .packaging_preview import _resolve_cache_path
    paths={str(a["path"]) for a in (p.get("preview_receipt") or {}).get("asset_versions",[])}
    if p.get("rel_path"):paths.add(str(_resolve_cache_path(Path(root),p["rel_path"])))
    if len(paths)>512:raise RuntimeError("WATCH_SOURCE_LIMIT: 预览依赖数量超出观看校验范围")
    stamps=[]
    for path in sorted(paths):
        try:
            st=Path(path).stat()
        except OSError as e:raise RuntimeError("WATCH_CANDIDATE_CHANGED: 预览或源素材已不可访问") from e
        stamps.append([path,st.st_size,st.st_mtime_ns,st.st_ctime_ns,st.st_ino,st.st_dev])
    return {"database":_db_version(db,p),"files":stamps}


def _full_snapshot(db,task_id,preview_id,root):
    before=_snapshot(db,_current(db,task_id,preview_id,root,full=False),root)
    p=_current(db,task_id,preview_id,root)
    after=_snapshot(db,p,root)
    if before!=after:raise RuntimeError("WATCH_CANDIDATE_CHANGED: 校验期间预览输入已变化")
    return p,after


def _identity(p):
    return json.dumps([p["canonical_key"],p["output_signature"],p["duration_seconds"]],sort_keys=True)


def _result(s, sid):
    ratio=min(s["covered"]/s["duration"],1)
    return {"sessionId":sid,"coveredSeconds":s["covered"],"coverageRatio":ratio,
        "reachedEnd":s["ended"],"canApprove":s["ended"] and ratio>=COVERAGE_THRESHOLD,
        "sequence":s["seq"]}


def start_preview_watch(db,task_id,preview_id,root,*,clock=None):
    p,guard=_full_snapshot(db,task_id,preview_id,root)
    with db.transaction(immediate=True) as conn:
        if _db_version(db,p)!=guard["database"]:raise RuntimeError("WATCH_CANDIDATE_CHANGED")
        duration=float(p["duration_seconds"])
        if not math.isfinite(duration) or duration<=0:raise ValueError("WATCH_DURATION_INVALID")
        now=_stamp(_now(clock));sid="pwatch-"+uuid.uuid4().hex
        s={"duration":duration,"started":now,"last":now,"position":0,"credit":0,
           "guard":guard,"playing":False,"seq":0,"covered":0,"ended":False,"tail":False,"intervals":[],"payload":None}
        conn.execute("UPDATE packaging_watch_sessions SET status='superseded' WHERE task_id=? AND preview_id=? AND status='active'",(task_id,preview_id))
        conn.execute("INSERT INTO packaging_watch_sessions VALUES(?,?,?,?,?,?)",(sid,task_id,preview_id,_identity(p),"active",json.dumps(s)))
    return _result(s,sid)


def _load(conn, task_id, preview_id, sid, p, now):
    row=conn.execute("SELECT * FROM packaging_watch_sessions WHERE session_id=? AND task_id=? AND preview_id=?",(sid,task_id,preview_id)).fetchone()
    if not row:raise RuntimeError("FULL_WATCH_REQUIRED: 缺少当前预览观看记录")
    if row["identity"]!=_identity(p):raise RuntimeError("WATCH_CANDIDATE_CHANGED: 预览版本已改变")
    if row["status"] not in {"active","completed"}:raise RuntimeError("WATCH_SESSION_INACTIVE: 观看已被新会话替代")
    s=json.loads(row["state_json"])
    if row["status"]!="completed" and (now-_parse_stamp(s["started"])).total_seconds()>7200:
        raise RuntimeError("WATCH_SESSION_EXPIRED: 请重新观看当前预览")
    return row,s


def _merge(intervals,start,end):
    merged=[]
    for a,b in sorted([*intervals,[start,end]]):
        if not merged or a>merged[-1][1]:merged.append([a,b])
        else:merged[-1][1]=max(merged[-1][1],b)
    return merged


def tick_preview_watch(db,task_id,preview_id,sid,sequence,position,event,root,*,clock=None):
    if type(sequence) is not int or sequence<1:raise ValueError("WATCH_SEQUENCE_INVALID")
    if type(position) not in {int,float} or not math.isfinite(position) or position<0:raise ValueError("INVALID_WATCH_POSITION")
    if event not in {"playing","tick","pause","seeking","seeked","ended","waiting"}:raise ValueError("WATCH_EVENT_INVALID")
    now=_now(clock)
    p=_current(db,task_id,preview_id,root,full=False)
    guard=_snapshot(db,p,root)
    with db.transaction(immediate=True) as conn:
        if _db_version(db,p)!=guard["database"]:raise RuntimeError("WATCH_CANDIDATE_CHANGED")
        row,s=_load(conn,task_id,preview_id,sid,p,now)
        if s.get("guard")!=guard:raise RuntimeError("WATCH_CANDIDATE_CHANGED: 画面、方案或源文件已改变")
        payload=[sequence,position,event]
        if payload==s["payload"]:return _result(s,sid)
        if sequence!=s["seq"]+1:raise ValueError("WATCH_SEQUENCE_INVALID: 观看事件顺序已变化")
        if row["status"]!="active":raise RuntimeError("WATCH_SESSION_INACTIVE")
        if position>s["duration"]+END_TOLERANCE_SECONDS:raise ValueError("INVALID_WATCH_POSITION")
        position=min(position,s["duration"]);delta=position-s["position"]
        elapsed=max(0,(now-_parse_stamp(s["last"])).total_seconds())
        allowed=min(elapsed*PLAYBACK_RATE_LIMIT+s["credit"],MAX_CONTIGUOUS_TICK_SECONDS)
        accepted=s["playing"] and event not in {"seeking","seeked","playing"} and 0<delta<=allowed
        if accepted:
            s["intervals"]=_merge(s["intervals"],s["position"],position)
            s["covered"]=sum(b-a for a,b in s["intervals"])
            s["tail"]=position>=s["duration"]-END_TOLERANCE_SECONDS
        # Native pause may report the last decoded frame just before ended reports
        # the container duration (e.g. 22.593 vs 22.596). Preserve the observed
        # tail across that sub-frame drain; seeking clears tail and cannot pass.
        final_frame_drain = 0 <= delta <= 0.04 and (s.get("payload") or [None, None, None])[2] == "pause"
        if event=="ended" and s.get("tail") and position>=s["duration"]-END_TOLERANCE_SECONDS and (accepted or delta==0 or final_frame_drain):s["ended"]=True
        if event in {"seeking","seeked"}:s["tail"]=False
        # Preserve server-observed time while ordered requests catch up after
        # transport latency. Capping at jitter discarded valid playing time.
        # The existing per-tick bound remains; seek/pause/resume cannot bank it.
        s["credit"]=max(0,allowed-delta) if accepted else 0
        if event=="playing":s["playing"]=True
        elif event!="tick":s["playing"]=False
        s.update(last=_stamp(now),position=position,seq=sequence,payload=payload)
        conn.execute("UPDATE packaging_watch_sessions SET state_json=? WHERE session_id=?",(json.dumps(s),sid))
    return _result(s,sid)


def complete_preview_watch(db,task_id,preview_id,sid,root,*,clock=None):
    p,guard=_full_snapshot(db,task_id,preview_id,root)
    with db.transaction(immediate=True) as conn:
        if _db_version(db,p)!=guard["database"]:raise RuntimeError("WATCH_CANDIDATE_CHANGED")
        row,s=_load(conn,task_id,preview_id,sid,p,_now(clock))
        if s.get("guard")!=guard:raise RuntimeError("WATCH_CANDIDATE_CHANGED: 画面、方案或源文件已改变")
        result=_result(s,sid)
        if not result["canApprove"]:raise RuntimeError("FULL_WATCH_REQUIRED: 请连续观看当前预览并播放至片尾")
        conn.execute("UPDATE packaging_watch_sessions SET status='completed' WHERE session_id=?",(sid,))
    return result


def has_completed_preview_watch(db,preview):
    rows=db.connection.execute("SELECT * FROM packaging_watch_sessions WHERE task_id=? AND preview_id=? AND identity=? AND status='completed'",(preview["task_id"],preview["preview_id"],_identity(preview)))
    return any(_result(json.loads(r["state_json"]),r["session_id"])["canApprove"] for r in rows)
