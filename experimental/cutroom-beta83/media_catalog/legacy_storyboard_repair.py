"""Explicit maintenance repair for unedited, unfilled legacy ad storyboards.

Reuses existing audio and reviewed local ASR boundaries. This never calls a
provider, changes approved copy, or silently upgrades user-selected shots.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

from .ad_actions import _task_context, _validate_ad_outputs, _validate_ad_plan, _write_json_atomically
from .ad_artifact_binding import capture_ad_artifact_hashes
from .ad_matching import load_observed_ad_windows, match_ad_shots
from .creative_assistance import register_reshoot_need
from .provenance import canonical_digest
from .scene_cards import add_scene_card, list_scene_cards
from .script_segments import create_script_segment
from .task_sources import list_eligible_assets
from .voice_ownership import read_voice_ownership


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _write(path, data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def _request(plan,alignment,mapping):
    return canonical_digest(dict(plan=plan,alignment=alignment,mapping=mapping))


def recover_pending_repair(db,root,task_id):
    # A live repair holds the same write lock. Recheck the journal only after
    # acquiring it, so a concurrent preview cannot undo an in-flight commit.
    with db.transaction(immediate=True):
        _recover_pending_repair_locked(db,root,task_id)


def _recover_pending_repair_locked(db,root,task_id):
    """Resolve an interrupted directory swap using committed SQLite identities."""
    tasks=(Path(root).resolve()/'runtime/workbench/tasks').resolve()
    task_root=(tasks/task_id).resolve()
    if not task_id or Path(task_id).name!=task_id or not task_root.is_relative_to(tasks) or task_root==tasks:
        raise ValueError('REPAIR_TASK_INVALID')
    journal=task_root/'history/storyboard-repair-pending.json'
    if not journal.exists():
        return
    if journal.is_symlink():
        raise ValueError('REPAIR_PATH_INVALID')
    data=_load(journal);identity=data.get('identity','')
    if not re.fullmatch(r'repair-[0-9a-f]{32}',identity):
        raise ValueError('REPAIR_JOURNAL_INVALID')
    ad=task_root/'artifacts/ad';archive=task_root/'history/storyboard-repairs'/identity
    interrupted=archive.with_name(identity+'-interrupted')
    for path in (ad,archive,interrupted):
        if path.is_symlink() or not path.resolve().is_relative_to(task_root):
            raise ValueError('REPAIR_PATH_INVALID')
    current=canonical_digest(list_scene_cards(db,task_id))
    if current==data.get('afterCards'):
        meta=_load(ad/'recommendation.json')
        if (meta.get('short_clause_repair',{}).get('baseRevision')!=data['baseRevision']
            or capture_ad_artifact_hashes(ad)!=data['afterFiles']):
            raise RuntimeError('REPAIR_RECOVERY_CONFLICT')
    elif current==data['beforeCards']:
        if archive.is_dir():
            if capture_ad_artifact_hashes(archive)!=data['beforeFiles']:
                raise RuntimeError('REPAIR_RECOVERY_CONFLICT')
            if ad.exists():
                if capture_ad_artifact_hashes(ad)!=data.get('afterFiles') or interrupted.exists():
                    raise RuntimeError('REPAIR_RECOVERY_CONFLICT')
                os.replace(ad,interrupted)
            os.replace(archive,ad)
        elif not ad.is_dir() or capture_ad_artifact_hashes(ad)!=data['beforeFiles']:
            raise RuntimeError('REPAIR_RECOVERY_CONFLICT')
    else:
        raise RuntimeError('REPAIR_RECOVERY_CONFLICT: 中断后出现新的人工修改，保留目录等待核对')
    journal.unlink()


def _prepare(db,root,task_id,plan,alignment,mapping):
    task_root,task,brief,script_path,sha = _task_context(db,SimpleNamespace(task_id=task_id,payload={}),root)
    ad = task_root/'artifacts/ad'
    script=script_path.read_text(encoding='utf-8')
    old_plan,old_timing=_validate_ad_outputs(script,brief['aspect_ratio'],ad/'adplan.json',ad/'adtiming.json',ad/'voice.mp3')
    _validate_ad_plan(script,brief['aspect_ratio'],plan)
    cards=list_scene_cards(db,task_id)
    if (not cards or any(c['asset_id'] or c['confirm_status'] or c.get('final_decision') or c.get('source_type')=='用户指定' for c in cards)
        or db.connection.execute('SELECT 1 FROM storyboard_feedback WHERE task_id=?',(task_id,)).fetchone()):
        raise RuntimeError('REPAIR_MANUAL_WORK: 已选画面或人工决定必须通过正式返工处理')
    for table in ('candidate_versions','picture_lock','packaging_plans','reshoot_requirement_revisions','rough_cut_edits'):
        if db.connection.execute('SELECT 1 FROM '+table+' WHERE task_id=?',(task_id,)).fetchone():
            raise RuntimeError('REPAIR_DOWNSTREAM_EXISTS: 已有后续产物，不能进行旧分镜维护修复')
    if read_voice_ownership(db,task_id).branch!='legacy':
        raise RuntimeError('REPAIR_VOICE_OWNED: 新声音版本必须沿声音编辑流程处理')
    if db.connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running','pending') AND job_type NOT IN ('scene_detection','visual_batch')",(task_id,)).fetchone():
        raise RuntimeError('REPAIR_TASK_BUSY: 制作作业尚未结束')
    if not any(len(s['text'])>40 for s in old_plan['segments']):
        raise RuntimeError('REPAIR_NOT_LEGACY: 当前规划没有旧长段缺陷')
    if any(not s['text'].strip() or len(s['text'])>40 for s in plan['segments']):
        raise ValueError('REPAIR_SHORT_CLAUSES_REQUIRED')
    audio_sha=hashlib.sha256((ad/'voice.mp3').read_bytes()).hexdigest()
    if alignment.get('source_sha256')!=audio_sha or not alignment.get('model'):
        raise RuntimeError('REPAIR_AUDIO_CHANGED: 本地识别不属于当前声音')
    measured=alignment.get('segments')
    if not isinstance(measured,list) or not measured or not isinstance(mapping,list) or len(mapping)!=len(plan['segments']):
        raise ValueError('REPAIR_ALIGNMENT_INVALID')
    if any(not isinstance(group,list) or not group or any(type(i) is not int for i in group) for group in mapping):
        raise ValueError('REPAIR_ALIGNMENT_INVALID')
    if [i for group in mapping for i in group]!=list(range(len(measured))):
        raise ValueError('REPAIR_ALIGNMENT_COVERAGE: 每个识别单元必须按顺序恰好使用一次')
    total=float(old_timing['total'])
    if not math.isfinite(float(alignment.get('duration',0))) or abs(float(alignment['duration'])-total)>0.15:
        raise ValueError('REPAIR_ALIGNMENT_DURATION')
    previous=0
    for item in measured:
        a,b=item.get('start'),item.get('end')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)) or not 0<=a<b<=total+0.15 or a<previous-0.02:
            raise ValueError('REPAIR_ALIGNMENT_TIME')
        previous=b
    # Existing TTS chunk joins are stronger than ASR estimates at those joins.
    known={}; offset=0
    for timed in old_timing['segments']:
        known[offset]=float(timed['start']);offset+=len(timed['text'])
    starts=[];offset=0
    for segment,group in zip(plan['segments'],mapping,strict=True):
        starts.append(known.get(offset,float(measured[group[0]]['start'])))
        offset+=len(segment['text'])
    if starts[0]!=0 or any(a>=b for a,b in zip(starts,starts[1:]+[total])):
        raise ValueError('REPAIR_ALIGNMENT_BOUNDARIES')
    timing={**old_timing,'segments':[], 'timing_basis':'local_asr_reviewed_boundaries'}
    for i,(segment,start,end) in enumerate(zip(plan['segments'],starts,starts[1:]+[total],strict=True)):
        timing['segments'].append(dict(i=i,text=segment['text'],start=start,end=end,dur=round(end-start,6)))
    eligible=list_eligible_assets(db,task_id)
    matches=match_ad_shots(plan['segments'],timing['segments'],load_observed_ad_windows(db,eligible),brief['aspect_ratio'])
    metadata=_load(ad/'recommendation.json')
    segments=[dict(x) for x in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx',(task_id,sha))]
    revision=canonical_digest(dict(requestDigest=_request(plan,alignment,mapping),timing=timing,cards=cards,segments=segments,brief=brief,metadata=metadata,hashes=capture_ad_artifact_hashes(ad),matches=matches))
    return dict(task_root=task_root,ad=ad,script=script,sha=sha,cards=cards,segments=segments,timing=timing,matches=matches,metadata=metadata,revision=revision)


def preview_repair(db,root,task_id,plan,alignment,mapping):
    recover_pending_repair(db,root,task_id)
    state=_prepare(db,Path(root),task_id,plan,alignment,mapping)
    return dict(revision=state['revision'],oldCount=len(state['cards']),newCount=len(plan['segments']),
        matchedCount=sum(m is not None for m in state['matches']),externalRequests=0,
        timingBasis='本地语音识别边界，原文与原配音保留；需在粗剪中试听核对',
        segments=[dict(text=s['text'],start=t['start'],end=t['end'],match=m) for s,t,m in zip(plan['segments'],state['timing']['segments'],state['matches'],strict=True)])


def apply_repair(db,root,task_id,plan,alignment,mapping,*,expected_revision):
    """Apply an explicitly reviewed repair; stale input and human edits fail closed."""
    root=Path(root).resolve()
    if not task_id or Path(task_id).name!=task_id or task_id in {'.','..'}:
        raise ValueError('REPAIR_TASK_INVALID')
    task_root=(root/'runtime/workbench/tasks'/task_id).resolve()
    if not task_root.is_relative_to(root/'runtime/workbench/tasks'):
        raise ValueError('REPAIR_TASK_INVALID')
    recover_pending_repair(db,root,task_id)
    ad=task_root/'artifacts/ad'
    journal=task_root/'history/storyboard-repair-pending.json'
    request=_request(plan,alignment,mapping)
    prior=_load(ad/'recommendation.json').get('short_clause_repair')
    if prior and prior.get('requestDigest')==request and prior.get('baseRevision')==expected_revision:
        if prior['cardsDigest']!=canonical_digest(list_scene_cards(db,task_id)) or capture_ad_artifact_hashes(ad)!=_load(ad/'recommendation.json')['artifact_sha256']:
            raise RuntimeError('REPAIR_STALE: 修复后已有变更')
        return prior['receipt']
    staging=None;archive=None;moved=False;installed=False
    try:
        with db.transaction(immediate=True) as c:
            state=_prepare(db,root,task_id,plan,alignment,mapping)
            if state['revision']!=expected_revision:
                raise RuntimeError('REPAIR_STALE: 分镜、素材或声音已变化，请重新预览')
            identity='repair-'+uuid.uuid4().hex
            staging=task_root/'artifacts'/('.'+identity)
            archive=task_root/'history/storyboard-repairs'/identity
            for path in (ad,staging,archive):
                if not path.resolve().is_relative_to(task_root) or path.is_symlink():
                    raise ValueError('REPAIR_PATH_INVALID')
            if any(p.is_symlink() for p in ad.rglob('*')):
                raise ValueError('REPAIR_PATH_INVALID')
            shutil.copytree(ad,staging)
            _write(staging/'adplan.json',plan)
            _write(staging/'adtiming.json',state['timing'])
            _write(staging/'alignment-evidence.json',dict(alignment=alignment,mapping=mapping,origin='local_asr_maintenance_review'))
            archive.parent.mkdir(parents=True,exist_ok=True)
            recovery=dict(identity=identity,baseRevision=expected_revision,beforeCards=canonical_digest(state['cards']),beforeFiles=capture_ad_artifact_hashes(ad))
            _write_json_atomically(journal,recovery)
            os.replace(ad,archive);moved=True
            _write(archive/'scene-ledger.json',dict(cards=state['cards'],segments=state['segments']))
            c.execute('DELETE FROM scene_cards WHERE task_id=?',(task_id,))
            c.execute('UPDATE reshoot_requirements SET active=0 WHERE task_id=?',(task_id,))
            c.execute('UPDATE script_segments SET script_revision=? WHERE task_id=? AND script_revision=?',('archived:'+identity+':'+state['sha'],task_id,state['sha']))
            for i,(segment,timed,match) in enumerate(zip(plan['segments'],state['timing']['segments'],state['matches'],strict=True)):
                # New opaque identity, same actual script hash. Archived segment IDs remain valid for history.
                part=create_script_segment(db,task_id,identity,i,segment['text'],round(timed['start']*1e6),round(timed['end']*1e6))
                c.execute('UPDATE script_segments SET script_revision=? WHERE segment_id=?',(state['sha'],part['segment_id']))
                if match:
                    c.execute("INSERT OR IGNORE INTO task_assets(task_id,asset_id,role) VALUES(?,?,'broll')",(task_id,match['asset_id']))
                card=add_scene_card(db,task_id,i,shot_id=identity+'-'+str(i),script_segment_id=part['segment_id'],
                    narrative_text=segment['text'],narrative_start=timed['start'],narrative_end=timed['end'],
                    asset_id=match['asset_id'] if match else None,segment_id=match['segment_id'] if match else None,
                    mix_form='broll全屏',enter_tc=match['source_start'] if match else None,exit_tc=match['source_end'] if match else None,
                    audio_policy_draft='replace',source_type='系统推荐' if match else '缺口替代',
                    system_suggestion='画面需求：'+segment['query']+'；'+('已有画面观察文字匹配：'+match['title'] if match else '已分析素材中暂无合格匹配，未分析素材需继续分析，不能据此判定必须补拍'))
                if not match:
                    register_reshoot_need(db,task_id,state['sha'],part['segment_id'],'card:'+card['card_id'],segment['query'],segment['type'],round(timed['start']*1e6),round(timed['end']*1e6),card['card_id'])
            cards=list_scene_cards(db,task_id)
            result=dict(sceneCardCount=len(cards),matchedCount=sum(bool(card['asset_id']) for card in cards),archive=str(archive),externalRequests=0)
            metadata=deepcopy(state['metadata'])
            metadata.update(scene_card_count=len(cards),missing_count=sum(not card['asset_id'] for card in cards),artifact_sha256=capture_ad_artifact_hashes(staging))
            metadata['short_clause_repair']=dict(requestDigest=request,baseRevision=expected_revision,cardsDigest=canonical_digest(cards),receipt=result,planningSource='reviewed_local_repair',timingBasis='local_asr_reviewed_boundaries')
            _write(staging/'recommendation.json',metadata)
            _validate_ad_outputs(state['script'],plan['ratio'],staging/'adplan.json',staging/'adtiming.json',staging/'voice.mp3')
            recovery.update(afterCards=canonical_digest(cards),afterFiles=capture_ad_artifact_hashes(staging))
            _write_json_atomically(journal,recovery)
            os.replace(staging,ad);installed=True
        journal.unlink(missing_ok=True)
        return result
    except BaseException:
        if journal.exists():
            recover_pending_repair(db,root,task_id)
        raise
    finally:
        if staging is not None and staging.exists() and staging.resolve().is_relative_to(task_root):
            shutil.rmtree(staging)
