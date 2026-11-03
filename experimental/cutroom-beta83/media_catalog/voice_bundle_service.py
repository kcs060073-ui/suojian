"""Task-owned full voice lifecycle. Browser never supplies audio or execution proof.

Queue freezes inputs; a claimed server worker alone registers controlled output.
Actual per-request decoded PCM boundaries drive original-text timing. Applying or
restoring uses one SQLite transaction and never revives downstream approvals.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import wave
from urllib.parse import urlsplit

from .voice_draft_service import VoiceDraftError, _owned_draft
from .voice_editing import VoiceEditing, compile_voice_input, _json, _digest
from .voice_task_context import resolve_voice_task_context
from .voice_audio_assembly import assemble_voice_audio, _pcm, _sha, _safe, _assembly_process_runner
from .voice_regeneration import plan_segment_regeneration
from .workbench_worker import enqueue_job
from .model_profiles import get_task_profile_binding


def _service(db,root,task_id):
    # Keep immutable bundle paths below Windows MAX_PATH in managed worktrees.
    return VoiceEditing(db,Path(root)/'runtime/voice'/_digest(task_id)[:20],
        duration_reader=lambda path:_pcm(path)[0]/48000)


def _error(code,message='声音版本或生成证据不一致，请重新加载核对。',status=409):
    raise VoiceDraftError(code,message,status)


def _current(db,root,task_id,draft_id):
    service=_service(db,root,task_id)
    draft=_owned_draft(db,task_id,draft_id,service)
    context=resolve_voice_task_context(db,root,task_id)
    head=service.current(task_id)
    if context.context_digest!=draft['context_digest'] or head['context_digest']!=context.context_digest:
        _error('CONTEXT_CHANGED')
    if draft['status']!='draft':_error('DRAFT_CLOSED')
    if head['revision']!=draft['base_revision']:_error('VERSION_CONFLICT')
    return service,draft,context,head


def _segments(config,stored_segments=()):
    # Genuine request boundaries; never distribute words across guessed times.
    text=config['script']; boundaries=[0]
    for match in re.finditer(r'[。！？!?；;]+(?:\s*)',text):
        end=match.end()
        if not any(rule['start']<end<rule['end'] for rule in config['rules']):boundaries.append(end)
    if boundaries[-1]!=len(text):boundaries.append(len(text))
    if stored_segments:
        if ''.join(row['text'] for row in stored_segments)!=text:_error('VOICE_SCRIPT_SEGMENT_MISMATCH')
        boundaries=[0];cursor=0
        for row in stored_segments:
            cursor+=len(row['text'])
            if any(rule['start']<cursor<rule['end'] for rule in config['rules']):_error('VOICE_RULE_CROSSES_SEGMENT')
            boundaries.append(cursor)
    rows=[]
    for index,(start,end) in enumerate(zip(boundaries,boundaries[1:])):
        if end==start:continue
        rules=[dict(rule,start=rule['start']-start,end=rule['end']-start) for rule in config['rules']
               if start<=rule['start'] and rule['end']<=end]
        substitutions={}
        for rule in rules:substitutions.setdefault(rule['original'],[]).append(rule['reading'])
        segment=compile_voice_input(text[start:end],rules,config['voice'],{'substitutions':substitutions})
        rows.append(dict(segment_id=f'seg{index:04d}',original_start=start,original_end=end,config=segment))
    if ''.join(row['config']['tts_text'] for row in rows)!=config['tts_text']:_error('VOICE_SEGMENT_INPUT_MISMATCH')
    if not rows or len(rows)>100:_error('VOICE_SEGMENT_LIMIT')
    return rows


def _generation_plan(db,root,task_id,draft,context,head,*,current_request_id=None):
    from .voice_registered_reuse import plan_registered_voice_reuse
    service=_service(db,root,task_id)
    source=(head.get('audio') or {}).get('bundle_id')
    if head.get('audio') and not source:_error('VOICE_BUNDLE_UNREGISTERED')
    if source and not db.connection.execute('SELECT 1 FROM voice_registered_bundles WHERE bundle_id=? AND task_id=?',(source,task_id)).fetchone():
        _error('VOICE_BUNDLE_UNREGISTERED')
    parts=_segments(draft['config'],[dict(r) for r in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx',(task_id,context.script_sha256))])
    try:
        planned=plan_registered_voice_reuse(db,service.audio_root,task_id=task_id,context_digest=context.context_digest,
            base_revision=head['revision'],config=draft['config'],segments=parts,source_bundle_id=source,
            registered_reader=lambda task,audio,config,digest:read_registered_voice(db,root,task,audio,config,digest),current_request_id=current_request_id)
    except ValueError as error:_error(str(error))
    return parts,planned,source


def _execution_segments(private):
    parts=private['segments']
    if private.get('schema_version')==1:return parts
    ids=private.get('generation_segment_ids')
    if (private.get('schema_version')!=2 or not isinstance(ids,list) or not ids or len(set(ids))!=len(ids)
        or ids!=[p['segment_id'] for p in parts if p['segment_id'] in ids]
        or private.get('reuse_plan_digest')!=private.get('reuse_plan',{}).get('digest')):_error('VOICE_GENERATION_CONFLICT')
    return [p for p in parts if p['segment_id'] in ids]


def queue_voice_preview(db,root,task_id,draft_id,body):
    if not isinstance(body,dict) or set(body)-{'requestId','confirmPaidCall','maximumRequests'} or 'requestId' not in body:
        _error('VOICE_REQUEST_INVALID','试听请求不接受声音文件、回执或配置。',400)
    operation=body['requestId']
    if not isinstance(operation,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',operation) is None:
        _error('VOICE_REQUEST_INVALID',status=400)
    if body.get('confirmPaidCall') is not True:
        _error('PAID_CONFIRMATION_REQUIRED','生成新版配音会发送需要生成的文案至已冻结的TTS服务并可能计费，请先明确确认。')
    service,draft,context,head=_current(db,root,task_id,draft_id)
    rid='voice-'+_digest([task_id,draft_id,operation]);job_id='job-'+rid
    existing=db.connection.execute('SELECT input_json,job_id FROM voice_generation_runs WHERE request_id=?',(rid,)).fetchone()
    if existing:
        private=json.loads(existing['input_json'])
        if (existing['job_id']!=job_id or any(private.get(k)!=v for k,v in dict(task_id=task_id,draft_id=draft_id,request_id=rid,config=draft['config'],context_digest=context.context_digest,base_revision=head['revision']).items())):_error('VOICE_GENERATION_CONFLICT')
        return dict(get_voice_preview(db,root,task_id,draft_id),jobId=job_id,maximumRequests=len(_execution_segments(private)),costStatus='unknown')
    binding=get_task_profile_binding(db,task_id,'tts')
    parts,planned,source=_generation_plan(db,root,task_id,draft,context,head)
    count=planned['maximum_requests']
    if not count:_error('UNCHANGED_NO_GENERATION','当前声音已对应这份配置，无需重复生成。')
    if 'maximumRequests' in body and (type(body['maximumRequests']) is not int or body['maximumRequests']!=count):
        _error('VOICE_FEE_ESTIMATE_CHANGED','生成次数已变化，请重新核对并确认。')
    private=dict(schema_version=1,task_id=task_id,draft_id=draft_id,request_id=rid,
        context_digest=context.context_digest,base_revision=head['revision'],
        profile_config_digest=binding['configDigest'],config=draft['config'],segments=parts)
    if count<len(parts):
        private.update(schema_version=2,source_bundle_id=source,reuse_plan=planned,reuse_plan_digest=planned['digest'],
            generation_segment_ids=[p['segment_id'] for p in planned['generation_segments']])
    def before(conn):
        _,latest,latest_context,latest_head=_current(db,root,task_id,draft_id)
        checked_parts,checked_plan,checked_source=_generation_plan(db,root,task_id,latest,latest_context,latest_head)
        if checked_parts!=parts or checked_plan!=planned or checked_source!=source:_error('VOICE_GENERATION_CONFLICT')
        try:service._prepare_in_transaction(conn,draft_id,rid,generate_missing_audio=True)
        except ValueError as error:_error(str(error))
        conn.execute("INSERT INTO voice_generation_runs(request_id,job_id,input_json,status) VALUES(?,?,?,'queued')",(rid,job_id,_json(private)))
    # Preparation, frozen run and queue row share the same SQLite transaction.
    enqueue_job(db,job_id=job_id,job_type='tts',task_id=task_id,
        payload={'action':'voice_editing_v1','request_id':rid},cost_status='unknown',
        provider_request_id=rid,retry_policy='never',pre_enqueue_check=before)
    return dict(get_voice_preview(db,root,task_id,draft_id),jobId=job_id,maximumRequests=count,costStatus='unknown')


def _run(db,task_id,request_id):
    row=db.connection.execute('SELECT r.*,d.task_id,d.draft_id,d.status AS draft_status FROM voice_generation_runs r '
      'JOIN voice_edit_requests q ON q.request_id=r.request_id JOIN voice_edit_drafts d ON d.draft_id=q.draft_id '
      'WHERE r.request_id=? AND d.task_id=?',(request_id,task_id)).fetchone()
    if row is None:_error('VOICE_PREVIEW_NOT_FOUND',status=404)
    return dict(row)


def _claim(conn,run,job):
    if run['job_id']!=job.job_id or run['request_id']!=job.payload.get('request_id'):_error('VOICE_WORKER_CLAIM_LOST')
    row=conn.execute('SELECT task_id,job_type,status,worker_id,cancel_requested,lease_expires_at>datetime(\'now\') AS live FROM jobs WHERE job_id=?',(run['job_id'],)).fetchone()
    if (row is None or row['task_id']!=job.task_id or row['job_type']!='tts' or row['status']!='running'
        or row['worker_id']!=job.worker_id or row['cancel_requested'] or not row['live']):_error('VOICE_WORKER_CLAIM_LOST')
    attempt=conn.execute("SELECT 1 FROM job_attempts WHERE job_id=? AND attempt_no=? AND worker_id=? AND status='running'",(job.job_id,job.attempt_no,job.worker_id)).fetchone()
    if not attempt:_error('VOICE_WORKER_CLAIM_LOST')


def _parameters(env):
    protocol=env['TTS_PROTOCOL'];endpoint=urlsplit(env['TTS_ENDPOINT'])
    parameters=dict(protocol=protocol,endpoint=endpoint.scheme+'://'+endpoint.netloc+endpoint.path,
        voice=env['TTS_VOICE_TYPE'],resource_id=None,model=None,cluster=None)
    if protocol=='volcengine_v3':
        parameters['resource_id']=env['TTS_RESOURCE_ID']
        parameters['audio']=dict(format='mp3',speech_rate=0,loudness_rate=0,bit_rate=64000,sample_rate=24000,
             additions=dict(post_process={'pitch':0},disable_markdown_filter=True,enable_latex_tn=False,latex_parser='v2'))
    elif protocol=='volcengine':
        parameters['cluster']=env.get('TTS_CLUSTER') or 'volcano_tts'
        parameters['audio']=dict(format='mp3',speed=1)
    elif protocol=='openai':
        parameters['model']=env['TTS_MODEL'];parameters['audio']=dict(format='mp3',speed=1)
    else:_error('VOICE_PROTOCOL_UNSUPPORTED')
    return parameters


def begin_voice_generation(db,root,job,environment):
    run=_run(db,job.task_id,job.payload['request_id'])
    private=json.loads(run['input_json'])
    service,draft,context,head=_current(db,root,job.task_id,run['draft_id'])
    if private['config']!=draft['config'] or private['base_revision']!=head['revision']:_error('VOICE_GENERATION_CONFLICT')
    params=_parameters(environment)
    if (params['voice']!=draft['config']['voice']['voice'] or environment.get('CUTROOM_TTS_PROFILE_DIGEST')!=private['profile_config_digest']
       or environment.get('CUTROOM_TTS_PROVIDER')!=draft['config']['voice']['provider']):_error('VOICE_GENERATION_INPUT_MISMATCH')
    resource=params['resource_id'] if params['protocol']=='volcengine_v3' else params['model'] if params['protocol']=='openai' else environment.get('CUTROOM_TTS_RESOURCE_ID')
    if resource!=draft['config']['voice']['resource']:_error('VOICE_GENERATION_INPUT_MISMATCH')
    with db.transaction(immediate=True) as conn:
        _claim(conn,run,job)
        _current(db,root,job.task_id,run['draft_id'])
        run=_run(db,job.task_id,run['request_id'])
        if json.loads(run['input_json'])!=private:_error('VOICE_GENERATION_CONFLICT')
        _validate_frozen_plan(db,root,run,private)
        if run['status']!='queued':_error('VOICE_GENERATION_ALREADY_STARTED')
        _claim(conn,run,job);_current(db,root,job.task_id,run['draft_id'])
        if json.loads(_run(db,job.task_id,run['request_id'])['input_json'])!=private:_error('VOICE_GENERATION_CONFLICT')
        req=service.request(run['request_id'])
        if req['status']!='prepared':_error('REQUEST_STATE_CONFLICT')
        conn.execute("UPDATE voice_edit_requests SET status='running' WHERE request_id=?",(run['request_id'],))
        conn.execute("UPDATE voice_generation_runs SET status='running',parameters_json=? WHERE request_id=?",(_json(params),run['request_id']))
    return private


def _validate_frozen_plan(db,root,run,private):
    _,draft,context,head=_current(db,root,run['task_id'],run['draft_id'])
    if (private.get('config')!=draft['config'] or private.get('base_revision')!=head['revision']
        or private.get('context_digest')!=context.context_digest or private.get('profile_config_digest')!=draft['config']['voice']['revision']):_error('VOICE_GENERATION_CONFLICT')
    if private.get('schema_version')==1:
        parts=_segments(draft['config'],[dict(r) for r in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx',(run['task_id'],context.script_sha256))])
        if private.get('segments')!=parts:_error('VOICE_GENERATION_CONFLICT')
        return
    parts,plan,source=_generation_plan(db,root,run['task_id'],draft,context,head,current_request_id=run['request_id'])
    if (parts!=private['segments'] or plan!=private['reuse_plan'] or source!=private['source_bundle_id']
        or plan['digest']!=private['reuse_plan_digest'] or [p['segment_id'] for p in plan['generation_segments']]!=private['generation_segment_ids']):
        _error('VOICE_REUSE_PLAN_CHANGED')


def _generation_root(service,rid):return _safe(service.audio_root/'gen'/_digest(rid)[:32],service.audio_root)


def _load(path):
    if not path.is_file() or path.stat().st_size>4*1024*1024:_error('VOICE_EXECUTION_RECORD_INVALID')
    try:return json.loads(path.read_text('utf-8'))
    except (OSError,ValueError):_error('VOICE_EXECUTION_RECORD_INVALID')


def _verify_execution(private,parameters,result,execution,directory):
    bindings=dict(schema_version=private['schema_version'],executor_version='voice-editing-tts-v1',input_sha256=_digest(private),
      **{k:private[k] for k in ('task_id','draft_id','request_id','context_digest','base_revision','profile_config_digest')},
      compiled_config_fingerprint=private['config']['fingerprint'],script_sha256=private['config']['script_sha256'])
    if private['schema_version']==2:bindings.update(generation_segment_ids=private['generation_segment_ids'],reuse_plan_digest=private['reuse_plan_digest'])
    segments=_execution_segments(private)
    if any(doc.get(k)!=v for doc in (result,execution) for k,v in bindings.items()):_error('VOICE_EXECUTION_BINDING_INVALID')
    if not result.get('run_id') or result.get('run_id')!=execution.get('run_id'):_error('VOICE_EXECUTION_BINDING_INVALID')
    if result.get('status')!='succeeded':_error('VOICE_GENERATION_NOT_COMPLETE')
    rows=result.get('segments');events=execution.get('events')
    if not isinstance(rows,list) or len(rows)!=len(segments) or not isinstance(events,list) or len(events)!=2*len(rows):_error('VOICE_EXECUTION_RECORD_INVALID')
    cursor=0;seen=set();files=[]
    for index,(segment,row) in enumerate(zip(segments,rows)):
        config=segment['config'];intent,finished=events[2*index:2*index+2]
        text_sha=hashlib.sha256(config['tts_text'].encode()).hexdigest()
        actual=_digest(dict(parameters=parameters,tts_text_sha256=text_sha))
        for phase,event,event_index in [('intent',intent,2*index),('result',finished,2*index+1)]:
            expected=dict(schema_version=1,executor_version='tts-controlled-execution-v1',phase=phase,event_index=event_index,
              segment_id=segment['segment_id'],profile_config_digest=private['profile_config_digest'],
              compiled_config_fingerprint=config['fingerprint'],tts_text_sha256=text_sha,actual_input_digest=actual,parameters=parameters)
            if any(event.get(k)!=v for k,v in expected.items()):_error('VOICE_ACTUAL_INPUT_MISMATCH')
        if (not finished.get('client_request_uuid') or finished['client_request_uuid']!=intent.get('client_request_uuid')
            or finished['client_request_uuid'] in seen):_error('VOICE_EXECUTION_ID_INVALID')
        seen.add(finished['client_request_uuid'])
        if finished.get('status')!='succeeded' or finished.get('dispatch_state')!='response_received' or finished.get('error_code') is not None:_error('VOICE_GENERATION_NOT_COMPLETE')
        if not 200<=finished.get('response',{}).get('http_status',0)<300:_error('VOICE_GENERATION_NOT_COMPLETE')
        codes=finished.get('response',{}).get('protocol_codes')
        valid=isinstance(codes,list) and all(type(code) is int for code in codes)
        if parameters['protocol']=='volcengine_v3':valid=valid and 20000000 in codes and set(codes)<={0,20000000}
        elif parameters['protocol']=='volcengine':valid=valid and codes==[3000]
        else:valid=valid and codes==[]
        if not valid:_error('VOICE_PROTOCOL_SUCCESS_REQUIRED')
        decoded=finished.get('decoded') or {}; frames=decoded.get('frame_count')
        if type(frames) is not int or not 0<frames<=48000*3600 or decoded.get('sample_rate')!=48000 or decoded.get('channels')!=1:_error('VOICE_DECODE_INVALID')
        expectedrow=dict(segment_id=segment['segment_id'],original_start=segment['original_start'],original_end=segment['original_end'],
            original_text=config['script'],compiled_config_fingerprint=config['fingerprint'],tts_text_sha256=text_sha,
            actual_input_digest=actual,client_request_uuid=finished['client_request_uuid'],event_indexes=[2*index,2*index+1],
            start_frame=cursor,end_frame=cursor+frames,timing_kind='generated_segment_boundary',decoded=decoded,
            raw_path='raw/'+segment['segment_id']+'.mp3')
        if any(row.get(k)!=v for k,v in expectedrow.items()):_error('VOICE_SEGMENT_RESULT_INVALID')
        path=_safe(directory/row['raw_path'],directory)
        raw=finished.get('raw_audio') or {}
        if not path.is_file() or not 0<path.stat().st_size<=64*1024*1024 or raw!=dict(sha256=_sha(path),size_bytes=path.stat().st_size):_error('VOICE_SOURCE_CHANGED')
        if row.get('raw_sha256')!=raw['sha256'] or row.get('raw_size_bytes')!=raw['size_bytes']:_error('VOICE_SOURCE_CHANGED')
        files.append(dict(path=str(path),sha256=raw['sha256']))
        cursor+=frames
    if result.get('total_frame_count')!=cursor or result.get('sample_rate')!=48000 or result.get('channels')!=1:_error('VOICE_DECODE_INVALID')
    return files


def register_voice_generation(db,root,job,*,runner=None):
    run=_run(db,job.task_id,job.payload['request_id'])
    if run['status']!='running':_error('VOICE_GENERATION_NOT_RUNNING')
    service,draft,context,head=_current(db,root,job.task_id,run['draft_id'])
    private=json.loads(run['input_json']);params=json.loads(run['parameters_json'])
    _validate_frozen_plan(db,root,run,private)
    directory=_generation_root(service,run['request_id'])
    result=_load(_safe(directory/'result.json',directory));execution=_load(_safe(directory/'execution.json',directory))
    source_files=_verify_execution(private,params,result,execution,directory)
    from .tools import find_ffmpeg,default_ffprobe_search_roots
    from .workbench_actions import _run_checked
    ffmpeg=find_ffmpeg(os.environ,search_roots=default_ffprobe_search_roots())
    import uuid
    _claim(db.connection,run,job)
    source_root=_safe(service.audio_root/'pcm'/_digest(run['request_id'])[:32],service.audio_root)
    source_root.mkdir(parents=True,exist_ok=True)
    segment_results={};segments=[];configs=[];files=[]
    for segment,row,source in zip(_execution_segments(private),result['segments'],source_files):
        target=_safe(source_root/(segment['segment_id']+'.wav'),source_root)
        temporary=source_root/(uuid.uuid4().hex+'.tmp.wav')
        try:
            _run_checked(runner or _assembly_process_runner,[str(ffmpeg),'-v','error','-nostdin','-xerror','-err_detect','explode','-n','-i',source['path'],'-map','0:a:0','-vn','-ac','1','-ar','48000','-c:a','pcm_s16le',str(temporary)],cwd=source_root,timeout_seconds=120,max_output_bytes=65536)
            frames,pcm_sha=_pcm(temporary)
            if frames!=row['decoded']['frame_count']:_error('VOICE_DECODE_INVALID')
            # Hold the writer lock across the lease check and atomic replacement,
            # so an obsolete worker cannot replace a newly registered source.
            with db.transaction(immediate=True) as conn:
                _claim(conn,run,job)
                if conn.execute('SELECT 1 FROM voice_registered_bundles WHERE request_id=?',(run['request_id'],)).fetchone():_error('VOICE_GENERATION_CONFLICT')
                os.replace(temporary,target)
        finally:
            if temporary.exists():temporary.unlink()
        config=segment['config'];configs.append(config);segments.append(dict(segment_id=segment['segment_id'],config=config))
        segment_results[segment['segment_id']]=dict(path=target.relative_to(service.audio_root).as_posix(),sha256=_sha(target),
           fingerprint=config['fingerprint'],provider_receipt='controlled:'+run['request_id']+':'+row['client_request_uuid'],
           timing_kind='measured_sentence',timing=[dict(text=config['script'],start=0,end=frames/48000)])
        files.extend([source,dict(path=str(target),sha256=_sha(target))])
    plan=(private['reuse_plan']['plan'] if private['schema_version']==2 else plan_segment_regeneration(job.task_id,private['base_revision'],segments,configs,route='ad_mix'))
    for part in plan['segments']:
        if part['mode']=='generate':segment_results[part['segment_id']]['request_id']=part['request_id']
    from .voice_registered_sources import RegisteredSourceVerifier
    verifier=RegisteredSourceVerifier(service,lambda:_validate_frozen_plan(db,root,run,private))
    if private['schema_version']==2:
        parent=db.connection.execute('SELECT files_json FROM voice_registered_bundles WHERE bundle_id=? AND task_id=?',(private['source_bundle_id'],job.task_id)).fetchone()
        if parent is None:_error('VOICE_REUSE_SOURCE_INVALID')
        files.extend(dict(path=str(service.audio_root/f['path']),sha256=f['sha256']) for f in json.loads(parent['files_json']))
    bundle=assemble_voice_audio(plan,segment_results,verifier,service.audio_root/'versions',runner=runner)
    audio=dict(path=Path(bundle['audio_path']).relative_to(service.audio_root).as_posix(),sha256=bundle['receipt']['sha256'],
        bundle_id=bundle['assembly_id'],request_id=run['request_id'],fingerprint=draft['config']['fingerprint'],
        provider_receipt=('mixed:' if private['schema_version']==2 else 'controlled:')+run['request_id'],timing_kind='measured_sentence',
        timing=[dict(text=r['text'],start=r['start_us']/1000000,end=r['end_us']/1000000) for r in bundle['timeline']['subtitles']])
    audio=service.verify_audio(draft['config'],audio)
    for name in ('voice.wav','receipt.json','timeline.json'):
        path=Path(bundle['directory'])/name;files.append(dict(path=str(path),sha256=_sha(path)))
    for name in ('result.json','execution.json'):
        path=directory/name;files.append(dict(path=str(path),sha256=_sha(path)))
    by_path={}
    for f in files:
        relative=Path(f['path']).relative_to(service.audio_root).as_posix()
        if relative in by_path and by_path[relative]!=f['sha256']:_error('VOICE_SOURCE_CHANGED')
        by_path[relative]=f['sha256']
    if len(by_path)>405:_error('VOICE_REUSE_DEPENDENCY_LIMIT')
    files=[dict(path=path,sha256=sha) for path,sha in by_path.items()]
    with db.transaction(immediate=True) as conn:
        _claim(conn,run,job);_current(db,root,job.task_id,run['draft_id'])
        _validate_frozen_plan(db,root,run,private)
        _verify_files(service.audio_root,files)
        package=Path(bundle['directory'])
        if (_sha(package/'voice.wav')!=audio['sha256'] or audio['sha256']!=bundle['receipt']['sha256']
            or _load(package/'receipt.json')!=bundle['receipt'] or _load(package/'timeline.json')!=bundle['timeline']):
            _error('VOICE_SOURCE_CHANGED')
        latest=_run(db,job.task_id,run['request_id'])
        if (latest['status']!='running' or latest['job_id']!=run['job_id'] or json.loads(latest['input_json'])!=private
            or json.loads(latest['parameters_json'])!=params):_error('VOICE_GENERATION_CONFLICT')
        if _load(directory/'result.json')!=result or _load(directory/'execution.json')!=execution:_error('VOICE_SOURCE_CHANGED')
        _verify_execution(private,params,result,execution,directory)
        _claim(conn,run,job);_current(db,root,job.task_id,run['draft_id'])
        conn.execute('INSERT INTO voice_registered_bundles(bundle_id,task_id,draft_id,request_id,context_digest,config_fingerprint,package_path,audio_json,timeline_json,files_json,execution_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
           (bundle['assembly_id'],job.task_id,run['draft_id'],run['request_id'],context.context_digest,draft['config']['fingerprint'],
            Path(bundle['directory']).relative_to(service.audio_root).as_posix(),_json(audio),_json(bundle['timeline']),_json(files),_digest(execution)))
        conn.execute("UPDATE voice_generation_runs SET status='ready',execution_json=?,bundle_id=? WHERE request_id=? AND status='running'",(_json(execution),bundle['assembly_id'],run['request_id']))
        conn.execute("UPDATE voice_edit_requests SET status='ready',audio_json=?,cost_json=? WHERE request_id=? AND status='running'",(_json(audio),_json({**(service.request(run['request_id']).get('cost') or {}),'status':'unknown'}),run['request_id']))
    return get_voice_preview(db,root,job.task_id,run['draft_id'])


def _verify_files(audio_root,files):
    def verify(item):
        raw=Path(item['path'])
        if raw.is_absolute() or '..' in raw.parts:_error('VOICE_BUNDLE_CORRUPT')
        path=_safe(audio_root/raw,audio_root)
        if not path.is_file() or _sha(path)!=item['sha256']:_error('VOICE_BUNDLE_CORRUPT')
    # Keep every path/content check and all three boundary passes. Independent
    # file I/O can overlap; workers never share a database connection or cache.
    if len(files) < 8:
        for item in files: verify(item)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='voice-verify') as pool:
            for _ in pool.map(verify, files): pass


def registered_voice_cache_snapshot(db,root,task_id,audio,config,context_digest):
    """Internal cache identity only; never authorizes playback, apply or render.

    The caller compares all immutable input file stamps with a previously fully
    verified snapshot. Content verification remains mandatory at write/render gates.
    """
    if not isinstance(audio,dict) or not audio.get('bundle_id'):_error('VOICE_BUNDLE_UNREGISTERED')
    row=db.connection.execute('SELECT b.*,r.status AS run_status,r.execution_json FROM voice_registered_bundles b JOIN voice_generation_runs r ON r.request_id=b.request_id WHERE b.bundle_id=? AND b.task_id=?',(audio['bundle_id'],task_id)).fetchone()
    if row is None:_error('VOICE_BUNDLE_UNREGISTERED')
    if (row['run_status']!='ready' or row['context_digest']!=context_digest or row['config_fingerprint']!=config['fingerprint']
        or json.loads(row['audio_json'])!=audio or _digest(json.loads(row['execution_json']))!=row['execution_digest']):_error('VOICE_BUNDLE_BINDING_INVALID')
    from .voice_audio_assembly import _safe
    service=_service(db,root,task_id);stamps=[]
    files=json.loads(row['files_json'])
    for entry in files:
        path=_safe(service.audio_root/entry['path'],service.audio_root)
        stat=path.stat()
        if not path.is_file():_error('VOICE_BUNDLE_CORRUPT')
        stamps.append([entry['path'],stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino])
    path=_safe(service.audio_root/audio['path'],service.audio_root)
    return dict(path=path,bundle_id=row['bundle_id'],stamp_digest=_digest(stamps))


def read_registered_voice(db,root,task_id,audio,config,context_digest,*,_seen=()):
    if not isinstance(audio,dict) or not audio.get('bundle_id'):_error('VOICE_BUNDLE_UNREGISTERED')
    if audio['bundle_id'] in _seen or len(_seen)>=16:_error('VOICE_REUSE_ANCESTRY_LIMIT')
    from .voice_registered_reuse import _snapshot
    row=_snapshot(db,task_id,audio['bundle_id'])['bundle']
    if row is None:_error('VOICE_BUNDLE_UNREGISTERED')
    if (row['run_status']!='ready' or row['context_digest']!=context_digest or row['config_fingerprint']!=config['fingerprint']
       or json.loads(row['audio_json'])!=audio or _digest(json.loads(row['execution_json']))!=row['execution_digest']):_error('VOICE_BUNDLE_BINDING_INVALID')
    service=_service(db,root,task_id);files=json.loads(row['files_json'])
    _verify_files(service.audio_root,files)
    checked=service.verify_audio(config,audio)
    frames,pcm_sha=_pcm(service.audio_root/audio['path'])
    timeline=json.loads(row['timeline_json'])
    if frames*1000000//48000 not in (timeline['duration_us'],timeline['duration_us']-1):_error('VOICE_BUNDLE_CORRUPT')
    _verify_files(service.audio_root,files)
    value=dict(path=service.audio_root/audio['path'],audio=checked,timeline=timeline,bundle_id=row['bundle_id'])
    from .voice_registered_sources import registered_segment_sources
    value['segment_sources']=registered_segment_sources(db,root,row,config,value,
        lambda task,a,c,d:read_registered_voice(db,root,task,a,c,d,_seen=(*_seen,audio['bundle_id'])))
    _verify_files(service.audio_root,files)
    return value


def read_registered_voice_projection(db,root,task_id,audio,config,context_digest):
    """Display-only; never use this shortcut at playback or write boundaries."""
    from .voice_detail_projection import read_voice_projection
    return read_voice_projection(db,root,task_id,audio,config,context_digest)


def voice_generation_failure_code(execution_json):
    """Expose only known local codes, including for pre-fix failed requests."""
    try:
        execution = json.loads(execution_json or '{}')
        events = execution.get('events', [])
        if not isinstance(events, list): return 'VOICE_GENERATION_FAILED'
        for event in reversed(events):
            if not isinstance(event, dict) or event.get('phase') != 'result': continue
            if event.get('status') == 'failed':
                code = event.get('error_code')
                return code if code in {'TTS_AUDIO_DECODE_FAILED', 'TTS_PROVIDER_REJECTED', 'TTS_HTTP_REJECTED'} else 'VOICE_GENERATION_FAILED'
    except (ValueError, TypeError, AttributeError):
        pass
    return 'VOICE_GENERATION_FAILED'


def get_voice_preview(db,root,task_id,draft_id):
    service=_service(db,root,task_id);draft=_owned_draft(db,task_id,draft_id,service)
    context=resolve_voice_task_context(db,root,task_id);head=service.current(task_id)
    row=db.connection.execute('SELECT request_id FROM voice_edit_requests WHERE draft_id=? ORDER BY rowid DESC LIMIT 1',(draft_id,)).fetchone()
    value=dict(taskId=task_id,draftId=draft_id,status='draft',requestId=None,canApply=False,mediaUrl=None)
    if row is None:return value
    req=service.request(row['request_id']);value.update(requestId=req['request_id'],status=req['status'])
    if req.get('error_code')=='VOICE_REQUEST_RELEASED':return dict(value,status='released',released=True,costStatus='unknown')
    run=_run(db,task_id,req['request_id']);value['maximumRequests']=len(_execution_segments(json.loads(run['input_json'])))
    if req['status']=='failed':value['errorCode']=voice_generation_failure_code(run.get('execution_json'))
    job=db.connection.execute('SELECT status FROM jobs WHERE job_id=?',(run['job_id'],)).fetchone()
    if req['status']=='running' and job and job['status'] in ('interrupted','failed','cancelled'):value['status']='unknown'
    if value['status']=='unknown':
        from .voice_unknown_recovery import recovery_action
        value['recoveryAction']=recovery_action(db,root,task_id,run,req)
    if req['status']=='ready':
        try:
            registered=read_registered_voice(db,root,task_id,req['audio'],draft['config'],context.context_digest)
            selection=draft['config']['rules'][0] if draft['config']['rules'] else None
            private=json.loads(run['input_json'])
            row=next((r for r in private['segments'] if selection and r['original_start']<=selection['start']<selection['end']<=r['original_end']),None)
            if row:
                window=next((r for r in registered['timeline']['segments'] if r['segment_id']==row['segment_id']),None)
                if window:value.update(previewStart=window['start_us']/1000000,previewEnd=window['end_us']/1000000)
            projection=preview_voice_timing(db,root,task_id,req['audio'],draft['config'],context.context_digest)
        except (ValueError,OSError):value.update(status='blocked',reason='VOICE_BUNDLE_UNAVAILABLE');return value
        value.update(mediaUrl=f'/api/tasks/{task_id}/voice-editing/previews/{req["request_id"]}/media',
          canApply=not projection['gaps'] and draft['status']=='draft' and draft['base_revision']==head['revision'] and context.context_digest==draft['context_digest']==head['context_digest'],timingGaps=projection['gaps'])
    return value


def resolve_voice_preview_media(db,root,task_id,request_id):
    run=_run(db,task_id,request_id);service=_service(db,root,task_id)
    req=service.request(request_id);draft=_owned_draft(db,task_id,run['draft_id'],service)
    if req['status']!='ready':_error('VOICE_PREVIEW_NOT_READY',status=404)
    context=resolve_voice_task_context(db,root,task_id)
    return read_registered_voice(db,root,task_id,req['audio'],draft['config'],context.context_digest)['path']


def _effects(db,root,task_id,context_digest):
    def apply(conn,head,config,audio):
        context=resolve_voice_task_context(db,root,task_id)
        if context.context_digest!=context_digest:_error('CONTEXT_CHANGED')
        checked=read_registered_voice(db,root,task_id,audio,config,context_digest)
        projection=preview_voice_timing(db,root,task_id,audio,config,context_digest)
        if projection['gaps']:_error('VOICE_TIMING_REVIEW_REQUIRED','新声音与部分镜头尚无法安全同步，请查看时码缺口。')
        for row in projection['script_updates']:
            conn.execute('UPDATE script_segments SET output_start_us=?,output_end_us=? WHERE segment_id=? AND task_id=?',
                (row['output_start_us'],row['output_end_us'],row['segment_id'],task_id))
        for row in projection['scene_updates']:
            changed=conn.execute('UPDATE scene_cards SET narrative_start=?,narrative_end=?,shot_revision=shot_revision+1,confirm_status=0,updated_at=datetime(\'now\') WHERE card_id=? AND task_id=? AND shot_revision=?',
                (row['narrative_start'],row['narrative_end'],row['card_id'],task_id,row['expected_shot_revision'])).rowcount
            if changed!=1:_error('VOICE_SCENE_REVISION_CHANGED')
        from .workflow import invalidate_from_connection
        # All derived candidate approvals are revoked even when restoring older audio.
        conn.execute('UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?',(task_id,))
        invalidate_from_connection(conn,task_id,'ai_storyboard',reason='voice audio and measured timeline changed')
    return apply


def apply_voice_draft(db,root,task_id,draft_id,body):
    if not isinstance(body,dict) or set(body)!={'expectedRevision','contextDigest'} or type(body['expectedRevision']) is not int:_error('VOICE_REQUEST_INVALID',status=400)
    service=_service(db,root,task_id);draft=_owned_draft(db,task_id,draft_id,service)
    context=resolve_voice_task_context(db,root,task_id);head=service.current(task_id)
    if context.context_digest!=body['contextDigest']:_error('CONTEXT_CHANGED')
    if head['revision']!=body['expectedRevision']:_error('VERSION_CONFLICT')
    state=get_voice_preview(db,root,task_id,draft_id)
    if not state['canApply'] and not(state['status']=='ready' and draft['status']=='applied' and draft['applied_revision']==head['revision']):_error('VOICE_PREVIEW_NOT_READY')
    if draft['status']=='applied':
        with db.transaction(immediate=True) as conn:
            latest=service.current(task_id)
            if latest['revision']!=body['expectedRevision']:_error('VERSION_CONFLICT')
            fresh=resolve_voice_task_context(db,root,task_id)
            read_registered_voice(db,root,task_id,latest['audio'],latest['config'],fresh.context_digest)
            result=latest
    else:
        result=service.apply(draft_id,context.context_digest,_effects(db,root,task_id,context.context_digest))
    return dict(taskId=task_id,revision=result['revision'],voiceId=result['config']['voice']['voice'],bundleId=result['audio']['bundle_id'],requiresNewPreview=True)


def restore_voice_version(db,root,task_id,revision,body):
    if not isinstance(body,dict) or set(body)!={'expectedRevision','contextDigest'} or type(body['expectedRevision']) is not int:_error('VOICE_REQUEST_INVALID',status=400)
    context=resolve_voice_task_context(db,root,task_id)
    if context.context_digest!=body['contextDigest']:_error('CONTEXT_CHANGED')
    service=_service(db,root,task_id)
    row=db.connection.execute('SELECT config_json,audio_json FROM voice_edit_versions WHERE task_id=? AND revision=?',(task_id,revision)).fetchone()
    if row is None or not row['audio_json']:_error('VOICE_VERSION_UNAVAILABLE')
    read_registered_voice(db,root,task_id,json.loads(row['audio_json']),json.loads(row['config_json']),context.context_digest)
    result=service.restore_version(task_id,revision,body['expectedRevision'],context.context_digest,_effects(db,root,task_id,context.context_digest))
    return dict(taskId=task_id,revision=result['revision'],bundleId=result['audio']['bundle_id'],requiresNewPreview=True)


def preview_voice_timing(db,root,task_id,audio,config,context_digest):
    checked=read_registered_voice(db,root,task_id,audio,config,context_digest)
    cards=[dict(r) for r in db.connection.execute('SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx',(task_id,))]
    if not cards:
        return dict(gaps=[],script_updates=[],scene_updates=[],timing=dict(total=checked['timeline']['duration_us']/1000000,
            segments=[dict(i=i,text=r['text'],start=r['start_us']/1000000,end=r['end_us']/1000000,dur=(r['end_us']-r['start_us'])/1000000)
                for i,r in enumerate(checked['timeline']['subtitles'])]))
    service=_service(db,root,task_id);package=service.audio_root/Path(audio['path']).parent
    receipt=_load(package/'receipt.json')
    run=_run(db,task_id,audio['request_id']);private=json.loads(run['input_json'])
    expected=dict(task_id=task_id,base_revision=private['base_revision'],script_sha256=config['script_sha256'],
        assembly_id=audio['bundle_id'],plan_digest=receipt['plan_digest'],manifest_digest=receipt['manifest_digest'],
        audio_sha256=audio['sha256'],config_fingerprints={row['segment_id']:row['config']['fingerprint'] for row in private['segments']})
    script_segments=[dict(r) for r in db.connection.execute('SELECT * FROM script_segments WHERE task_id=? AND script_revision=? ORDER BY idx',(task_id,config['script_sha256']))]
    from .rough_cut import resolved_ad_source_windows
    from .ad_matching import TRANSITION_HANDLE_SECONDS
    sources=resolved_ad_source_windows(db.connection,task_id,include_render_handles=True);windows={}
    for card in cards:
        source=sources.get(card['card_id'])
        if source is None:continue
        asset=db.connection.execute('SELECT quick_fingerprint FROM assets WHERE asset_id=?',(card['asset_id'],)).fetchone()
        lo=round(source['source_start']*1000000);hi=round(source['source_end']*1000000)
        windows[card['card_id']]=dict(asset_id=card['asset_id'],asset_fingerprint=asset['quick_fingerprint'],media_kind=source['media_kind'],
          source_start_us=lo,source_end_us=hi,allowed_start_us=lo,allowed_end_us=hi,transition_handle_us=round(TRANSITION_HANDLE_SECONDS*1000000) if source['media_kind']=='video' else 0,
          validated_transition_end_us=round(source['validated_transition_end']*1000000))
    from .voice_render_timing import project_ad_voice_timing
    return project_ad_voice_timing(dict(assembly_id=audio['bundle_id'],receipt=receipt,timeline=checked['timeline']),expected=expected,
        script=config['script'],script_segments=script_segments,scene_cards=cards,source_windows=windows)


def resolve_ad_render_inputs(db,root,task_id,script,ratio):
    """Same current audio + timing for packaging and formal render; no legacy overwrite."""
    from .voice_ownership import read_voice_ownership
    from .ad_actions import _validate_ad_outputs,_validate_ad_plan
    ad_dir=Path(root)/'runtime/workbench/tasks'/task_id/'artifacts/ad'
    plan_path=ad_dir/'adplan.json';ownership=read_voice_ownership(db,task_id)
    if ownership.branch=='legacy':
        timing_path=ad_dir/'adtiming.json';voice_path=ad_dir/'voice.mp3'
        plan,timing=_validate_ad_outputs(script,ratio,plan_path,timing_path,voice_path)
        return dict(plan=plan,timing=timing,voice_path=voice_path,timing_path=timing_path,files=[plan_path,timing_path,voice_path],identity={'source':'legacy'})
    context=resolve_voice_task_context(db,root,task_id)
    head=_service(db,root,task_id).current(task_id)
    checked=read_registered_voice(db,root,task_id,head['audio'],head['config'],context.context_digest)
    plan=_load(plan_path);metadata=_load(ad_dir/'recommendation.json')
    if metadata.get('script_sha256')!=context.script_sha256 or (metadata.get('artifact_sha256') or {}).get('adplan.json')!=_sha(plan_path):_error('VOICE_ORIGINAL_PLAN_CHANGED')
    _validate_ad_plan(script,ratio,plan)
    projection=preview_voice_timing(db,root,task_id,head['audio'],head['config'],context.context_digest)
    if projection['gaps']:_error('VOICE_TIMING_REVIEW_REQUIRED')
    if [r['text'] for r in projection['timing']['segments']]!=[r['text'] for r in plan['segments']]:_error('VOICE_PLAN_TIMING_MISMATCH')
    package=checked['path'].parent
    return dict(plan=plan,timing=projection['timing'],voice_path=checked['path'],timing_path=package/'timeline.json',
      files=[plan_path,ad_dir/'recommendation.json',package/'voice.wav',package/'receipt.json',package/'timeline.json'],
      identity=dict(source='registered',revision=head['revision'],context_digest=context.context_digest,bundle_id=checked['bundle_id'],audio_sha256=head['audio']['sha256'],timeline_digest=_digest(checked['timeline'])))


def list_voice_versions(db,root,task_id):
    context=resolve_voice_task_context(db,root,task_id);rows=[]
    for row in db.connection.execute('SELECT revision,context_digest,config_json,audio_json,reason FROM voice_edit_versions WHERE task_id=? ORDER BY revision DESC LIMIT 100',(task_id,)):
        config=json.loads(row['config_json']);audio=json.loads(row['audio_json']) if row['audio_json'] else None
        rows.append(dict(revision=row['revision'],voiceId=config['voice']['voice'],reason=row['reason'],
          hasRegisteredAudio=bool(audio and audio.get('bundle_id')),contextMatches=row['context_digest']==context.context_digest,
          mediaUrl=(f'/api/tasks/{task_id}/voice-editing/previews/{audio["request_id"]}/media' if audio and audio.get('bundle_id') else None)))
    return dict(taskId=task_id,versions=rows)


def voice_generation_failure_for_job(db, job_id):
    """Read only the safe failure code from the original failed voice execution."""
    run = db.connection.execute(
        "SELECT execution_json FROM voice_generation_runs WHERE job_id=? AND status='failed'",
        (job_id,),
    ).fetchone()
    return voice_generation_failure_code(run['execution_json']) if run is not None else None
