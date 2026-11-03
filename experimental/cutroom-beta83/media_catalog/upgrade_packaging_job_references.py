"""Backup-only inspection of P09 model requests and local audio measurements.

Terminal unknown provider outcomes remain unknown. No approvals, current input
rebuild, network calls, retries or database mutations are performed here.
"""
import hashlib
import json
from pathlib import Path
from .upgrade_packaging_references import _object, _digest, _address, _fail
from .upgrade_workflow_job_references import canonical

KINDS={'packaging_suggestion','packaging_audio_analysis'}
SUGGESTION_FIELDS={'task_id','request_id','style','execution_mode','queued_at','base_plan_id','requested_roles','visual_mode','visual_digest','profile_id','profile_digest','model_version','prompt_version','prompt_sha256','execution_authorization'}

def inspect_packaging_job_references(db,source_root,tables,bind,pending):
    report=dict(jobs=[],recognizedJobIds=[],unresolved=[],managedFileReferences=[],canReplay=False)
    def rows(table):return [dict(r) for r in db.connection.execute('SELECT * FROM '+table)] if table in tables else []
    payloads={r['job_id']:r for r in rows('job_payloads')}
    tasks={r['task_id']:r for r in rows('tasks')}
    requests={r['request_id']:r for r in rows('model_contract_requests')}
    drafts={r['request_id']:r for r in rows('model_result_drafts')}
    audio={r['job_id']:r for r in rows('packaging_audio_jobs')}
    evidence={(r['source_sha256'],r['method_version']):r for r in rows('packaging_audio_evidence')}
    plans={r['plan_id']:r for r in rows('packaging_plans')}
    links=rows('packaging_recommendation_links')
    def invalid():_fail('FROZEN_BINDING_INVALID','Packaging historical request or persisted result binding changed.')
    def gap(identity,code):
        report['unresolved'].append(dict(table='job_payloads',rowId=identity,code=code));pending('job_payloads',identity,code)
    def observe(identity,path):
        path=_address(str(path));report['managedFileReferences'].append(dict(table='job_payloads',rowId=identity,field='response_audit',path=str(path)))
    for job in rows('jobs'):
        kind=job['job_type']
        if kind not in KINDS:continue
        identity=job['job_id'];task=job.get('task_id');row=payloads.get(identity)
        if row is None:gap(identity,'PACKAGING_JOB_PAYLOAD_UNAVAILABLE');continue
        payload=_object(row.get('payload_json'))
        if canonical(payload)!=row.get('payload_digest'):invalid()
        summary=dict(jobId=identity,jobType=kind,status=job.get('status'),outcomeStatus=row.get('outcome_status'),costStatus=row.get('cost_status'),providerState=row.get('provider_state'),canReplay=False,frozenBusinessReuseVerified=False)
        report['jobs'].append(summary)
        if job.get('status') not in {'completed','failed','cancelled','interrupted'}:
            gap(identity,'JOB_NOT_TERMINAL');continue
        completed=job['status']=='completed'
        if task not in tasks or tasks[task].get('task_type') not in {'ad_mix','digital_human'} or payload.get('task_id')!=task:invalid()
        if kind=='packaging_suggestion':
            if (set(payload)-SUGGESTION_FIELDS or not {'task_id','request_id','profile_id','profile_digest','prompt_version','prompt_sha256','style'}<=set(payload)
                    or any(not isinstance(payload.get(k),str) or not payload[k] for k in ('request_id','profile_id','style','prompt_version'))
                    or any(not _digest(payload.get(k)) for k in ('profile_digest','prompt_sha256'))):
                gap(identity,'PACKAGING_JOB_PAYLOAD_UNSUPPORTED');continue
            if (row.get('is_paid')!=1 or row.get('provider_request_id')!='p09:'+task+':'+payload['request_id']
                    or row.get('outcome_status') not in {'not_started','in_progress','confirmed','unknown'}
                    or row.get('cost_status') not in {'waived','estimated','approved','incurred','unknown'}):
                gap(identity,'PACKAGING_JOB_PAYMENT_UNSUPPORTED');continue
            request=requests.get(payload['request_id'])
            if request is None:gap(identity,'PACKAGING_REQUEST_UNAVAILABLE');continue
            if (request['task_id']!=task or request['client_id']!='provider:'+payload['profile_id'] or request['template_id']!='P09'
                    or request['prompt_version']!=payload['prompt_version'] or request['prompt_sha256']!=payload['prompt_sha256']):invalid()
            value=_object(request['request_json']);context=_object(request['context_json'])
            if (canonical([task,request['client_id'],request['request_json'],request['context_json'],request['prompt_version'],request['prompt_sha256']])!=request['registration_digest']):invalid()
            expected_route={'ad_mix':'ad','digital_human':'digital'}[tasks[task]['task_type']]
            for obj in (value,context):
                if (obj.get('request_id')!=payload['request_id'] or obj.get('input_digest')!=request['input_digest']
                        or not _digest(obj.get('input_digest')) or obj.get('template_id')!='P09' or obj.get('route')!=expected_route
                        or obj.get('schema_version')!='1.0.0'):invalid()
            known={'allowed_operations','base_revision','input_digest','locked_refs','orientation','payload','records','request_id','route','schema_version','template_id'}
            if set(value)!=known or not isinstance(value['records'],list):
                gap(identity,'PACKAGING_REQUEST_VERSION_UNSUPPORTED');continue
            # The registered records embed source images as data URIs; a new local
            # file/remote address layout requires its own explicit adapter.
            if any(not isinstance(record,dict) or (record.get('media_uri') is not None and not str(record['media_uri']).startswith('data:image/')) for record in value['records']):
                gap(identity,'PACKAGING_REQUEST_MEDIA_UNSUPPORTED');continue
            authorization=payload.get('execution_authorization')
            if authorization is not None:
                expected=dict(kind='technical_model_execution',task_id=task,request_id=payload['request_id'],prompt_version=payload['prompt_version'],prompt_sha256=payload['prompt_sha256'],profile_id=payload['profile_id'],profile_digest=payload['profile_digest'],visual_digest=payload.get('visual_digest'),input_digest=request['input_digest'],confirmed_by='explicit_user_action')
                if not isinstance(authorization,dict) or set(authorization)-set(expected)-{'effective_system_sha256'} or any(authorization.get(k)!=v for k,v in expected.items()):invalid()
                if 'effective_system_sha256' in authorization and not _digest(authorization['effective_system_sha256']):invalid()
            draft=drafts.get(payload['request_id'])
            if completed and (job.get('cursor')!='packaging_suggestion:complete' or job.get('progress_current')!=1 or job.get('progress_total')!=1 or draft is None):
                gap(identity,'PACKAGING_JOB_RESULT_UNAVAILABLE');continue
            if draft:
                response=_object(draft['response_json'])
                if canonical(response)!=draft['response_digest']:invalid()
                if (set(response)!={'template_id','status','result','clarifications','evidence_refs','unknowns'} or response.get('template_id')!='P09'
                        or response.get('status') not in {'ok','partial','needs_input','refused'}):
                    gap(identity,'PACKAGING_RESPONSE_VERSION_UNSUPPORTED');continue
                if draft.get('state') not in {'draft','stale','rejected'}:gap(identity,'PACKAGING_RESULT_STATE_UNSUPPORTED');continue
                for link in links:
                    if link['receipt_id']==draft['receipt_id'] and (link.get('task_id')!=task or plans.get(link['plan_id'],{}).get('task_id')!=task):invalid()
                summary['receiptId']=draft['receipt_id']
            parent=Path(getattr(db,'path',Path(source_root)/'runtime/workbench/catalog.sqlite')).parent
            audit=_address(str(parent/'packaging-responses'/(hashlib.sha256(payload['request_id'].encode()).hexdigest()+'.json')))
            if draft is not None and not audit.is_file():
                gap(identity,'PACKAGING_RESPONSE_AUDIT_UNAVAILABLE');continue
            if audit.exists():
                data=_object(audit.read_text(encoding='utf-8'))
                if set(data)!={'requestId','taskId','inputDigest','response'} or data['requestId']!=payload['request_id'] or data['taskId']!=task or data['inputDigest']!=request['input_digest']:invalid()
                if draft and canonical(data['response'])!=draft['response_digest']:invalid()
                observe(identity,audit)
            summary['requestId']=payload['request_id']
        else:
            if set(payload)!={'task_id','input_digest'} or not _digest(payload.get('input_digest')):
                gap(identity,'PACKAGING_AUDIO_PAYLOAD_UNSUPPORTED');continue
            if row.get('is_paid')!=0 or row.get('cost_status')!='waived' or row.get('provider_request_id'):
                gap(identity,'PACKAGING_AUDIO_PAYMENT_UNSUPPORTED');continue
            record=audio.get(identity)
            if record is None:gap(identity,'PACKAGING_AUDIO_RESULT_UNAVAILABLE');continue
            if record['task_id']!=task or record['input_digest']!=payload['input_digest']:invalid()
            wrapped=_object('{"items":'+record['items_json']+'}')
            items=wrapped['items']
            if not isinstance(items,list) or not items:gap(identity,'PACKAGING_AUDIO_ITEMS_UNSUPPORTED');continue
            valid=True;refs=set()
            for item in items:
                fields={'ref','name','role','binding','source_sha256','file_stamp','problem','status','evidence'}
                if (not isinstance(item,dict) or set(item)!=fields or not isinstance(item['ref'],str) or item['ref'] in refs
                        or item['role'] not in {'source_voice','bgm'} or not _digest(item['binding'])
                        or item['status'] not in {'pending','running','completed','failed','stale'}):valid=False;break
                refs.add(item['ref']);sha=item['source_sha256'];stamp=item['file_stamp']
                if sha is not None and not _digest(sha):invalid()
                if stamp is not None and (not isinstance(stamp,list) or len(stamp)!=5 or not _digest(stamp[0]) or any(type(x) is not int or x<0 for x in stamp[1:])):invalid()
                measurement=item['evidence']
                if item['status']=='completed':
                    if not isinstance(measurement,dict) or measurement.get('status')!='completed' or measurement.get('source_sha256')!=sha or measurement.get('method_version')!='pcm-energy-envelope-v1':invalid()
                    stored=evidence.get((sha,'pcm-energy-envelope-v1'))
                    if stored is None or _object(stored['evidence_json'])!=measurement:invalid()
                elif measurement is not None:invalid()
            if not valid:gap(identity,'PACKAGING_AUDIO_ITEMS_UNSUPPORTED');continue
            if completed and (job.get('cursor') not in {'packaging_audio:complete','packaging_audio:cancelled','packaging_audio:stale'} or job.get('progress_total')!=len(items)
                    or type(job.get('progress_current')) is not int or not 0<=job['progress_current']<=len(items)):
                gap(identity,'PACKAGING_AUDIO_RESULT_UNSUPPORTED');continue
            summary['sourceBindingEvidence']='recorded_digest_and_path_hash_only'
            summary['itemCount']=len(items)
        report['recognizedJobIds'].append(identity)
    report['auditComplete']=not report['unresolved']
    return report
