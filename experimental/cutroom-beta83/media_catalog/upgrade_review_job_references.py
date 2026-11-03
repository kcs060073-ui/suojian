"""Read-only historical content/audio review inventory, never replay or approve.

Terminal uncertainty is preserved exactly. Original hashes are checked where
recorded; unsigned film receipts are explicitly only current backup observations.
"""
import hashlib
import json
import math
from pathlib import Path
import re

from .upgrade_recovery import RecoveryPlanError, _plain_path
from .upgrade_frozen_references import _observe, _signature

_INPUTS={'candidate_id','qa_snapshot_id','qa_report_id','video_sha256','script_sha256'}
_CONTENT=_INPUTS|{'task_id','job_id','request_id','profile_id','profile_digest','model_version','prompt_version','prompt_sha256'}
_AUDIO_BINDING=_INPUTS|{'duration_seconds','start_seconds','end_seconds','prompt_version','prompt_sha256','model','request_parameters'}
_AUDIO=_AUDIO_BINDING|{'task_id','job_id','request_id','inputs_digest','account_id','account_digest'}
_TERMINAL={'completed','failed','cancelled','interrupted'}


class _Gap(Exception):pass

def _fail(code='REVIEW_JOB_BINDING_INVALID'):
    raise RecoveryPlanError(code,'检查请求、冻结候选或已保存回执不一致，不能自动接续。')

def _need(value,code):
    if not value:raise _Gap(code)

def _sha(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None

def _digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _json(text):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:_fail('REVIEW_JOB_RECORD_INVALID')
            value[key]=item
        return value
    try:return json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _: _fail('REVIEW_JOB_RECORD_INVALID'))
    except (ValueError,TypeError,RecursionError):_fail('REVIEW_JOB_RECORD_INVALID')

def _object(text):
    value=_json(text)
    if not isinstance(value,dict):_fail('REVIEW_JOB_RECORD_INVALID')
    return value

def _read(path):
    try:return _object(_plain_path(path).read_text(encoding='utf-8'))
    except (OSError,UnicodeError):_fail('REVIEW_JOB_FILE_UNAVAILABLE')

def _number(value):return type(value) in (int,float) and math.isfinite(value)


def inspect_review_job_references(db,source_root,tables,bind,pending):
    root=_plain_path(source_root)
    report={'phase':'review-job-reference-inspection','jobs':[],'recognizedJobIds':[],
            'managedFileReferences':[],'unresolved':[],'auditComplete':False,'canReplay':False}
    def gap(identity,code):
        report['unresolved'].append({'table':'job_payloads','rowId':identity,'code':code});pending('job_payloads',identity,code)
    def columns(table):return {r[1] for r in db.connection.execute(f'PRAGMA table_info("{table}")')} if table in tables else set()
    def rows(table):return [dict(r) for r in db.connection.execute(f'SELECT * FROM "{table}"')] if table in tables else []
    if 'jobs' not in tables:return report
    if not {'job_id','task_id','job_type','status','cursor'}<=columns('jobs'):
        gap('jobs','REVIEW_JOB_SCHEMA_UNSUPPORTED');return report
    jobs=[j for j in rows('jobs') if j['job_type'] in {'content_review','audio_review'}]
    required={'job_payloads':{'job_id','payload_json','payload_digest','is_paid','outcome_status','cost_status'},
              'tasks':{'task_id'},'candidate_versions':{'candidate_id','task_id','qa_snapshot_id','qa_report_id','output_path','output_signature_json'},
              'render_snapshots':{'snapshot_id','task_id','stage','output_path','params_json'},
              'qa_reports':{'report_id','task_id','video_path','report_json'}}
    if any(not fields<=columns(table) for table,fields in required.items()):
        for job in jobs:gap(job['job_id'],'REVIEW_JOB_SCHEMA_UNSUPPORTED')
        return report
    payloads={r['job_id']:r for r in rows('job_payloads')};tasks={r['task_id'] for r in rows('tasks')}
    candidates={r['candidate_id']:r for r in rows('candidate_versions')}
    snapshots={r['snapshot_id']:r for r in rows('render_snapshots')};qas={r['report_id']:r for r in rows('qa_reports')}
    for job in jobs:
        identity=job['job_id'];task_id=job['task_id'];kind=job['job_type'];refs=[];bindings=[]
        summary={'jobId':identity,'taskId':task_id,'jobType':kind,'status':job['status'],
                 'canReplay':False,'frozenBusinessReuseVerified':False,'providerEvidence':'unverified','costEvidence':'unverified'}
        report['jobs'].append(summary)
        def observe(path,field,expected=None,table='job_payloads',row_id=None):
            path=_plain_path(path);actual=_observe(path)
            if expected is not None and (not _sha(expected) or expected!=actual['sha256']):_fail('REVIEW_JOB_FILE_CHANGED')
            item={'table':table,'rowId':row_id or identity,'field':field,'path':str(path)}
            if expected is not None:item['sha256']=expected
            refs.append(item);return actual
        try:
            if not isinstance(task_id,str) or re.fullmatch('[A-Za-z0-9_-]{1,160}',task_id) is None or task_id not in tasks:_fail()
            row=payloads.get(identity);_need(row is not None,'REVIEW_JOB_PAYLOAD_UNAVAILABLE')
            p=_object(row['payload_json'])
            _need(_sha(row['payload_digest']),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
            if _digest(p)!=row['payload_digest'] or p.get('task_id')!=task_id or p.get('job_id')!=identity:_fail()
            _need(job['status'] in _TERMINAL,'REVIEW_JOB_NOT_TERMINAL')
            _need(row['is_paid']==1 and row['outcome_status'] in {'not_started','in_progress','confirmed','unknown'}
                  and row['cost_status'] in {'unknown','approved','estimated','incurred','waived'},'REVIEW_JOB_OUTCOME_VERSION_UNSUPPORTED')
            summary.update(outcomeStatus=row['outcome_status'],costStatus=row['cost_status'],providerState=row.get('provider_state'))
            if kind=='content_review':
                mode=p.get('review_mode','sparse');expected=set(_CONTENT)
                if 'supplemental' in p:
                    expected.add('supplemental');_need(p['supplemental'] is True,'REVIEW_JOB_PAYLOAD_VERSION_UNSUPPORTED')
                if mode in {'timeline','audiovisual'}:expected|={'review_mode','window_count'}
                if mode=='audiovisual':expected|={'audio_account_id','audio_account_digest'}
                _need(mode in {'sparse','timeline','audiovisual'} and set(p)==expected,'REVIEW_JOB_PAYLOAD_VERSION_UNSUPPORTED')
                _need(p['prompt_version'] in ({'content-review-dev-v1'} if mode=='sparse' else {'film-review-v2','film-review-v3'}),'REVIEW_JOB_PROTOCOL_UNSUPPORTED')
                _need(_sha(p['profile_digest']),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
                if mode=='audiovisual':_need(_sha(p['audio_account_digest']),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
            else:
                _need(set(p)==_AUDIO and p['prompt_version']=='audio-review-natural-v1' and p['model']=='glm-4-voice'
                      and p['request_parameters']=={'max_tokens':1024,'stream':False},'REVIEW_JOB_PAYLOAD_VERSION_UNSUPPORTED')
                _need(_sha(p['inputs_digest']) and _sha(p['account_digest']),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
                if _digest({k:p[k] for k in _AUDIO_BINDING})!=p['inputs_digest']:_fail()
            _need(all(_sha(p[k]) for k in ('video_sha256','script_sha256','prompt_sha256')),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
            _need(all(isinstance(p[k],str) and p[k] for k in ('candidate_id','qa_snapshot_id','qa_report_id','request_id')),'REVIEW_JOB_PAYLOAD_VERSION_UNSUPPORTED')
            candidate=candidates.get(p['candidate_id']);qa=qas.get(p['qa_report_id']);snapshot=snapshots.get(p['qa_snapshot_id'])
            _need(candidate is not None and qa is not None and snapshot is not None,'REVIEW_JOB_CANDIDATE_UNAVAILABLE')
            if (any(r['task_id']!=task_id for r in (candidate,qa,snapshot)) or candidate['qa_snapshot_id']!=p['qa_snapshot_id']
                or candidate['qa_report_id']!=p['qa_report_id'] or snapshot['stage']!='qa'):_fail()
            video=_plain_path(candidate['output_path']);signature=_signature(_object(candidate['output_signature_json']))
            if signature['sha256']!=p['video_sha256'] or _plain_path(qa['video_path'])!=video or _plain_path(snapshot['output_path'])!=video:_fail()
            parameters=_object(snapshot['params_json']);qa_result=_object(qa['report_json'])
            if parameters.get('output_signature')!=signature or parameters.get('qa_report_id')!=p['qa_report_id'] or qa_result.get('video_path')!=str(video):_fail()
            observed=observe(video,'output_path',signature['sha256'],'candidate_versions',p['candidate_id'])
            if observed!=signature:_fail('REVIEW_JOB_FILE_CHANGED')
            bindings.append(('candidate_versions',p['candidate_id'],'output_path',str(video),signature))
            duration=(qa_result.get('video_spec') or {}).get('duration')
            _need(_number(duration) and duration>0,'REVIEW_JOB_DURATION_UNAVAILABLE')
            task_root=_plain_path(root/'runtime/workbench/tasks'/task_id)
            histories=task_root/'history/script-revisions'
            archives=[_plain_path(x) for x in sorted(histories.iterdir()) if x.is_dir()] if histories.exists() else []
            scripts=[task_root/'inputs/final-script.txt',*[x/'previous-script.txt' for x in archives]]
            matching=[x for x in scripts if x.is_file() and _observe(_plain_path(x))['sha256']==p['script_sha256']]
            _need(bool(matching),'REVIEW_JOB_SCRIPT_UNAVAILABLE')
            for path in matching:observe(path,'script_sha256',p['script_sha256'])
            artifact_roots=[task_root/'artifacts',*[x/'artifacts' for x in archives]]
            if kind=='content_review':
                _need({'run_id','task_id','candidate_id','qa_snapshot_id','qa_report_id','video_sha256','duration_seconds','executor','prompt_version','model_version','status','coverage_refs_json','uncovered_json','issues_json','unknowns_json','conclusion'}<=columns('content_review_runs'),'REVIEW_JOB_RUN_SCHEMA_UNSUPPORTED')
                cursor=job['cursor'];run_id=cursor[len('content_review:'):] if isinstance(cursor,str) and cursor.startswith('content_review:') else None
                _need(cursor is None or run_id,'REVIEW_JOB_CURSOR_UNSUPPORTED')
                found=[r for r in rows('content_review_runs') if r['run_id']==run_id]
                if not found:
                    _need(job['status']!='completed' and cursor is None,'REVIEW_JOB_RUN_UNAVAILABLE')
                    summary['resultEvidence']='no_run_record'
                else:
                    if len(found)!=1:_fail()
                    run=found[0]
                    if run['task_id']!=task_id or any(run[k]!=p[k] for k in ('candidate_id','qa_snapshot_id','qa_report_id','video_sha256','prompt_version','model_version')) or run['duration_seconds']!=duration:_fail()
                    _need(run['executor']=='vision_service' and run['status'] in {'running','completed','partial','failed'},'REVIEW_JOB_RUN_VERSION_UNSUPPORTED')
                    if job['status']=='completed':_need(run['status'] in {'completed','partial'},'REVIEW_JOB_RESULT_UNAVAILABLE')
                    values={k:_json(run[k+'_json']) for k in ('coverage_refs','uncovered','issues','unknowns')}
                    _need(all(isinstance(values[k],list) for k in values),'REVIEW_JOB_RESULT_VERSION_UNSUPPORTED')
                    _need(all(isinstance(x,str) and bool(x) for k in ('coverage_refs','uncovered','unknowns') for x in values[k]),'REVIEW_JOB_RESULT_VERSION_UNSUPPORTED')
                    from .content_review import _validated_issues
                    _validated_issues(values['issues'],values['coverage_refs'],duration)
                    if run['status'] in {'completed','partial'}:
                        if run['conclusion'] not in {'issues_found','no_issue_in_coverage','insufficient_evidence'} or (run['conclusion']=='issues_found')!=bool(values['issues']):_fail('REVIEW_JOB_RESULT_INVALID')
                    summary.update(runId=run_id,runStatus=run['status'],resultEvidence='stored_run')
                if mode!='sparse':
                    _film_inventory(p,duration,artifact_roots,observe,found[0] if found else None)
            else:
                _need({'run_id','job_id','task_id','candidate_id','inputs_digest','binding_json','account_digest','start_seconds','end_seconds','status','audio_sha256','response_path','response_sha256','parsed_json','stale_at_completion'}<=columns('audio_review_runs'),'REVIEW_JOB_RUN_SCHEMA_UNSUPPORTED')
                start,end=p['start_seconds'],p['end_seconds']
                _need(p['duration_seconds']==duration and _number(start) and _number(end) and 0<=start<end<=duration and end-start<=30,'REVIEW_JOB_WINDOW_INVALID')
                found=[r for r in rows('audio_review_runs') if r['job_id']==identity]
                if not found:
                    _need(job['status']!='completed' and job['cursor'] is None,'REVIEW_JOB_RUN_UNAVAILABLE');summary['resultEvidence']='no_run_record'
                else:
                    if len(found)!=1:_fail()
                    run=found[0];run_id=run['run_id']
                    if not isinstance(run_id,str) or re.fullmatch('[A-Za-z0-9_-]{1,160}',run_id) is None:_fail()
                    if (job['cursor']!='audio_review:'+run_id or run['task_id']!=task_id or any(run[k]!=p[k] for k in ('candidate_id','inputs_digest','account_digest','start_seconds','end_seconds'))
                        or _object(run['binding_json'])!={k:p[k] for k in _AUDIO_BINDING}):_fail()
                    _need(run['status'] in {'preparing','dispatched','unknown','cancelled','failed','structured','unstructured','ambiguous'},'REVIEW_JOB_RUN_VERSION_UNSUPPORTED')
                    _need(run['stale_at_completion'] in (0,1),'REVIEW_JOB_RUN_VERSION_UNSUPPORTED')
                    folders=[a/'audio-review'/run_id for a in artifact_roots if (a/'audio-review'/run_id).exists()]
                    _need(len(folders)<=1,'REVIEW_JOB_RECEIPT_AMBIGUOUS')
                    if folders:
                        folder=_plain_path(folders[0]);request=folder/'request.json'
                        _need(request.is_file(),'REVIEW_JOB_REQUEST_UNAVAILABLE')
                        if _read(request)!={'jobId':identity,'inputsDigest':p['inputs_digest'],'accountDigest':p['account_digest']}:_fail()
                        observe(request,'audio_review/request.json')
                        _need(all(x.name in {'request.json','response.json'} and x.is_file() for x in folder.iterdir()),'REVIEW_JOB_RECEIPT_VERSION_UNSUPPORTED')
                    else:folder=None
                    response=run['response_path'];response_sha=run['response_sha256']
                    if response or response_sha:
                        _need(folder is not None and response and _sha(response_sha),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
                        original=task_root/'artifacts/audio-review'/run_id/'response.json'
                        actual=folder/'response.json'
                        if _plain_path(response) not in {original,actual}:_fail('REVIEW_JOB_RECEIPT_PATH_INVALID')
                        observe(actual,'response_path',response_sha,'audio_review_runs',run_id)
                        if run['parsed_json']:
                            from .audio_review_protocol import parse_audio_response
                            if _object(run['parsed_json'])!=parse_audio_response(actual.read_bytes()):_fail('REVIEW_JOB_PARSED_RECEIPT_CHANGED')
                    else:
                        _need(job['status']!='completed' and not run['parsed_json'],'REVIEW_JOB_RESULT_UNAVAILABLE')
                        _need(folder is None or not (folder/'response.json').exists(),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
                    if run['audio_sha256'] is not None:_need(_sha(run['audio_sha256']),'REVIEW_JOB_SIGNATURE_UNAVAILABLE')
                    if job['status']=='completed':_need(run['status'] in {'structured','unstructured','ambiguous'} and bool(run['parsed_json']),'REVIEW_JOB_RESULT_UNAVAILABLE')
                    summary.update(runId=run_id,runStatus=run['status'],staleAtCompletion=bool(run['stale_at_completion']),resultEvidence='stored_run')
            for args in bindings:bind(*args)
            report['managedFileReferences'].extend(refs);report['recognizedJobIds'].append(identity)
        except _Gap as error:gap(identity,str(error))
        except (OSError,UnicodeError,KeyError,TypeError,ValueError,RuntimeError) as error:
            if isinstance(error,RecoveryPlanError):raise
            _fail('REVIEW_JOB_RECORD_INVALID')
    return report


def _film_inventory(payload,duration,artifact_roots,observe,run):
    from .film_review import timeline_windows,validate_window_answer
    windows=timeline_windows(duration)
    _need(type(payload['window_count']) is int and payload['window_count']==len(windows),'REVIEW_JOB_WINDOW_INVALID')
    model=payload['model_version']
    if payload['review_mode']=='audiovisual':
        _need(model.endswith(' + glm-4-voice'),'REVIEW_JOB_MODEL_VERSION_UNSUPPORTED');model=model[:-len(' + glm-4-voice')]
    binding=hashlib.sha256(json.dumps({**{k:payload[k] for k in _INPUTS},
        'profile':{'model':model,'digest':payload['profile_digest']},'version':payload['prompt_sha256'],
        'audio':payload.get('audio_account_digest')},sort_keys=True).encode()).hexdigest()
    folders=[a/'film-review'/binding for a in artifact_roots if (a/'film-review'/binding).exists()]
    completed=run is not None and run['status'] in {'completed','partial'}
    _need(bool(folders) or not completed,'REVIEW_JOB_FILM_RECEIPTS_UNAVAILABLE')
    for folder in folders:
        folder=_plain_path(folder)
        allowed={'local-audio.json','summary.json'}
        for i in range(len(windows)):
            for prefix in ('window','audio'):
                allowed.update(f'{prefix}-{i}'+suffix for suffix in ('.json','.sent','.failure.json','.response.tmp'))
        _need(all(p.name in allowed and p.is_file() for p in folder.iterdir()),'REVIEW_JOB_FILM_RECEIPT_VERSION_UNSUPPORTED')
        for path in folder.iterdir():
            _plain_path(path)
            if path.name=='local-audio.json':
                value=_read(path)
                if set(value)!={'segments','levels','duration','method'} or value['duration']!=duration or value['method']!='local-small-asr-and-mixed-pcm-v1' or not isinstance(value['segments'],list) or not isinstance(value['levels'],list):_fail('REVIEW_JOB_LOCAL_AUDIO_INVALID')
                for segment in value['segments']:
                    if (not isinstance(segment,dict) or set(segment)!={'start','end','text','probability'}
                        or not isinstance(segment['text'],str) or not _number(segment['probability']) or not 0<=segment['probability']<=1
                        or not _number(segment['start']) or not _number(segment['end']) or not 0<=segment['start']<segment['end']<=duration):_fail('REVIEW_JOB_LOCAL_AUDIO_INVALID')
                for level in value['levels']:
                    if (not isinstance(level,dict) or set(level)!={'start','end','rms_dbfs','peak_dbfs'}
                        or not all(_number(level[k]) for k in level) or not 0<=level['start']<level['end']<=duration):_fail('REVIEW_JOB_LOCAL_AUDIO_INVALID')
            elif path.name=='summary.json':
                value=_read(path)
                if set(value)!={'windows','coverage','uncovered','conclusion','unknowns','issues'} or value['windows']!=windows:_fail('REVIEW_JOB_FILM_SUMMARY_INVALID')
                if run is not None and (any(value[k]!=_json(run[col]) for k,col in [('coverage','coverage_refs_json'),('uncovered','uncovered_json'),('unknowns','unknowns_json'),('issues','issues_json')]) or value['conclusion']!=run['conclusion']):_fail('REVIEW_JOB_FILM_SUMMARY_INVALID')
            elif path.suffix=='.sent':
                if path.read_text(encoding='utf-8')!='provider call started':_fail('REVIEW_JOB_FILM_MARKER_INVALID')
            elif path.name.endswith('.failure.json'):
                value=_read(path)
                if set(value)!={'type','error'} or value['type'] not in {'ProviderOutcomeConfirmedFailure','ProviderRequestNotSent'} or not isinstance(value['error'],str):_fail('REVIEW_JOB_FILM_FAILURE_INVALID')
            elif path.name.startswith('window-') and path.suffix=='.json':
                # Failed runs may retain rejected provider output. Preserve those
                # bytes as backup observations, never as reusable success evidence.
                # Completed/partial runs still require valid bound evidence.
                if completed:
                    index=int(path.stem.split('-')[1]);window=windows[index]
                    answer=_read(path)
                    from .content_review_jobs import review_schema
                    from .visual_schema import validate_visual_schema
                    validate_visual_schema(answer,review_schema(),'json_object_local_schema')
                    script_ref='script:'+payload['script_sha256']
                    audio_ref=f"audio:{window['start']}:{window['end']}:{payload['video_sha256']}"
                    coverage=_json(run['coverage_refs_json']) if completed else []
                    issue_refs={ref for item in answer['issues'] for ref in [item['target_ref'],*item['evidence_refs']]}
                    if completed and not issue_refs<=set(coverage):_fail('REVIEW_JOB_FILM_EVIDENCE_INVALID')
                    frames=[]
                    for ref in set(coverage)|issue_refs:
                        if ref.startswith('frame:'):
                            match=re.fullmatch(r'frame:([0-9]+\.[0-9]{6}):([0-9a-f]{64})',ref)
                            if not match:_fail('REVIEW_JOB_FILM_EVIDENCE_INVALID')
                            position=float(match.group(1))
                            if window['start']<=position<window['end']:
                                frames.append({'ref':ref,'position_seconds':position})
                    validate_window_answer(answer,window,frames,script_ref,audio_ref)
            elif path.name.startswith('audio-') and path.suffix=='.json':
                _need(payload['review_mode']=='audiovisual','REVIEW_JOB_FILM_RECEIPT_VERSION_UNSUPPORTED')
                # A signed failure raw response can be invalid provider output;
                # completed runs must retain a parseable response.
                if completed:
                    from .audio_review_protocol import parse_audio_response
                    parse_audio_response(path.read_bytes())
            observe(path,'film-review/'+binding+'/'+path.name)
        if completed:
            _need((folder/'summary.json').is_file() and (folder/'local-audio.json').is_file()
                  and all((folder/f'window-{i}.json').is_file() for i in range(len(windows))), 'REVIEW_JOB_FILM_RECEIPTS_UNAVAILABLE')
            if payload['review_mode']=='audiovisual':_need(all((folder/f'audio-{i}.json').is_file() for i in range(len(windows))),'REVIEW_JOB_FILM_RECEIPTS_UNAVAILABLE')
