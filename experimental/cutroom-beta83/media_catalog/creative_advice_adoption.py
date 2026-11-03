"""Explicitly turn one current visual reshoot suggestion into an existing gap requirement."""
from __future__ import annotations
import json
import math
from .creative_assistance import _context, _digest, register_reshoot_need
from .creative_visual_advice import get_creative_advice


def adopt_reshoot_advice(db,task_id,*,expected_revision,card_id,item_index,query,job_id=None,evidence_ref=None,project_root=None):
    if (not isinstance(expected_revision,str) or not expected_revision or bool(job_id)==bool(evidence_ref) or not isinstance(job_id or evidence_ref,str)
            or len(job_id or evidence_ref)>500 or not isinstance(card_id,str) or not card_id
            or type(item_index) is not int or item_index<0 or not isinstance(query,str)
            or not 1<=len(query.strip())<=500):
        raise ValueError('RESHOOT_ADVICE_INPUT: 请明确一个建议、对应镜头及500字以内的补画面要求。')
    query=query.strip();source=job_id or evidence_ref
    with db.transaction(immediate=True):
        _,revision,cards,projection,_=_context(db,task_id,expected_revision)
        report=get_creative_advice(db,task_id,source,project_root=project_root)
        result=report.get('result') or {}
        if report.get('status')!='saved' or report.get('stale') or result.get('taskRevision')!=revision:
            raise RuntimeError('RESHOOT_ADVICE_STALE: 建议尚未保存或当前依据已变化，请重新核对。')
        items=result.get('items') or []
        if item_index>=len(items):raise ValueError('RESHOOT_ADVICE_ITEM: 本次建议没有该条目。')
        item=items[item_index]
        if item.get('kind')!='reshoot':raise ValueError('RESHOOT_ADVICE_KIND: 仅补画面建议可登记为缺口；节奏建议请回到编辑器核对。')
        card=next((c for c in cards if c['card_id']==card_id),None)
        if card is None or item.get('cardId')!=card_id:
            raise ValueError('RESHOOT_ADVICE_ITEM: 建议与当前任务镜头不匹配。')
        start,end=round(card['narrative_start']*1e6),round(card['narrative_end']*1e6)
        if (type(item.get('startUs')) is not int or type(item.get('endUs')) is not int
                or (item['startUs'],item['endUs'])!=(start,end)):
            raise ValueError('RESHOOT_ADVICE_EVIDENCE: 建议时间不属于当前镜头。')
        # A saved provider result carries its exact sampled source windows. Cached
        # rhythm-only review advice cannot be reinterpreted as source reshoot evidence.
        row=db.connection.execute("SELECT j.status,j.cancel_requested,p.payload_json FROM jobs j JOIN job_payloads p USING(job_id) WHERE j.job_id=? AND j.task_id=? AND j.job_type='creative_visual_advice'",(job_id,task_id)).fetchone()
        if row is None or row['cancel_requested'] or row['status'] in {'cancelled','interrupted'}:
            raise ValueError('RESHOOT_ADVICE_EVIDENCE: 缺少当前可采用的源画面作业依据。')
        context=json.loads(row['payload_json']).get('context',{})
        shot=next((s for s in context.get('shots',[]) if s.get('cardId')==card_id),None)
        frames={f.get('ref'):f for f in result.get('coverage',{}).get('frames',[])}
        refs=item.get('frameRefs',[])
        if not shot or not refs or (shot.get('startUs'),shot.get('endUs'))!=(start,end):
            raise ValueError('RESHOOT_ADVICE_EVIDENCE: 建议缺少当前镜头取样依据。')
        for ref in refs:
            frame=frames.get(ref,{});position=frame.get('position_seconds')
            valid=type(position) in (int,float) and math.isfinite(position)
            if (frame.get('cardId')!=card_id or not valid
                    or (frame.get('outputStartUs'),frame.get('outputEndUs'))!=(start,end)
                    or (shot['kind']=='video' and not shot['sourceStart']<=position<shot['sourceEnd'])
                    or (shot['kind']=='image' and position!=0)):
                raise ValueError('RESHOOT_ADVICE_EVIDENCE: 取样帧不属于对应的当前源区间。')
        origin='advice:'+source+':'+_digest([item,query])
        gap_id=register_reshoot_need(db,task_id,projection['scriptRevision'],card['script_segment_id'],origin,
            query,'image' if shot['kind']=='image' else 'video',start,end,card_id)
        return {'gapId':gap_id,'status':'registered','cardId':card_id,'query':query,'taskRevision':revision,
                'message':'已加入缺口清单。登记不改变镜头；请刷新清单，审看候选后再决定是否采用。'}