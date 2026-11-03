"""Segment regeneration drafts based on actual audio, without queue or gate writes.

Public worker integration owns authorization, persistent job claiming, current
revision checks and final atomic application. This module never calls TTS.
"""
from copy import deepcopy

from .timeline_contract import seconds_to_us
from .voice_editing import _check_config, _digest


def plan_segment_regeneration(task_id, base_revision, segments, configs, *, route):
    if route != 'ad_mix':
        raise ValueError('DIGITAL_ORIGINAL_VOICE_PRESERVED')
    if not isinstance(task_id,str) or not task_id or type(base_revision) is not int or base_revision < 0:
        raise ValueError('TASK_VERSION_REQUIRED')
    if not isinstance(segments,list) or not segments or not isinstance(configs,list) or len(segments)!=len(configs):
        raise ValueError('SEGMENT_CONFIG_MISMATCH')
    planned,jobs,seen=[],[],set()
    for segment,config in zip(segments,configs):
        segment_id=segment.get('segment_id')
        if not isinstance(segment_id,str) or not segment_id or segment_id in seen:
            raise ValueError('SEGMENT_ID_INVALID')
        seen.add(segment_id)
        old=_check_config(segment['config']);config=_check_config(config)
        if old['script_sha256']!=config['script_sha256']:
            raise ValueError('SCRIPT_CHANGE_REQUIRES_SCRIPT_WORKFLOW')
        reuse=old['fingerprint']==config['fingerprint'] and segment.get('audio') is not None
        item=dict(segment_id=segment_id,config=deepcopy(config),mode='reuse' if reuse else 'generate')
        if reuse:
            item['audio']=deepcopy(segment['audio'])
        else:
            request_id='segment-'+_digest([task_id,base_revision,segment_id,config['fingerprint']])
            item['request_id']=request_id
            jobs.append(dict(segment_id=segment_id,request_id=request_id,fingerprint=config['fingerprint']))
        planned.append(item)
    plan=dict(task_id=task_id,base_revision=base_revision,segments=planned,jobs=jobs)
    plan['digest']=_digest(plan)
    return plan


def bind_measured_timeline(plan, results, verifier, *, shots=()):
    """Return a draft timeline; never stretch guessed timing or extend source bounds.

    Shot anchors reference inclusive indices in the original-text measured
    timing list for their segment. Unavailable anchors/windows become explicit
    gaps. The caller must compile/render/QA before applying any derived output.
    """
    body={key:value for key,value in plan.items() if key!='digest'}
    if plan.get('digest')!=_digest(body):
        raise ValueError('REGENERATION_PLAN_CHANGED')
    expected={s['segment_id'] for s in plan['segments'] if s['mode']=='generate'}
    if not isinstance(results,dict) or set(results)!=expected:
        raise ValueError('SEGMENT_AUDIO_REQUIRED')
    offset=0;subtitles=[];audio=[];segments=[];lookup={}
    for segment in plan['segments']:
        segment_id=segment['segment_id']
        receipt=results[segment_id] if segment['mode']=='generate' else segment['audio']
        if segment['mode']=='generate' and receipt.get('request_id')!=segment['request_id']:
            raise ValueError('SEGMENT_REQUEST_MISMATCH')
        checked=verifier.verify_audio(segment['config'],receipt)
        duration=seconds_to_us(checked['duration'])
        if duration <= 0:
            raise ValueError('AUDIO_INVALID')
        local=[]
        for part in checked['timing']:
            start,end=seconds_to_us(part['start']),seconds_to_us(part['end'])
            if end > duration or end <= start:
                raise ValueError('AUDIO_TIMING_INVALID')
            row=dict(segment_id=segment_id,text=part['text'],start_us=offset+start,end_us=offset+end)
            subtitles.append(row);local.append(row)
        lookup[segment_id]=dict(timing=local,start_us=offset,end_us=offset+duration)
        audio.append(dict(segment_id=segment_id,path=checked['path'],sha256=checked['sha256'],
                          start_us=offset,end_us=offset+duration,mode=segment['mode'],request_id=checked.get('request_id')))
        segments.append(dict(segment_id=segment_id,start_us=offset,end_us=offset+duration))
        offset+=duration
    bound,gaps,ids=[],[],set()
    for shot in shots:
        shot_id=shot.get('shot_id');segment_id=shot.get('segment_id')
        if not isinstance(shot_id,str) or not shot_id or shot_id in ids:
            raise ValueError('SHOT_ID_INVALID')
        ids.add(shot_id)
        segment=lookup.get(segment_id)
        first,last=shot.get('first_timing'),shot.get('last_timing')
        if (segment is None or type(first) is not int or type(last) is not int
                or not 0<=first<=last<len(segment['timing'])):
            gaps.append(dict(shot_id=shot_id,segment_id=segment_id,code='MEASURED_ANCHOR_UNAVAILABLE'))
            continue
        start,end=segment['timing'][first]['start_us'],segment['timing'][last]['end_us']
        source_start,source_end=shot.get('source_start_us'),shot.get('source_end_us')
        if any(type(v) is not int for v in (source_start,source_end)) or not 0<=source_start<source_end:
            raise ValueError('SOURCE_WINDOW_INVALID')
        needed=end-start
        if source_end-source_start < needed:
            gaps.append(dict(shot_id=shot_id,segment_id=segment_id,code='SOURCE_WINDOW_TOO_SHORT',
                             shortfall_us=needed-(source_end-source_start),start_us=start,end_us=end))
            continue
        bound.append(dict(shot_id=shot_id,segment_id=segment_id,start_us=start,end_us=end,
                          source_start_us=source_start,source_end_us=source_start+needed))
    return dict(task_id=plan['task_id'],base_revision=plan['base_revision'],plan_digest=plan['digest'],
                duration_us=offset,segments=segments,audio=audio,subtitles=subtitles,shots=bound,gaps=gaps)
