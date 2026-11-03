"""Root-owned provenance for registered whole and mixed voice packages.

Only immutable task registrations plus actual controlled records authorize inherited
PCM. A package manifest or a metadata cache never authorizes an old source.
"""
from copy import deepcopy
import hashlib,json,time
from pathlib import Path
from .voice_editing import VoiceEditing,_digest
from .voice_regeneration import plan_segment_regeneration
from .voice_audio_assembly import ASSEMBLER_VERSION,FORMAT,_existing,_pcm,_sha,_safe
from .voice_registered_reuse import VERSION,_parts,_snapshot,_registered_segments


def plan_from_sources(task_id,context_digest,base_revision,config,parts,previous_config,previous_parts,entries):
    by_window={(p['segment_id'],p['original_start'],p['original_end']):p for p in previous_parts}
    by_id={entry['segment_id']:entry for entry in entries}
    old=[];origins=[]
    for part in parts:
        sid=part['segment_id'];before=by_window.get((sid,part['original_start'],part['original_end']))
        item=dict(segment_id=sid,config=part['config'])
        if before is not None and previous_config['voice']==config['voice'] and before['config']==part['config']:
            item['audio']=deepcopy(by_id[sid]['audio']);origins.append(deepcopy(by_id[sid]['origin']))
        old.append(item)
    plan=plan_segment_regeneration(task_id,base_revision,old,[p['config'] for p in parts],route='ad_mix')
    generated={p['segment_id'] for p in plan['segments'] if p['mode']=='generate'}
    output=dict(schema_version=1,planner_version=VERSION,status='planned',task_id=task_id,
        context_digest=context_digest,base_revision=base_revision,config_fingerprint=config['fingerprint'],plan=plan,
        generation_segments=[p for p in parts if p['segment_id'] in generated],reuse_sources=origins,
        maximum_requests=len(generated),ready_for_application=False,provider_dispatched=False)
    output['digest']=_digest(output)
    return output


def registered_segment_sources(db,root,record,config,checked,reader):
    # The imports stay here to avoid service/planner import cycles.
    from .voice_bundle_service import _service,_generation_root,_load,_verify_execution,_error,_verify_files
    task=record['task_id'];ctx=record['context_digest'];private=json.loads(record['input_json'])
    service=_service(db,root,task);audio_root=service.audio_root;directory=_generation_root(service,record['request_id'])
    params=json.loads(record['parameters_json']);execution=_load(directory/'execution.json');result=_load(directory/'result.json')
    if (_digest(execution)!=record['execution_digest'] or execution!=json.loads(record['execution_json'])
        or record['run_status']!='ready' or record['request_status']!='ready' or record['draft_status'] not in ('draft','applied')
        or record['run_bundle_id']!=record['bundle_id'] or record['draft_task_id']!=task or record['draft_context_digest']!=ctx
        or json.loads(record['draft_config_json'])!=config or json.loads(record['request_audio_json'])!=checked['audio']
        or record['request_fingerprint']!=config['fingerprint'] or private.get('config')!=config
        or any(private.get(k)!=v for k,v in dict(task_id=task,draft_id=record['draft_id'],request_id=record['request_id'],
            context_digest=ctx,base_revision=record['draft_base_revision'],profile_config_digest=config['voice']['revision']).items())
        or params.get('voice')!=config['voice']['voice']):_error('VOICE_BUNDLE_BINDING_INVALID')
    parts=_parts(config,private.get('segments'))
    _verify_execution(private,params,result,execution,directory)
    if private.get('schema_version')==1:
        _,_,audios,origins=_registered_segments(audio_root,record,task,ctx,lambda *args:checked,time.monotonic()+120)
        return [dict(segment_id=p['segment_id'],config=p['config'],audio=audios[p['segment_id']],origin=o) for p,o in zip(parts,origins)]
    source=private.get('source_bundle_id')
    parent=_snapshot(db,task,source)['bundle'] if isinstance(source,str) else None
    if parent is None or source==record['bundle_id']:_error('VOICE_REUSE_SOURCE_INVALID')
    parent_config=json.loads(parent['draft_config_json']);parent_audio=json.loads(parent['audio_json'])
    previous=reader(task,parent_audio,parent_config,ctx)
    previous_parts=_parts(parent_config,json.loads(parent['input_json']).get('segments'))
    planned=plan_from_sources(task,ctx,private['base_revision'],config,parts,parent_config,previous_parts,previous['segment_sources'])
    if planned!=private.get('reuse_plan') or planned['digest']!=private.get('reuse_plan_digest'):
        _error('VOICE_REUSE_PLAN_CHANGED')
    if private.get('generation_segment_ids')!=[p['segment_id'] for p in planned['generation_segments']]:_error('VOICE_REUSE_PLAN_CHANGED')
    package=_safe(audio_root/record['package_path'],audio_root)
    receipt=_load(package/'receipt.json');timeline=_load(package/'timeline.json')
    inventory={f['path']:f['sha256'] for f in json.loads(record['files_json'])}
    inherited={e['segment_id']:e for e in previous['segment_sources']}
    rows={r['segment_id']:r for r in result['segments']};sources=[];origins=[];results={};paths=[];cursor=0
    for part,planned_part in zip(parts,planned['plan']['segments']):
        sid=part['segment_id'];cfg=part['config']
        if planned_part['mode']=='reuse':
            entry=deepcopy(inherited[sid]);audio=entry['audio'];origin=entry['origin']
        else:
            row=rows[sid];target=_safe(audio_root/'pcm'/_digest(record['request_id'])[:32]/(sid+'.wav'),audio_root)
            frames,pcm_sha=_pcm(target)
            if frames!=row['decoded']['frame_count']:_error('VOICE_DECODE_INVALID')
            audio=service.verify_audio(cfg,dict(path=target.relative_to(audio_root).as_posix(),sha256=_sha(target),fingerprint=cfg['fingerprint'],
                request_id=planned_part['request_id'],provider_receipt='controlled:'+record['request_id']+':'+row['client_request_uuid'],
                timing_kind='measured_sentence',timing=[dict(text=cfg['script'],start=0,end=frames/48000)]))
            origin=dict(segment_id=sid,source_bundle_id=record['bundle_id'],source_request_id=record['request_id'],
                source_execution_digest=record['execution_digest'],source_input_digest=row['actual_input_digest'],
                source_config_fingerprint=cfg['fingerprint'],source_path=audio['path'],source_sha256=audio['sha256'],
                source_raw_sha256=row['raw_sha256'],source_pcm_sha256=pcm_sha,source_start_frame=cursor,source_end_frame=cursor+frames,
                sample_rate=48000,new_tts_execution=False)
            results[sid]=audio
        path=_safe(audio_root/audio['path'],audio_root);frames,pcm_sha=_pcm(path)
        if (inventory.get(audio['path'])!=audio['sha256'] or origin['source_end_frame']-origin['source_start_frame']!=frames
            or origin['source_pcm_sha256']!=pcm_sha):_error('VOICE_REUSE_SOURCE_CHANGED')
        sources.append(dict(segment_id=sid,mode=planned_part['mode'],config_fingerprint=cfg['fingerprint'],audio=audio))
        origins.append(dict(segment_id=sid,config=cfg,audio=audio,origin=origin));paths.append(path);cursor+=frames
    identity=_digest(dict(version=ASSEMBLER_VERSION,format=FORMAT,plan_digest=planned['plan']['digest'],sources=sources,shots=[]))
    if identity!=record['bundle_id'] or package.name!=identity:_error('VOICE_BUNDLE_BINDING_INVALID')
    existing=_existing(package,identity,sources,planned['plan'],results,service,[],paths,time.monotonic()+120)
    if existing['timeline']!=checked['timeline'] or existing['receipt']!=receipt or receipt['sha256']!=checked['audio']['sha256'] or timeline!=checked['timeline']:
        _error('VOICE_BUNDLE_BINDING_INVALID')
    # Every inherited evidence dependency must remain in the mixed inventory.
    for item in json.loads(parent['files_json']):
        if inventory.get(item['path'])!=item['sha256']:_error('VOICE_REUSE_SOURCE_UNREGISTERED')
    _verify_files(audio_root,json.loads(record['files_json']))
    return origins


class RegisteredSourceVerifier:
    """Keep root provenance revalidation when A2 replaces the duration reader."""
    def __init__(self,service,check):self.service=service;self.audio_root=service.audio_root;self.check=check
    def with_duration_reader(self,duration_reader,*,deadline):
        return RegisteredSourceVerifier(VoiceEditing(None,self.audio_root,duration_reader=duration_reader),self.check)
    def verify_audio(self,config,audio):return self.service.verify_audio(config,audio)
    def validate_sources(self):self.check()
