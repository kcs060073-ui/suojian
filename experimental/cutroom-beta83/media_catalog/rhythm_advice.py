"""Evidence-bound local rhythm hints and read-only manual-edit projections.

Relative durations are review clues, never an aesthetic verdict or cut clock.
This module deliberately has no apply, render or provider execution API.
"""
from __future__ import annotations

import json
import math
import re
import statistics

from .creative_assistance import _context, _digest
from .fingerprint import quick_fingerprint
from .snapshots import file_content_signature
from .rough_cut import _snapshot_state, _planned_state, preview_edit_impact
from .scene_cards import picture_view_digest
from .rough_media import _scoped_location, _available_source, RoughMediaError
from .visual_uniqueness import validate_scene_card_uniqueness
from .timeline_contract import TimeWindow, validate_coverage, seconds_to_us

METHOD_VERSION = "local-rhythm-v1"
LIMITATIONS = [
    "本地结构检查，不代表AI已理解全片或节奏已通过。",
    "未可靠判断景别、画面运动、配乐听感和包装的整体表达；需要实际观看试听。",
    "镜头时长比例只是复看线索，不按固定秒数切镜；保留锁稿、原声/本片配音和倍速。",
]


def _inputs(db, task_id, expected_revision=None):
    try:
        task, revision, cards, projection, direction = _context(db, task_id, expected_revision)
    except (KeyError, ValueError, RuntimeError) as error:
        raise type(error)(str(error).replace("RESHOOT_", "RHYTHM_")) from error
    if projection["unassignedShotIds"]:
        raise RuntimeError("RHYTHM_TIMING_REQUIRED: 存在未关联当前文案段的镜头，请先恢复时间轴")
    try:
        groups = projection['groups']
        validate_coverage([TimeWindow(g['output_start_us'],g['output_end_us']) for g in groups],
            TimeWindow(0,max(g['output_end_us'] for g in groups)))
        for group in groups:
            validate_coverage([TimeWindow(seconds_to_us(c['narrative_start']),seconds_to_us(c['narrative_end']))
                for c in group['shots']],TimeWindow(group['output_start_us'],group['output_end_us']))
    except (ValueError, TypeError) as error:
        raise RuntimeError('RHYTHM_TIMING_REQUIRED: 当前文案与镜头时间轴有空档、重叠或无效时码，请先恢复') from error
    files = []
    evidence = []
    unavailable = []
    source_ids = {c["asset_id"] for c in cards if c.get("asset_id")}
    if task['task_type'] == 'digital_human':
        source_ids.update(r[0] for r in db.connection.execute(
            "SELECT asset_id FROM task_assets WHERE task_id=? AND role='digital_human'", (task_id,)))
    for asset_id in sorted(source_ids):
        asset = db.connection.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        try:
            location = _scoped_location(db, task_id, asset_id)
            if location is None:
                raise ValueError("授权素材当前不可用")
            path, _ = _available_source(db, asset_id, location['library_id'], location['relative_path'])
            fingerprint = quick_fingerprint(path)
            signature = file_content_signature(path)
            valid = (asset is not None and fingerprint == asset['quick_fingerprint']
                     and (not asset['full_sha256'] or asset['full_sha256'] == signature['sha256']))
        except (OSError, ValueError, KeyError, RuntimeError, RoughMediaError):
            valid = False; fingerprint = None; signature = None
        files.append({"assetId": asset_id, "available": valid, "fingerprint": fingerprint, "signature": signature})
        if not valid:
            unavailable.append(asset_id)
        for table in ("analysis_runs", "visual_observations", "catalog_segment_marks", "quality_evidence_runs"):
            evidence.extend([table, dict(r)] for r in db.connection.execute(f"SELECT * FROM {table} WHERE asset_id=? ORDER BY rowid", (asset_id,)))
    packaging = [dict(r) for r in db.connection.execute("SELECT * FROM packaging_plans WHERE task_id=? ORDER BY plan_id", (task_id,))]
    for table in ("packaging_plan_items", "packaging_plan_instances"):
        packaging.extend([table,dict(r)] for r in db.connection.execute(f"SELECT i.* FROM {table} i JOIN packaging_plans p USING(plan_id) WHERE p.task_id=? ORDER BY i.rowid",(task_id,)))
    context = {"method": METHOD_VERSION, "revision": revision, "brief": task['brief_json'], "files": files, "evidence": evidence, "packaging": packaging}
    return task, revision, cards, projection, direction, _digest(context), unavailable


def _summary(card):
    return {k: card.get(k) for k in ('card_id','idx','asset_id','mix_form','narrative_start','narrative_end','enter_tc','exit_tc','script_segment_id')}


def _draft(db, task_id, state, edit, unavailable, duplicate_error):
    if unavailable:
        return None, "当前镜头素材离线、文件变化或不在授权范围，先恢复素材后重新检查。"
    if duplicate_error:
        return None, "存在明确重复画面约束，先处理重复；节奏偏好不能绕过该约束。"
    try:
        _planned_state(db, task_id, state, edit)
    except (ValueError, RuntimeError, KeyError) as error:
        return None, str(error)
    return edit, None


def _diagnose(db, task, cards, projection, unavailable):
    state = _snapshot_state(db.connection, task['task_id'])
    items = []
    duplicate_error = None
    try:
        validate_scene_card_uniqueness(db.connection, cards)
    except ValueError as error:
        duplicate_error = str(error)
    for group in projection['groups']:
        shots = sorted(group['shots'], key=lambda c: c['narrative_start'])
        start, end = group['output_start_us'], group['output_end_us']
        if not shots:
            raise RuntimeError("RHYTHM_TIMING_REQUIRED: 文案段没有对应镜头")
        span = end - start
        durations = [c['narrative_end']-c['narrative_start'] for c in shots]
        if not all(isinstance(x,(float,int)) and math.isfinite(x) and x>0 for x in durations):
            raise RuntimeError("RHYTHM_TIMING_REQUIRED: 镜头时码无效")
        def add(kind, card, title, reason, suggestion, edit=None):
            draft, blocked = _draft(db,task['task_id'],state,edit,unavailable,duplicate_error) if edit else (None,None)
            items.append({"suggestionId": 'rhythm-'+_digest([group['segment_id'],card['card_id'],kind])[:24],
                "kind":kind,"scriptSegmentId":group['segment_id'],"segmentIndex":group['idx'],
                "cardId":card['card_id'],"cardIndex":card['idx'],"startUs":round(card['narrative_start']*1e6),
                "endUs":round(card['narrative_end']*1e6),"title":title,"reason":reason,
                "suggestion":suggestion,"editDraft":draft,"blockedReason":blocked,
                "evidence":{"text":group['text'],"segmentStartUs":start,"segmentEndUs":end,"shotCount":len(shots),"shotDurationsUs":[round(d*1e6) for d in durations]}})
        candidates = [c for c,d in zip(shots,durations) if len(shots)>=3 and d*1e6/span<=.2 and d<=statistics.median(durations)*.5]
        if candidates:
            card = min(candidates,key=lambda c:c['narrative_end']-c['narrative_start'])
            presenter = card['mix_form']=='数字人全屏'
            fraction = round((card['narrative_end']-card['narrative_start'])*1e8/span,1)
            add('brief_fragment',card,'建议试听这一处较短切镜',
                f"本段有{len(shots)}个镜头，该镜头占本段{fraction}%，明显短于本段其他镜头；这是结构线索，不等于节奏不好。",
                '数字人优先保留表达连续性，可手动调整补画面安排。' if presenter else '可试着减少一次切镜，由同段相邻镜头补足时间；先查看修改草稿，再试听判断。',
                None if presenter else {'action':'delete','card_id':card['card_id']})
        fact_count = len(re.findall(r"[0-9]+(?:[.,][0-9]+)?(?:万|亿|米|平米|千瓦|%|个|家|P|秒|小时)",group['text'],re.I))
        if len(shots)>=3 and fact_count>=3:
            add('dense_facts',shots[0],'数据密集段建议保持稳定画面',f"本段文案包含{fact_count}处数字表达，并安排{len(shots)}个镜头。没有词级可靠锚点，未推定数字所在秒数。",'边听边检查数字是否易理解；必要时用稳定画面或信息卡承接，保留原文及配音。')
        clauses = [v for v in re.split(r'[，,。；;！？!?]',group['text']) if v.strip()]
        if len(shots)==1 and len(clauses)>=3:
            card=shots[0]
            if task['task_type']=='digital_human' and card['mix_form']=='数字人全屏':
                add('presenter_context',card,'可考虑补充与文案对应的画面',f"本段{len(clauses)}个表达单元由同一人物画面承接；人物持续表达本身不构成节奏问题。",'有真实可用素材时再考虑补画面；素材不足保留原口播，可查看缺口处理清单。')
            elif task['task_type']=='ad_mix' and card['mix_form']!='数字人全屏':
                asset = db.connection.execute('SELECT media_kind FROM assets WHERE asset_id=?',(card.get('asset_id'),)).fetchone()
                if asset and asset['media_kind']=='image':
                    add('ad_static_context',card,'建议复看静态画面能否承接整段',f"本段{len(clauses)}个表达单元使用一张图片；未把视频误认为静止。",'在真实可用素材中考虑补充对应画面；不机械均分，也不自动循环凑时长。')
    if duplicate_error or unavailable:
        first=cards[0]
        items.append({"suggestionId":'rhythm-'+_digest(['constraint',duplicate_error,unavailable])[:24],"kind":'duplicate_visual' if duplicate_error else 'source_unavailable',
            "cardId":first['card_id'],"cardIndex":first['idx'],"scriptSegmentId":first['script_segment_id'],"segmentIndex":0,
            "startUs":0,"endUs":max(g['output_end_us'] for g in projection['groups']),
            "title":'先处理画面约束再判断节奏',"reason":duplicate_error or '当前选定素材离线、变化或授权不可用。',
            "suggestion":'使用现有镜头编辑/素材恢复入口处理，重新检查。',"editDraft":None,"blockedReason":duplicate_error or '素材不可用',"evidence":{"assetIds":unavailable}})
    return items


def build_rhythm_advice(db, task_id: str, expected_revision: str) -> dict:
    if not isinstance(expected_revision,str) or not expected_revision:
        raise ValueError('RHYTHM_REVISION_REQUIRED: 需要您当前看到的时间轴版本')
    with db.transaction(immediate=True) as c:
        task,revision,cards,projection,direction,digest,unavailable=_inputs(db,task_id,expected_revision)
        old=c.execute('SELECT result_json FROM rhythm_reports WHERE task_id=? AND task_revision=? AND inputs_digest=?',(task_id,revision,digest)).fetchone()
        if old:
            result=json.loads(old['result_json'])
        else:
            result={"reportId":'rhythm-report-'+_digest([task_id,revision,digest])[:24],"taskId":task_id,"taskRevision":revision,
                "inputsDigest":digest,"method":METHOD_VERSION,"route":task['task_type'],"orientation":direction,
                "status":"draft","stale":False,"aiEvaluated":False,"limitations":LIMITATIONS,
                "durationUs":max(g['output_end_us'] for g in projection['groups']),"items":_diagnose(db,task,cards,projection,unavailable)}
            c.execute('INSERT INTO rhythm_reports(report_id,task_id,task_revision,inputs_digest,result_json) VALUES(?,?,?,?,?)',
                (result['reportId'],task_id,revision,digest,json.dumps(result,ensure_ascii=False)))
        c.execute('INSERT INTO task_rhythm_current(task_id,report_id) VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET report_id=excluded.report_id',(task_id,result['reportId']))
    return result


def get_rhythm_advice(db, task_id: str, report_id: str | None = None) -> dict:
    if report_id is None:
        row=db.connection.execute('SELECT report_id FROM task_rhythm_current WHERE task_id=?',(task_id,)).fetchone()
        if row is None:return {'status':'not_generated','taskId':task_id,'items':[],'stale':False}
        report_id=row['report_id']
    row=db.connection.execute('SELECT * FROM rhythm_reports WHERE task_id=? AND report_id=?',(task_id,report_id)).fetchone()
    if row is None:raise KeyError('RHYTHM_REPORT_NOT_FOUND: 当前任务没有这份建议')
    result=json.loads(row['result_json'])
    try:
        inputs=_inputs(db,task_id)
        result['stale']=inputs[1]!=row['task_revision'] or inputs[5]!=row['inputs_digest']
    except (KeyError,ValueError,RuntimeError):result['stale']=True
    return result


def preview_rhythm_edit(db, task_id: str, report_id: str, suggestion_id: str, expected_revision: str) -> dict:
    with db.transaction(immediate=True):
        report=get_rhythm_advice(db,task_id,report_id)
        task,revision,cards,projection,direction,digest,unavailable=_inputs(db,task_id,expected_revision)
        if report['taskRevision']!=revision:
            raise RuntimeError('RHYTHM_REVISION_CHANGED: 当前镜头已修改，请重新检查')
        if report['inputsDigest']!=digest:
            raise RuntimeError('RHYTHM_INPUTS_CHANGED: 素材、包装或分析依据已变化，请重新检查')
        item=next((i for i in report['items'] if i['suggestionId']==suggestion_id),None)
        if item is None:raise KeyError('RHYTHM_ITEM_NOT_FOUND: 建议条目不存在')
        edit=item.get('editDraft')
        if not edit:raise ValueError('RHYTHM_MANUAL_REQUIRED: 该建议需要手动判断或先处理素材约束')
        before=_snapshot_state(db.connection,task_id)
        after,affected=_planned_state(db,task_id,before,edit)
        impact=preview_edit_impact(db,task_id,edit,expected_digest=picture_view_digest(db,task_id))
        return {"reportId":report_id,"suggestionId":suggestion_id,"taskRevision":revision,"edit":edit,
            "before":[_summary(c) for c in before['cards'] if c['card_id'] in affected],
            "after":[_summary(c) for c in after['cards'] if c['card_id'] in affected],
            "affectedCardIds":affected,"invalidatedStages":impact['invalidated_stages'],"locked":impact['locked'],
            "status":"preview_only","expiresAt":impact['expires_at'],"durationPreserved":True,"mainAudioPreserved":True}


def locate_rhythm_suggestion(db, task_id: str, report_id: str, suggestion_id: str, expected_revision: str) -> dict:
    """A location is actionable only while the report still describes this task."""
    with db.transaction(immediate=True):
        report = get_rhythm_advice(db, task_id, report_id)
        inputs = _inputs(db, task_id, expected_revision)
        if report['taskRevision'] != inputs[1]:
            raise RuntimeError('RHYTHM_REVISION_CHANGED: 镜头已修改，请重新检查')
        if report['inputsDigest'] != inputs[5]:
            raise RuntimeError('RHYTHM_INPUTS_CHANGED: 检查依据已变化，请重新检查')
        item = next((i for i in report['items'] if i['suggestionId'] == suggestion_id), None)
        if item is None:
            raise KeyError('RHYTHM_ITEM_NOT_FOUND: 建议条目不存在')
        return {k: item[k] for k in ('cardId','cardIndex','startUs','endUs','scriptSegmentId')}
