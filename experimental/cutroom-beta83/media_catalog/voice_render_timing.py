"""Pure A2 measured-timing projection; no media reads, database or generation.

Call with independently registered bundle expectations and current task/card/source
snapshots. Digest consistency is not provider provenance or real-media verification.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

from .timeline_contract import seconds_to_us

_VERSION='voice-pcm-assembly-v1'
_FORMAT=dict(sample_rate=48000,channels=1,sample_width=2,codec='pcm_s16le')
_EXPECTED={'task_id','base_revision','script_sha256','assembly_id','plan_digest',
           'manifest_digest','audio_sha256','config_fingerprints'}
_CODES={'VOICE_RENDER_BINDING_INVALID','VOICE_RENDER_BUNDLE_INVALID','VOICE_RENDER_INPUT_INVALID'}
_SHA=re.compile('[0-9a-f]{64}')


def _require(valid,code):
    if not valid:raise ValueError(code)


def _text(value):return isinstance(value,str) and bool(value.strip())
def _sha(value):return isinstance(value,str) and _SHA.fullmatch(value) is not None
def _uint(value):return type(value) is int and value>=0


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()


def _frame_us(frame):return (frame*1000000+24000)//48000


def _seconds(value):
    _require(type(value) in (int,float) and math.isfinite(value) and value>=0,'VOICE_RENDER_INPUT_INVALID')
    return seconds_to_us(value)


def _bundle_rows(bundle,expected,script):
    bind='VOICE_RENDER_BINDING_INVALID';bad='VOICE_RENDER_BUNDLE_INVALID'
    _require(isinstance(expected,dict) and set(expected)==_EXPECTED,bind)
    _require(_text(expected['task_id']) and _uint(expected['base_revision']) and
        all(_sha(expected[k]) for k in ('script_sha256','assembly_id','plan_digest','manifest_digest','audio_sha256')),bind)
    configs=expected['config_fingerprints']
    _require(isinstance(configs,dict) and configs and all(_text(k) and _sha(v) for k,v in configs.items()),bind)
    _require(isinstance(script,str) and script and hashlib.sha256(script.encode('utf8')).hexdigest()==expected['script_sha256'],bind)
    _require(isinstance(bundle,dict) and isinstance(bundle.get('receipt'),dict) and isinstance(bundle.get('timeline'),dict),bad)
    receipt,timeline=bundle['receipt'],bundle['timeline']
    _require(bundle.get('assembly_id')==receipt.get('assembly_id')==expected['assembly_id'] and
        receipt.get('task_id')==timeline.get('task_id')==expected['task_id'] and
        type(receipt.get('base_revision')) is int and type(timeline.get('base_revision')) is int and
        receipt['base_revision']==timeline['base_revision']==expected['base_revision'] and
        receipt.get('plan_digest')==timeline.get('plan_digest')==expected['plan_digest'] and
        receipt.get('manifest_digest')==expected['manifest_digest'] and receipt.get('sha256')==expected['audio_sha256'],bind)
    _require(receipt['manifest_digest']==_digest({k:v for k,v in receipt.items() if k!='manifest_digest'}) and
        receipt.get('timeline_digest')==_digest(timeline),bad)
    frames=receipt.get('frame_count');duration=receipt.get('duration_us')
    _require(type(frames) is int and 0<frames<=48000*3600 and type(duration) is int and
        duration==_frame_us(frames)==timeline.get('duration_us') and type(timeline.get('duration_us')) is int,bad)
    _require(receipt.get('assembler_version')==timeline.get('assembler_version')==_VERSION and
        receipt.get('format')==_FORMAT and receipt.get('status')=='assembled_draft' and
        receipt.get('full_decode_verified') is True and _sha(receipt.get('pcm_sha256')),bad)
    sources,records=receipt.get('sources'),receipt.get('segments')
    _require(isinstance(sources,list) and sources and isinstance(records,list) and len(sources)==len(records)==len(configs),bad)
    computed=[];ranges={};segments=[];audio=[];cursor=0
    for source,record in zip(sources,records):
        _require(isinstance(source,dict) and isinstance(record,dict),bad)
        sid=source.get('segment_id');original=source.get('audio')
        _require(_text(sid) and sid not in ranges and isinstance(original,dict),bad)
        _require(source.get('config_fingerprint')==configs.get(sid)==original.get('fingerprint'),bind)
        first,last=record.get('start_frame'),record.get('end_frame')
        _require(type(first) is int and type(last) is int and first==cursor<last<=frames and
            record.get('segment_id')==sid and record.get('mode')==source.get('mode') in ('reuse','generate') and
            _sha(original.get('sha256')) and record.get('source_sha256')==original['sha256'] and
            _sha(record.get('normalized_pcm_sha256')),bad)
        declared=original.get('duration')
        _require(type(declared) in (int,float) and math.isfinite(declared) and declared>0,bad)
        error=_frame_us(last-first)-round(declared*1000000)
        _require(type(record.get('boundary_error_us')) is int and record['boundary_error_us']==error and abs(error)<=21,bad)
        start,end=_frame_us(first),_frame_us(last)
        parts=original.get('timing')
        _require(original.get('timing_kind') in ('measured_sentence','measured_word') and isinstance(parts,list) and parts,bad)
        local=[];previous=0
        for part in parts:
            _require(isinstance(part,dict) and _text(part.get('text')),bad)
            lo,hi=part.get('start'),part.get('end')
            _require(type(lo) in (int,float) and type(hi) in (int,float) and math.isfinite(lo) and math.isfinite(hi) and 0<=lo<hi,bad)
            lo,hi=seconds_to_us(lo),seconds_to_us(hi)
            local_duration=seconds_to_us((last-first)/48000)
            _require(previous<=lo<hi<=local_duration,bad);previous=hi
            row=dict(segment_id=sid,text=part['text'],start_us=start+lo,
                end_us=end if hi==local_duration else start+hi)
            _require(start<=row['start_us']<row['end_us']<=end,bad)
            local.append(row);computed.append(row)
        ranges[sid]=local
        segments.append(dict(segment_id=sid,start_us=start,end_us=end))
        audio.append(dict(segment_id=sid,path=original.get('path'),sha256=original['sha256'],
            start_us=start,end_us=end,mode=source['mode'],request_id=original.get('request_id')))
        cursor=last
    _require(cursor==frames and set(ranges)==set(configs) and timeline.get('segments')==segments and timeline.get('audio')==audio,bad)
    actual=timeline.get('subtitles')
    _require(isinstance(actual,list) and all(isinstance(row,dict) and type(row.get('start_us')) is int and
        type(row.get('end_us')) is int for row in actual) and actual==computed and ''.join(r['text'] for r in actual)==script,bad)
    gaps=[];cursor=0
    for row in computed:
        if row['start_us']>cursor:gaps.append(dict(code='MEASURED_TIMING_GAP',start_us=cursor,end_us=row['start_us']))
        _require(row['start_us']>=cursor,bad);cursor=row['end_us']
    if cursor<duration:gaps.append(dict(code='MEASURED_TIMING_GAP',start_us=cursor,end_us=duration))
    shots=timeline.get('shots')
    _require(isinstance(shots,list),bad);anchors={}
    for shot in shots:
        _require(isinstance(shot,dict) and _text(shot.get('shot_id')) and shot['shot_id'] not in anchors,bad)
        measured=ranges.get(shot.get('segment_id'))
        _require(measured is not None and all(_uint(shot.get(k)) for k in ('start_us','end_us','source_start_us','source_end_us')) and
            shot['start_us']<shot['end_us'] and shot['start_us'] in {r['start_us'] for r in measured} and
            shot['end_us'] in {r['end_us'] for r in measured} and
            shot['source_end_us']-shot['source_start_us']==shot['end_us']-shot['start_us'],bad)
        anchors[shot['shot_id']]=shot
    # Recompute visual gaps against CURRENT cards below. Empty A2 shots may be
    # filled by an exact whole-sentence card, not by invented sub-sentence timing.
    _require(isinstance(timeline.get('gaps'),list),bad)
    return computed,anchors,duration,gaps


def _script_rows(script,segments,expected,measured,gaps):
    bad='VOICE_RENDER_INPUT_INVALID'
    _require(isinstance(segments,list) and segments,bad)
    rows=sorted(segments,key=lambda r:r['idx']);seen=set();old_end=0
    _require([r['idx'] for r in rows]==list(range(len(rows))) and all(type(r['idx']) is int for r in rows),bad)
    _require(''.join(r['text'] for r in rows)==script,bad)
    boundaries={0:0};chars=0
    for i,row in enumerate(measured):chars+=len(row['text']);boundaries[chars]=i+1
    mapped={};updates=[];timed=[];char_start=0
    for row in rows:
        sid=row.get('segment_id');first,last=row.get('output_start_us'),row.get('output_end_us')
        _require(_text(sid) and sid not in seen and row.get('task_id')==expected['task_id'] and
            row.get('script_revision')==expected['script_sha256'] and _text(row.get('text')) and
            type(first) is int and type(last) is int and first==old_end<last,bad)
        seen.add(sid);old_end=last;char_end=char_start+len(row['text'])
        if char_start not in boundaries or char_end not in boundaries:
            gaps.append(dict(code='SCRIPT_SEGMENT_ANCHOR_UNAVAILABLE',script_segment_id=sid))
        else:
            relevant=measured[boundaries[char_start]:boundaries[char_end]]
            start,end=relevant[0]['start_us'],relevant[-1]['end_us']
            mapped[sid]=dict(row=row,start_us=start,end_us=end)
            updates.append(dict(segment_id=sid,output_start_us=start,output_end_us=end))
            timed.append(dict(i=row['idx'],text=row['text'],start=start/1000000,end=end/1000000,dur=(end-start)/1000000))
        char_start=char_end
    return {r['segment_id']:r for r in rows},mapped,updates,timed


def _source_problem(card,window,start,end,anchor):
    if not card.get('asset_id'):return 'SCENE_ASSET_REQUIRED',None
    keys={'asset_id','asset_fingerprint','media_kind','source_start_us','source_end_us','allowed_start_us','allowed_end_us','transition_handle_us'}
    if not isinstance(window,dict) or set(window) not in (keys, keys|{'validated_transition_end_us'}):return 'SOURCE_WINDOW_INVALID',None
    transition_end=window.get('validated_transition_end_us',window['source_end_us'])
    if not _uint(transition_end) or transition_end<window['source_end_us']:return 'SOURCE_WINDOW_INVALID',None
    if window['asset_id']!=card['asset_id'] or not _text(window['asset_fingerprint']):return 'SHOT_SOURCE_WINDOW_CHANGED',None
    if not all(_uint(window[k]) for k in keys-{'asset_id','asset_fingerprint','media_kind'}):return 'SOURCE_WINDOW_INVALID',None
    pair=(card.get('enter_tc'),card.get('exit_tc'))
    if any(v is not None and (type(v) not in (int,float) or not math.isfinite(v) or v<0) for v in pair):
        return 'SOURCE_WINDOW_INVALID',None
    if window['media_kind']=='image':
        if transition_end or any(window[k] for k in keys-{'asset_id','asset_fingerprint','media_kind'}) or pair not in ((None,None),(0,0)):
            return 'SOURCE_WINDOW_INVALID',None
        return None,None
    if window['media_kind']!='video' or not (window['allowed_start_us']<=window['source_start_us']<window['source_end_us']<=window['allowed_end_us']):
        return 'SOURCE_WINDOW_INVALID',None
    if pair not in ((None,None),(0,0)):
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in pair):return 'SOURCE_WINDOW_INVALID',None
        if tuple(seconds_to_us(v) for v in pair)!=(window['source_start_us'],window['source_end_us']):return 'SHOT_SOURCE_WINDOW_CHANGED',None
    if anchor is not None and (anchor['source_start_us']!=window['source_start_us'] or anchor['source_end_us']>window['source_end_us']):
        return 'SHOT_SOURCE_WINDOW_CHANGED',None
    # The selected narrative window remains fixed. Only the transition may use
    # the separately verified render handle; a longer new voice still needs review.
    shortfall=max(end-start-(window['source_end_us']-window['source_start_us']),
        end-start+window['transition_handle_us']-(transition_end-window['source_start_us']))
    return ('SOURCE_WINDOW_TOO_SHORT',shortfall) if shortfall>0 else (None,None)


def _project(bundle,expected,script,script_segments,scene_cards,source_windows):
    measured,anchors,duration,gaps=_bundle_rows(bundle,expected,script)
    old,mapped,script_updates,timing=_script_rows(script,script_segments,expected,measured,gaps)
    bad='VOICE_RENDER_INPUT_INVALID'
    _require(isinstance(scene_cards,list) and isinstance(source_windows,dict),bad)
    cards=sorted(scene_cards,key=lambda c:c['idx']);ids=set();grouped={sid:[] for sid in old}
    _require([c['idx'] for c in cards]==list(range(len(cards))) and all(type(c['idx']) is int for c in cards),bad)
    for card in cards:
        cid=card.get('card_id')
        _require(_text(cid) and cid not in ids and card.get('task_id')==expected['task_id'] and _uint(card.get('shot_revision')),bad)
        ids.add(cid)
        if card.get('script_segment_id') in grouped:grouped[card['script_segment_id']].append(card)
    _require(set(source_windows)<=ids,bad)
    updates=[]
    for card in cards:
        cid=card['card_id'];sid=card.get('script_segment_id');group=old.get(sid)
        def gap(code,**facts):gaps.append(dict(code=code,card_id=cid,script_segment_id=sid,**facts))
        if group is None:gap('CARD_SCRIPT_SEGMENT_MISSING');continue
        start,end=_seconds(card.get('narrative_start')),_seconds(card.get('narrative_end'))
        if card.get('narrative_text')!=group['text'] or not group['output_start_us']<=start<end<=group['output_end_us']:
            gap('CARD_SEGMENT_WINDOW_MISMATCH');continue
        current=mapped.get(sid)
        if current is None:gap('SCRIPT_SEGMENT_ANCHOR_UNAVAILABLE');continue
        anchor=anchors.get(cid)
        if anchor is not None:
            lo,hi=anchor['start_us'],anchor['end_us']
            if not current['start_us']<=lo<hi<=current['end_us']:
                gap('CARD_MEASURED_ANCHOR_MISMATCH');continue
        elif len(grouped[sid])==1 and (start,end)==(group['output_start_us'],group['output_end_us']):
            lo,hi=current['start_us'],current['end_us']
        else:gap('MEASURED_SHOT_ANCHOR_UNAVAILABLE');continue
        problem,shortfall=_source_problem(card,source_windows.get(cid),lo,hi,anchor)
        if problem:
            gap(problem,start_us=lo,end_us=hi,**({'shortfall_us':shortfall} if shortfall is not None else {}));continue
        updates.append(dict(card_id=cid,script_segment_id=sid,expected_shot_revision=card['shot_revision'],
            narrative_start=lo/1000000,narrative_end=hi/1000000,enter_tc=card.get('enter_tc'),exit_tc=card.get('exit_tc')))
    cursor=0
    for row in sorted(updates,key=lambda c:(c['narrative_start'],c['narrative_end'],c['card_id'])):
        start,end=seconds_to_us(row['narrative_start']),seconds_to_us(row['narrative_end'])
        if start>cursor:gaps.append(dict(code='SHOT_COVERAGE_MISSING',start_us=cursor,end_us=start))
        elif start<cursor:gaps.append(dict(code='SHOT_OVERLAP',card_id=row['card_id'],start_us=start,end_us=min(cursor,end)))
        cursor=max(cursor,end)
    if cursor<duration:gaps.append(dict(code='SHOT_COVERAGE_MISSING',start_us=cursor,end_us=duration))
    return dict(task_id=expected['task_id'],base_revision=expected['base_revision'],assembly_id=expected['assembly_id'],
        input_digest=_digest(dict(expected=expected,script=script,script_segments=script_segments,
            scene_cards=scene_cards,source_windows=source_windows)),duration_us=duration,
        timing=dict(total=duration/1000000,segments=timing),script_updates=script_updates,
        scene_updates=updates,gaps=gaps,ready_for_render=not gaps)


def project_ad_voice_timing(bundle,*,expected,script,script_segments,scene_cards,source_windows):
    """Return existing ad timing plus update drafts; gaps prohibit apply/render.

    Whole-sentence cards can move with their measured sentence. Subshots require
    A2 measured anchors; source selection never moves or grows. Root owns current
    context/file proof, atomic application, source readiness and gate invalidation.
    """
    try:
        bundle,expected,script,script_segments,scene_cards,source_windows=deepcopy(
            (bundle,expected,script,script_segments,scene_cards,source_windows))
        return _project(bundle,expected,script,script_segments,scene_cards,source_windows)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError) as error:
        code=str(error) if isinstance(error,ValueError) and str(error) in _CODES else 'VOICE_RENDER_INPUT_INVALID'
        raise ValueError(code) from None
