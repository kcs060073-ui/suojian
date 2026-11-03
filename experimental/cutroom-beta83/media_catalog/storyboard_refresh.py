"""Refresh unconfirmed automatic material choices without replanning narration."""
import copy
import json
import re
import uuid
import hashlib
from pathlib import Path
from .ad_matching import load_observed_ad_windows, match_task_ad_shots
from .task_sources import list_eligible_assets, require_source_readiness
from .script_segments import storyboard_digest
from .storyboard_edits import _editable, _candidate, _validate, _write_state, _canonical
from .rough_cut import _snapshot_state, _advance_accepted_storyboard
from .workflow import project_workflow


def _input_snapshot(db, task_id):
    """DB-only optimistic lock; deliberately excludes worker leases and heartbeats."""
    connection=db.connection
    task_tables=("tasks","scene_cards","task_assets","storyboard_feedback","storyboard_edits",
        "script_segments","picture_lock","task_source_scopes","task_source_settings",
        "task_source_snapshots","task_source_checks","task_stage_states","task_gate_events","rough_cut_edits",
        "rework_requests","render_snapshots","qa_reports","candidate_versions",
        "packaging_plans","publish_records","content_review_runs","review_approvals")
    rows={table:[dict(r) for r in connection.execute(
        f"SELECT * FROM {table} WHERE task_id=? ORDER BY rowid",(task_id,))] for table in task_tables}
    rows["task_job_status"]=[dict(r) for r in connection.execute(
        "SELECT job_id,job_type,status,cancel_requested FROM jobs WHERE task_id=? ORDER BY job_id",(task_id,))]
    rows["source_check_status"]=[dict(r) for r in connection.execute(
        "SELECT j.job_id,j.status FROM task_source_checks c JOIN jobs j ON j.job_id=c.job_id WHERE c.task_id=? ORDER BY j.job_id",(task_id,))]
    from .task_sources import _active_scope_rows, _is_within_scope
    scopes=[dict(r) for r in _active_scope_rows(db,task_id)]
    rows["libraries"]=[dict(r) for r in connection.execute("SELECT * FROM libraries ORDER BY library_id")]
    locations=[dict(r) for r in connection.execute("SELECT * FROM asset_locations ORDER BY location_id")]
    ids={r["asset_id"] for r in rows["task_assets"]}
    ids.update(r["asset_id"] for r in rows["scene_cards"] if r.get("asset_id"))
    ids.update(r["asset_id"] for r in locations if any(r["library_id"]==scope["library_id"]
        and _is_within_scope(r["relative_path"],scope["relative_path"]) for scope in scopes))
    # Duplicate evidence can consult the other source outside this task's scope.
    duplicate_rows=[dict(r) for r in connection.execute("SELECT * FROM duplicate_evidence ORDER BY rowid")
        if r["left_asset_id"] in ids or r["right_asset_id"] in ids]
    ids.update(r[k] for r in duplicate_rows for k in ("left_asset_id","right_asset_id"))
    rows["duplicate_evidence"]=duplicate_rows
    rows["asset_locations"]=[r for r in locations if r["asset_id"] in ids]
    for table in ("assets","segments","analysis_runs","visual_observations",
                  "catalog_segment_marks","quality_evidence_runs","privacy_flags"):
        rows[table]=[]
        ordered=sorted(ids)
        for i in range(0,len(ordered),400):
            batch=ordered[i:i+400]
            rows[table].extend(dict(r) for r in connection.execute(
                f"SELECT * FROM {table} WHERE asset_id IN ({','.join('?' for _ in batch)}) ORDER BY rowid",batch))
    # Path resolution/stat is separate, never a full media read under a write lock.
    libraries={r["library_id"]:r for r in rows["libraries"]}
    paths={str(Path(libraries[r["library_id"]]["mount_path"])/r["relative_path"])
        for r in rows["asset_locations"] if r["library_id"] in libraries}
    paths.update(str(Path(scope["mount_path"])/scope["relative_path"]) for scope in scopes)
    if len(paths)>4096:
        raise RuntimeError("STORYBOARD_REFRESH_SCOPE_LIMIT: 请缩小本次素材范围后刷新")
    return hashlib.sha256(_canonical(rows).encode()).hexdigest(),sorted(paths)


def _file_snapshot(paths):
    result=[]
    for name in paths:
        try:
            path=Path(name);stat=path.stat()
            result.append((name,str(path.resolve()),stat.st_size,stat.st_mtime_ns,
                           stat.st_ctime_ns,stat.st_ino,stat.st_dev))
        except OSError:
            result.append((name,None))
    return result


def _content_snapshot(paths):
    # Same bounded source fingerprint used by the observation catalog; outside
    # the write transaction only. File stats close the final commit window.
    from .fingerprint import quick_fingerprint
    result=[]
    for name in paths:
        try:
            path=Path(name)
            result.append((name,quick_fingerprint(path) if path.is_file() else None))
        except OSError:
            result.append((name,None))
    return result


def _unchanged(db,task_id,expected,files):
    current=_input_snapshot(db,task_id)
    if current!=expected or _file_snapshot(current[1])!=files:
        raise RuntimeError("STORYBOARD_REFRESH_CHANGED: 刷新期间任务、人工选择或素材发生变化，请重新刷新")


def rough_refresh_blocker(db, task_id):
    """Read-only capability guard; applying also rechecks stage, digest and sources."""
    if db.connection.execute("SELECT 1 FROM picture_lock WHERE task_id=?",(task_id,)).fetchone():
        return "STORYBOARD_LOCKED: 画面已锁定，请从粗剪修改"
    for table in ("packaging_plans", "render_snapshots", "qa_reports", "candidate_versions",
                  "publish_records", "content_review_runs", "review_approvals"):
        if db.connection.execute(f"SELECT 1 FROM {table} WHERE task_id=? LIMIT 1",(task_id,)).fetchone():
            return "STORYBOARD_REFRESH_DOWNSTREAM: 已有包装或成片成果，请使用受控返工修改画面"
    if db.connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND status IN ('pending','queued','running') LIMIT 1",(task_id,)).fetchone():
        return "STORYBOARD_REFRESH_BUSY: 本任务仍有后台作业，请等待结束后更新选材"
    return None


def _refresh_editable(db, task_id, expected_digest):
    try:
        _editable(db, task_id, expected_digest)
        return False
    except RuntimeError as error:
        if not str(error).startswith("STORYBOARD_STAGE:"):
            raise
        if project_workflow(db,task_id)["current_stage"] not in {"rough_cut", "picture_order", "picture_lock"}:
            raise
    blocker=rough_refresh_blocker(db,task_id)
    if blocker:
        raise RuntimeError(blocker)
    if storyboard_digest(db,task_id) != expected_digest:
        raise RuntimeError("STORYBOARD_CHANGED: 分镜在您查看后已变化，请刷新核对")
    require_source_readiness(db,task_id)
    return True


def refresh_storyboard_materials(db, task_id, expected_digest):
    if db.connection.in_transaction:
        raise RuntimeError("STORYBOARD_REFRESH_TRANSACTION: 刷新必须在独立事务外准备")
    connection=db.connection
    expected=_input_snapshot(db,task_id)
    files=_file_snapshot(expected[1])
    contents=_content_snapshot(expected[1])
    _unchanged(db,task_id,expected,files)
    rough_refresh = _refresh_editable(db, task_id, expected_digest)
    task=connection.execute('SELECT task_type,brief_json FROM tasks WHERE task_id=?',(task_id,)).fetchone()
    if task['task_type']!='ad_mix':
        raise ValueError('STORYBOARD_REFRESH_ROUTE: 本次选材刷新适用于广告短句分镜')
    before=_snapshot_state(connection,task_id)
    after=copy.deepcopy(before)
    cards=after['cards']
    feedback={r[0] for r in connection.execute('SELECT card_id FROM storyboard_feedback WHERE task_id=?',(task_id,))}
    manually_edited=set()
    for edit in connection.execute("SELECT before_json,after_json FROM storyboard_edits WHERE task_id=? AND status='applied' AND edit_id NOT LIKE 'sbrefresh-%'",(task_id,)):
        old={c['card_id']:c for c in json.loads(edit['before_json'])['cards']}
        for c in json.loads(edit['after_json'])['cards']:
            if c['card_id'] not in old or any(c.get(k)!=old[c['card_id']].get(k) for k in ('asset_id','segment_id','enter_tc','exit_tc','mix_form','narrative_start','narrative_end')):
                manually_edited.add(c['card_id'])
    # Only applied manual rough edits protect cards. Earlier automatic refreshes
    # remain eligible for newer analysis, and reverted edits are no longer active.
    for edit in connection.execute("SELECT before_json,after_json FROM rough_cut_edits WHERE task_id=? AND status='applied' AND edit_id NOT LIKE 'rough-refresh-%'",(task_id,)):
        old={c['card_id']:c for c in json.loads(edit['before_json'])['cards']}
        for c in json.loads(edit['after_json'])['cards']:
            if c['card_id'] not in old or any(c.get(k)!=old[c['card_id']].get(k) for k in
                    ('asset_id','segment_id','enter_tc','exit_tc','mix_form','idx','narrative_start','narrative_end')):
                manually_edited.add(c['card_id'])
    protected=[c for c in cards if c['card_id'] in manually_edited or c.get('confirm_status') or c.get('final_decision') or
               c.get('source_type')=='用户指定' or c['card_id'] in feedback]
    editable=[c for c in cards if c not in protected]
    if not editable:
        return {'changedCount':0,'preservedCount':len(protected),'secondaryCount':0,
                'storyboardDigest':expected_digest}
    eligible_assets=list_eligible_assets(db,task_id)
    source_modes={item['asset_id']:item.get('source_mode') for item in eligible_assets}
    candidates=load_observed_ad_windows(db,eligible_assets)
    # Do not duplicate any protected source. A user choice has precedence.
    protected_assets={c['asset_id'] for c in protected if c.get('asset_id')}
    candidates=[c for c in candidates if c['asset_id'] not in protected_assets]
    plan=[];timing=[]
    for card in editable:
        query=re.search(r'画面需求：([^；]+)',card.get('system_suggestion') or '')
        plan.append({'query':query.group(1) if query else card['narrative_text'],
                     'text':card['narrative_text'],
                     'type':'image' if card.get('mix_form')=='信息卡' else 'video'})
        timing.append({'dur':card['narrative_end']-card['narrative_start']})
    from .motion_ranking import context_for_cards
    motion_context=context_for_cards(db,after,editable)
    matches=match_task_ad_shots(db,task_id,plan,timing,candidates,json.loads(task['brief_json'])['aspect_ratio'],
                              motion_context=motion_context)
    changed=0;secondary=0
    for card,planned,match in zip(editable,plan,matches):
        old=copy.deepcopy(card)
        if match is None:
            from .ad_matching import _requires_project_source, _GENERIC_SOURCES
            # Preserve unmatched usable selections, but remove obsolete automatic
            # illustration choices from an explicit project-fact slot.
            if not (_requires_project_source(planned)
                    and source_modes.get(card.get('asset_id')) in _GENERIC_SOURCES):
                continue
            card.update(asset_id=None,segment_id=None,enter_tc=None,exit_tc=None,
                audio_policy_draft=None,source_type='缺口替代',final_decision=None,
                system_suggestion='画面需求：'+planned['query']+
                    '；原自动推荐来自通用示意，不能证明本项目实拍；请补充项目素材或明确修改用途')
        else:
            _candidate(db,task_id,after,card,match['asset_id'],match.get('segment_id'),match.get('source_start'))
            evidence=match.get('match_evidence',{})
            is_secondary=evidence.get('tier')=='park_secondary'
            card.update(source_type='缺口替代' if is_secondary else '系统推荐',final_decision=None,
                system_suggestion='画面需求：'+planned['query']+'；画面依据：'+match['title']+' '+match['description']+'；'+
                  evidence.get('reason','精准内容匹配，已核对方向和取用时长'))
            if is_secondary:secondary+=1
        comparable=lambda c:{k:v for k,v in c.items() if k not in ('shot_revision','updated_at')}
        if comparable(card)!=comparable(old):
            card['shot_revision']=old['shot_revision']+1
            card['updated_at']=connection.execute("SELECT datetime('now')").fetchone()[0]
            changed+=1
    if changed:
        after['task_assets'].sort(key=lambda a:a['asset_id'])
        _validate(db,task_id,after)
        _unchanged(db,task_id,expected,files)
        _refresh_editable(db,task_id,expected_digest)
        if _content_snapshot(expected[1])!=contents:
            raise RuntimeError("STORYBOARD_REFRESH_CHANGED: 刷新期间源素材内容已变化")
        # Expensive preparation and source verification have finished. No model,
        # matching, workflow projection or content hashing belongs in this lock.
        with db.transaction(immediate=True) as connection:
            _unchanged(db,task_id,expected,files)
            if _snapshot_state(connection,task_id)!=before:
                raise RuntimeError("STORYBOARD_REFRESH_CHANGED: 分镜快照已变化")
            _write_state(connection,task_id,after)
            if rough_refresh:
                # Reuse rough history so its existing undo/redo and later locked
                # picture invalidation apply. This does not approve any new frame.
                _advance_accepted_storyboard(db,task_id)
                connection.execute("UPDATE rough_cut_edits SET status='pending',updated_at=datetime('now') WHERE task_id=? AND status='reverted'",(task_id,))
                edit_id='rough-refresh-'+uuid.uuid4().hex
                impact=hashlib.sha256(_canonical({'before':before,'after':after}).encode()).hexdigest()
                connection.execute("INSERT INTO rough_cut_edits(edit_id,task_id,before_json,after_json,impact_digest,status) VALUES(?,?,?,?,?,'applied')",
                    (edit_id,task_id,_canonical(before),_canonical(after),impact))
            else:
                edit_id='sbrefresh-'+uuid.uuid4().hex
                connection.execute("INSERT INTO storyboard_edits(edit_id,task_id,before_json,after_json,status) VALUES(?,?,?,?,'applied')",
                    (edit_id,task_id,_canonical(before),_canonical(after)))
            connection.execute("UPDATE tasks SET updated_at=datetime('now') WHERE task_id=?",(task_id,))
    if not changed:
        _unchanged(db,task_id,expected,files)
    return {'changedCount':changed,'preservedCount':len(protected),'secondaryCount':secondary,
            'gapCount':sum(not c.get('asset_id') for c in after['cards']),
            'storyboardDigest':storyboard_digest(db,task_id)}
