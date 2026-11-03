"""Strict read-only storyboard inventory; never finalize, reconcile or replay jobs.

Standalone recommendation queues did not record a per-job output digest. Their
current AND archived task-owned outputs are inventoried without inventing that
link. Recovery requests retain an exact digest; Agent ads retain job/artifact IDs.
"""
import hashlib
import json
from pathlib import Path
import re

from .upgrade_recovery import RecoveryPlanError, _plain_path
from .upgrade_frozen_references import _observe

_SHA = re.compile(r'[0-9a-f]{64}')
_DIGITAL = {'task_id','script_sha256','transcription_digest','words_sha256','source_snapshot_ids','profile'}
_DIGITAL_OPTIONAL = {'model_profile_digest','packaging_profile_digest'}
_AD = {'task_id','script_sha256','source_snapshot_ids','ratio','general_model','voice'}
_AD_OPTIONAL = {'general_profile_digest','tts_profile_digest'}
_AGENT = {'task_id','script_sha256','source_snapshot_ids','ratio','job_id','artifact_id'}
_META = {'input_digest','source','state','brief_sha256','plan_sha256','missing','scene_card_count'}
_AD_META = {'input_digest','source','artifact_sha256','scene_card_count','missing_count'}
_TRANS = {'task_id','script_sha256','source_path','source_size_bytes','source_mtime_ns','model'}
_TRANS_OPTIONAL = {'input_digest','words_sha256','source_sha256','srt_sha256','word_count','reconciliation'}


class _Gap(Exception):
    pass


def _fail(code='STORYBOARD_JOB_BINDING_INVALID'):
    raise RecoveryPlanError(code,'分镜原始摘要、产物或任务归属不一致，不能自动接续。')


def _need(condition, code):
    if not condition:
        raise _Gap(code)


def _sha(value):
    return isinstance(value,str) and _SHA.fullmatch(value) is not None


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _json(text):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:_fail('STORYBOARD_JOB_RECORD_INVALID')
            result[key]=value
        return result
    try:return json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _: _fail('STORYBOARD_JOB_RECORD_INVALID'))
    except (ValueError,TypeError,RecursionError):_fail('STORYBOARD_JOB_RECORD_INVALID')


def _object(text):
    value=_json(text)
    if not isinstance(value,dict):_fail('STORYBOARD_JOB_RECORD_INVALID')
    return value


def _file_json(path):
    try:return _object(_plain_path(path).read_text(encoding='utf-8'))
    except (OSError,UnicodeError):_fail('STORYBOARD_JOB_FILE_UNAVAILABLE')


def inspect_storyboard_job_references(db, source_root, tables, bind, pending):
    root=_plain_path(source_root)
    report={'phase':'storyboard-job-reference-inspection','jobs':[],'recognizedJobIds':[],
            'managedFileReferences':[],'unresolved':[],'auditComplete':False,'canReplay':False}
    def gap(identity,code):
        report['unresolved'].append({'table':'job_payloads','rowId':identity,'code':code})
        pending('job_payloads',identity,code)
    def columns(table):
        return {r[1] for r in db.connection.execute(f'PRAGMA table_info("{table}")')} if table in tables else set()
    def rows(table):
        return [dict(r) for r in db.connection.execute(f'SELECT * FROM "{table}"')] if table in tables else []
    if 'jobs' not in tables:return report
    if not {'job_id','job_type','task_id','status','cursor','progress_current','progress_total'}<=columns('jobs'):
        gap('jobs','STORYBOARD_JOB_SCHEMA_UNSUPPORTED');return report
    all_jobs={j['job_id']:j for j in rows('jobs')}
    jobs=[j for j in all_jobs.values() if j['job_type'] in {'recommend_storyboard','recover_storyboard'}]
    required={'job_payloads':{'job_id','payload_json','payload_digest','is_paid','cost_status','outcome_status'},
              'tasks':{'task_id','task_type','orchestration_mode'},
              'task_source_snapshots':{'snapshot_id','task_id','scope_id'},
              'task_source_scopes':{'scope_id','task_id'}}
    if any(not fields<=columns(table) for table,fields in required.items()):
        for job in jobs:gap(job['job_id'],'STORYBOARD_JOB_SCHEMA_UNSUPPORTED')
        return report
    payloads={r['job_id']:r for r in rows('job_payloads')}
    tasks={r['task_id']:r for r in rows('tasks')}
    snapshots={r['snapshot_id']:r for r in rows('task_source_snapshots')}
    scopes={r['scope_id']:r for r in rows('task_source_scopes')}

    for job in jobs:
        identity=job['job_id'];task_id=job['task_id'];kind=job['job_type']
        summary={'jobId':identity,'taskId':task_id,'jobType':kind,'status':job['status'],
                 'frozenBusinessReuseVerified':False,'canReplay':False,
                 'providerEvidence':'unverified','costEvidence':'unverified','inventories':[]}
        report['jobs'].append(summary)
        refs=[];bindings=[]
        def file_reference(path,field,expected=None):
            path=_plain_path(path)
            observed=_observe(path)
            if expected is not None and (not _sha(expected) or observed['sha256']!=expected):
                _fail('STORYBOARD_JOB_FILE_CHANGED')
            item={'table':'job_payloads','rowId':identity,'field':field,'path':str(path)}
            if expected is not None:item['sha256']=expected
            refs.append(item)
            return observed
        try:
            if not isinstance(task_id,str) or not task_id or task_id in {'.','..'} or any(c in task_id for c in ('/','\\',':','\0')):_fail()
            task=tasks.get(task_id)
            if task is None:_fail()
            _need(task['task_type'] in {'digital_human','ad_mix'} and task['orchestration_mode'] in {'standalone','agent'},'STORYBOARD_JOB_ROUTE_UNSUPPORTED')
            row=payloads.get(identity);_need(row is not None,'STORYBOARD_JOB_PAYLOAD_UNAVAILABLE')
            summary.update(outcomeStatus=row['outcome_status'],costStatus=row['cost_status'],
                           providerState=row.get('provider_state'),requestedCostStatus=row.get('requested_cost_status'))
            payload=_object(row['payload_json'])
            _need(_sha(row['payload_digest']),'STORYBOARD_JOB_PAYLOAD_SIGNATURE_UNAVAILABLE')
            if _digest(payload)!=row['payload_digest'] or payload.get('task_id')!=task_id or payload.get('action')!=kind:_fail()
            keys={'action','task_id'}
            if kind=='recover_storyboard':
                keys|={'request_id','local_recovery_only','expected_input_digest'}
                _need(task['task_type']=='digital_human' and task['orchestration_mode']=='standalone','STORYBOARD_JOB_ROUTE_UNSUPPORTED')
                _need(payload.get('local_recovery_only') is True and _sha(payload.get('expected_input_digest')),'STORYBOARD_JOB_PAYLOAD_VERSION_UNSUPPORTED')
                _need(row['is_paid']==0 and row['cost_status'] in {'waived','unknown'},'STORYBOARD_JOB_COST_UNRESOLVED')
            elif task['orchestration_mode']=='agent':
                keys.add('artifact_id')
                _need(task['task_type']=='ad_mix','STORYBOARD_JOB_ROUTE_UNSUPPORTED')
            elif 'request_id' in payload:keys.add('request_id')
            _need(set(payload)==keys,'STORYBOARD_JOB_PAYLOAD_VERSION_UNSUPPORTED')
            _need(all(isinstance(payload[k],str) and payload[k] for k in ('request_id','artifact_id') if k in payload),'STORYBOARD_JOB_PAYLOAD_VERSION_UNSUPPORTED')
            _need(job['status'] in {'completed','failed','cancelled','interrupted'},'STORYBOARD_JOB_OUTCOME_UNRESOLVED')
            _need(row['outcome_status'] in {'not_started','in_progress','confirmed','unknown'},'STORYBOARD_JOB_OUTCOME_VERSION_UNSUPPORTED')
            if kind=='recommend_storyboard':
                _need(row['is_paid']==1 and row['cost_status'] in {'approved','estimated','incurred','unknown','waived'},'STORYBOARD_JOB_COST_UNRESOLVED')
            if job['status']=='completed':
                _need(job['cursor']==kind+':complete' and type(job['progress_current']) is int
                      and job['progress_current']==1 and type(job['progress_total']) is int and job['progress_total']==1,'STORYBOARD_JOB_RESULT_VERSION_UNSUPPORTED')
            else:
                _need(job['cursor'] in {None,kind+':complete'},'STORYBOARD_JOB_RESULT_VERSION_UNSUPPORTED')
            task_root=_plain_path(root/'runtime/workbench/tasks'/task_id)
            directories=[task_root/'artifacts']
            history=task_root/'history/script-revisions'
            if history.exists():
                _plain_path(history)
                directories.extend(_plain_path(p)/'artifacts' for p in sorted(history.iterdir()) if p.is_dir())
            name='storyboard' if task['task_type']=='digital_human' else 'ad'
            candidates=[d/name/'recommendation.json' for d in directories if (d/name/'recommendation.json').exists()]
            _need(bool(candidates),'STORYBOARD_JOB_RESULT_UNAVAILABLE')
            recovery_matches=0;agent_matches=0
            for metadata_path in candidates:
                _plain_path(metadata_path)
                metadata=_file_json(metadata_path)
                if metadata.get('task_id')!=task_id:_fail()
                source=metadata.get('source')
                digital=task['task_type']=='digital_human'
                is_agent=source=='agent_artifact'
                _need(source in {'standalone_model','agent_artifact'},'STORYBOARD_JOB_SOURCE_UNSUPPORTED')
                _need(is_agent==(task['orchestration_mode']=='agent'),'STORYBOARD_JOB_SOURCE_UNSUPPORTED')
                base=_DIGITAL if digital else _AGENT if is_agent else _AD
                optional=_DIGITAL_OPTIONAL if digital else set() if is_agent else _AD_OPTIONAL
                output=_META if digital else _AD_META
                _need(base<=set(metadata) and not set(metadata)-(base|optional|output),'STORYBOARD_JOB_METADATA_VERSION_UNSUPPORTED')
                binding={k:metadata[k] for k in base|optional if k in metadata}
                _need(_sha(metadata.get('input_digest')) and _sha(metadata.get('script_sha256')),'STORYBOARD_JOB_SIGNATURE_UNAVAILABLE')
                if not is_agent and _digest(binding)!=metadata['input_digest']:_fail()
                for k in optional:
                    if k in binding:_need(_sha(binding[k]),'STORYBOARD_JOB_SIGNATURE_UNAVAILABLE')
                snapshot_ids=metadata['source_snapshot_ids']
                _need(isinstance(snapshot_ids,list) and bool(snapshot_ids) and all(isinstance(x,str) for x in snapshot_ids) and len(set(snapshot_ids))==len(snapshot_ids),'STORYBOARD_JOB_SNAPSHOT_VERSION_UNSUPPORTED')
                for snapshot_id in snapshot_ids:
                    snapshot=snapshots.get(snapshot_id)
                    _need(snapshot is not None,'STORYBOARD_JOB_SNAPSHOT_UNAVAILABLE')
                    if snapshot['task_id']!=task_id:_fail()
                    scope=scopes.get(snapshot['scope_id'])
                    _need(scope is not None,'STORYBOARD_JOB_SCOPE_UNAVAILABLE')
                    if scope['task_id']!=task_id:_fail()
                folder=metadata_path.parent;artifact=folder.parent
                archived=artifact!=task_root/'artifacts'
                script=artifact.parent/'previous-script.txt' if archived else task_root/'inputs/final-script.txt'
                prefix=metadata_path.relative_to(task_root).as_posix()
                file_reference(script,prefix+'/script_sha256',metadata['script_sha256'])
                script_text=script.read_text(encoding='utf-8')
                if digital:
                    if metadata.get('state')=='generated':
                        # A failed local finalization still owns signed model outputs.
                        # Preserve them for explicit recovery without proving completion.
                        _need(job['status']!='completed' and 'scene_card_count' not in metadata
                              and metadata.get('missing')==[],'STORYBOARD_JOB_RESULT_VERSION_UNSUPPORTED')
                    else:
                        _need(metadata.get('state')=='complete' and type(metadata.get('scene_card_count')) is int and isinstance(metadata.get('missing'),list),'STORYBOARD_JOB_RESULT_VERSION_UNSUPPORTED')
                    for key in ('brief','plan'):
                        _need(_sha(metadata.get(key+'_sha256')),'STORYBOARD_JOB_SIGNATURE_UNAVAILABLE')
                        file_reference(folder/(key+'.json'),prefix+'/'+key+'_sha256',metadata[key+'_sha256'])
                    from .planning_finalize import validate_digital_storyboard_content
                    validate_digital_storyboard_content({'brief':_file_json(folder/'brief.json'),'plan':_file_json(folder/'plan.json')},script_text)
                    trans=_file_json(artifact/'transcription.json')
                    _need(_TRANS<=set(trans) and not set(trans)-(_TRANS|_TRANS_OPTIONAL),'STORYBOARD_JOB_TRANSCRIPTION_VERSION_UNSUPPORTED')
                    if (trans['task_id']!=task_id or trans['script_sha256']!=metadata['script_sha256']
                        or trans.get('input_digest')!=metadata['transcription_digest']
                        or _digest({k:trans[k] for k in _TRANS})!=metadata['transcription_digest']
                        or trans.get('words_sha256')!=metadata['words_sha256']):_fail()
                    file_reference(artifact/'transcript.words.json',prefix+'/words_sha256',metadata['words_sha256'])
                    _need(_sha(trans.get('source_sha256')) and type(trans.get('source_size_bytes')) is int,'STORYBOARD_JOB_SIGNATURE_UNAVAILABLE')
                    source_path=Path(trans['source_path'])
                    if not source_path.is_absolute() or '..' in source_path.parts:_fail('STORYBOARD_JOB_PATH_INVALID')
                    observed=file_reference(source_path,prefix+'/transcription/source_path',trans['source_sha256'])
                    if observed['size_bytes']!=trans['source_size_bytes']:_fail('STORYBOARD_JOB_FILE_CHANGED')
                    bindings.append(('job_payloads',identity,prefix+'/transcription/source_path',str(source_path),{'size_bytes':trans['source_size_bytes'],'sha256':trans['source_sha256']}))
                    file_reference(artifact/'transcription.json',prefix+'/transcription')
                else:
                    hashes=metadata.get('artifact_sha256')
                    _need(isinstance(hashes,dict) and set(hashes)=={'adplan.json','adtiming.json','voice.mp3'} and all(_sha(v) for v in hashes.values()),'STORYBOARD_JOB_SIGNATURE_UNAVAILABLE')
                    _need(metadata['ratio'] in {'9:16','16:9'} and type(metadata.get('scene_card_count')) is int and type(metadata.get('missing_count')) is int,'STORYBOARD_JOB_RESULT_VERSION_UNSUPPORTED')
                    for filename,expected in hashes.items():file_reference(folder/filename,prefix+'/artifact_sha256/'+filename,expected)
                    from .ad_actions import _validate_ad_outputs
                    _validate_ad_outputs(script_text,metadata['ratio'],folder/'adplan.json',folder/'adtiming.json',folder/'voice.mp3')
                    # Strictly parse JSON too; production validator predates duplicate-key rejection.
                    plan=_file_json(folder/'adplan.json');_file_json(folder/'adtiming.json')
                    if is_agent:
                        _need({'artifact_id','task_id','artifact_type','schema_version','content_json','content_digest','input_digest','status'}<=columns('agent_artifacts'),'STORYBOARD_JOB_AGENT_SCHEMA_UNSUPPORTED')
                        found=[a for a in rows('agent_artifacts') if a['artifact_id']==metadata['artifact_id']]
                        if len(found)!=1:_fail()
                        agent=found[0];content=_object(agent['content_json'])
                        if (agent['task_id']!=task_id or agent['artifact_type']!='ad_storyboard'
                            or agent['input_digest']!=metadata['input_digest'] or _digest(content)!=agent['content_digest']
                            or content.get('plan')!=plan):_fail()
                        _need(agent['schema_version']==1 and agent['status']=='accepted','STORYBOARD_JOB_AGENT_VERSION_UNSUPPORTED')
                        owner_job=all_jobs.get(metadata['job_id'])
                        owner_payload=payloads.get(metadata['job_id'])
                        if not owner_job or owner_job['task_id']!=task_id or owner_job['job_type']!='recommend_storyboard' or not owner_payload:_fail()
                        owner_request=_object(owner_payload['payload_json'])
                        if (_digest(owner_request)!=owner_payload['payload_digest'] or owner_request.get('artifact_id')!=metadata['artifact_id']
                            or owner_request.get('task_id')!=task_id or owner_request.get('action')!='recommend_storyboard'):_fail()
                        if metadata['job_id']==identity:
                            if payload.get('artifact_id')!=metadata['artifact_id']:_fail()
                            agent_matches+=1
                file_reference(metadata_path,prefix)
                summary['inventories'].append({'metadataPath':str(metadata_path),'inputDigest':metadata['input_digest'],'archived':archived,'source':source,'state':metadata.get('state')})
                if kind=='recover_storyboard' and metadata['input_digest']==payload['expected_input_digest']:recovery_matches+=1
            if kind=='recover_storyboard' and recovery_matches!=1:_fail('STORYBOARD_JOB_RECOVERY_BINDING_INVALID')
            if task['orchestration_mode']=='agent' and agent_matches!=1:_fail()
            summary['outputBindingEvidence']='exact_recovery_digest' if kind=='recover_storyboard' else 'agent_job_artifact' if task['orchestration_mode']=='agent' else 'task_owned_inventory'
            for args in bindings:bind(*args)
            report['managedFileReferences'].extend(refs)
            report['recognizedJobIds'].append(identity)
        except _Gap as error:gap(identity,str(error))
        except (OSError,UnicodeError,KeyError,TypeError,ValueError,RuntimeError) as error:
            if isinstance(error,RecoveryPlanError):raise
            _fail('STORYBOARD_JOB_RECORD_INVALID')
    return report
