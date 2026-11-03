"""Read-only, exact voice-editing queue records; never replay or approve them.

Only recognizedJobIds may be handed to the public maintenance orchestrator.
Registered PCM and ancestry remain owned by the existing formal voice reader.
Queue completion is not evidence of a real provider call, billing or listening QA.
"""
import hashlib
import json
from pathlib import Path
import re

from .upgrade_recovery import RecoveryPlanError, _plain_path


_SHA = re.compile(r'[0-9a-f]{64}')
_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
_INPUT_FIELDS = {'schema_version', 'task_id', 'draft_id', 'request_id', 'context_digest',
                 'base_revision', 'profile_config_digest', 'config', 'segments'}
_REUSE_FIELDS = {'source_bundle_id', 'reuse_plan', 'reuse_plan_digest', 'generation_segment_ids'}
_COLUMNS = {
    'jobs': {'job_id', 'task_id', 'job_type', 'status', 'cursor', 'progress_current', 'progress_total'},
    'tasks': {'task_id', 'task_type'},
    'voice_generation_runs': {'request_id', 'job_id', 'input_json', 'parameters_json', 'status',
                              'execution_json', 'bundle_id'},
    'voice_registered_bundles': {'bundle_id', 'task_id', 'draft_id', 'request_id', 'context_digest',
                                 'config_fingerprint', 'package_path', 'audio_json', 'timeline_json',
                                 'files_json', 'execution_digest'},
    'voice_edit_heads': {'task_id', 'revision', 'context_digest', 'config_json', 'audio_json'},
    'voice_edit_drafts': {'draft_id', 'task_id', 'base_revision', 'context_digest', 'config_json', 'status'},
    'voice_edit_requests': {'request_id', 'draft_id', 'status', 'fingerprint', 'audio_json'},
}


def _fail(code):
    raise RecoveryPlanError(code, '声音作业原始记录、登记关系或文件证据不一致，不能自动接续。')


def _json_value(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail('VOICE_JOB_RECORD_INVALID')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda _: _fail('VOICE_JOB_RECORD_INVALID'))
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, RecoveryPlanError):
            raise
        _fail('VOICE_JOB_RECORD_INVALID')
    return value


def _object(text):
    value = _json_value(text)
    if not isinstance(value, dict):
        _fail('VOICE_JOB_RECORD_INVALID')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _inspect_terminal_voice(db, root, job, payload_row, run, private, bind, report, gap):
    """Validate interrupted/failed original evidence without completing a run."""
    from .upgrade_packaging_references import _address, _signature
    from .voice_bundle_service import _execution_segments
    identity=job['job_id']
    if (run['status'] not in {'queued','running','failed','unknown'} or run.get('bundle_id')
            or payload_row.get('outcome_status') not in {'not_started','in_progress','unknown','confirmed'}
            or payload_row.get('cost_status') not in {'waived','estimated','approved','incurred','unknown'}):
        gap(identity,'VOICE_JOB_TERMINAL_VERSION_UNSUPPORTED');return False
    draft=db.connection.execute('SELECT * FROM voice_edit_drafts WHERE draft_id=?',(private['draft_id'],)).fetchone()
    request=db.connection.execute('SELECT * FROM voice_edit_requests WHERE request_id=?',(private['request_id'],)).fetchone()
    if (draft is None or request is None or draft['task_id']!=job['task_id'] or request['draft_id']!=private['draft_id']
            or _object(draft['config_json'])!=private['config'] or draft['base_revision']!=private['base_revision']
            or draft['context_digest']!=private['context_digest']):_fail('VOICE_JOB_BINDING_INVALID')
    if private['schema_version']==2:
        source=db.connection.execute('SELECT task_id FROM voice_registered_bundles WHERE bundle_id=?',(private['source_bundle_id'],)).fetchone()
        plan=private.get('reuse_plan')
        if (source is None or source['task_id']!=job['task_id'] or not isinstance(plan,dict)
                or plan.get('task_id')!=job['task_id'] or plan.get('digest')!=private.get('reuse_plan_digest')
                or _digest({k:v for k,v in plan.items() if k!='digest'})!=private.get('reuse_plan_digest')):_fail('VOICE_JOB_BINDING_INVALID')
    generation=_address(str(root/'runtime/voice'/_digest(job['task_id'])[:20]/'gen'/_digest(private['request_id'])[:32]))
    def observed(path,field):
        path=_address(str(path))
        if path.exists():report['managedFileReferences'].append(dict(table='voice_generation_runs',rowId=private['request_id'],field=field,path=str(path)))
    execution=_object(run['execution_json']) if run.get('execution_json') else None
    disk=generation/'execution.json'
    if disk.exists():
        stored=_object(_address(str(disk)).read_text(encoding='utf-8'))
        if execution is not None and stored!=execution:_fail('VOICE_JOB_BINDING_INVALID')
        execution=stored;observed(disk,'execution.json')
    result=None
    for name in ('input.json','result.json'):
        path=generation/name
        if path.exists():
            value=_object(_address(str(path)).read_text(encoding='utf-8'))
            if name=='input.json' and value!=private:_fail('VOICE_JOB_BINDING_INVALID')
            if name=='result.json' and any(value.get(k)!=v for k,v in dict(input_sha256=_digest(private),task_id=job['task_id'],request_id=private['request_id']).items()):_fail('VOICE_JOB_BINDING_INVALID')
            if name=='result.json':result=value
            observed(path,name)
    if result is not None and execution is None:
        gap(identity,'VOICE_JOB_EXECUTION_UNAVAILABLE');return False
    if execution is None:
        # Dispatch may be unknown even without a result. Preserve that exact
        # boundary; no successful output or no-send claim is manufactured.
        return True
    expected=dict(schema_version=private['schema_version'],executor_version='voice-editing-tts-v1',input_sha256=_digest(private),
        **{k:private[k] for k in ('task_id','draft_id','request_id','context_digest','base_revision','profile_config_digest')},
        compiled_config_fingerprint=private['config']['fingerprint'],script_sha256=private['config']['script_sha256'])
    if private['schema_version']==2:expected.update(generation_segment_ids=private['generation_segment_ids'],reuse_plan_digest=private['reuse_plan_digest'])
    if set(execution)!=set(expected)|{'run_id','events'} or not isinstance(execution.get('events'),list):
        gap(identity,'VOICE_JOB_EXECUTION_VERSION_UNSUPPORTED');return False
    if any(execution.get(k)!=v for k,v in expected.items()):_fail('VOICE_JOB_BINDING_INVALID')
    parameters=_object(run['parameters_json'])
    segments=_execution_segments(private);events=execution['events']
    if len(events)>2*len(segments):_fail('VOICE_JOB_BINDING_INVALID')
    fields={'schema_version','executor_version','phase','event_index','segment_id','profile_config_digest','compiled_config_fingerprint','tts_text_sha256','actual_input_digest','parameters','client_request_uuid','wire_request_id','started_at','finished_at','dispatch_state','status','error_code','response','raw_audio','decoded'}
    for index,event in enumerate(events):
        if not isinstance(event,dict) or set(event)!=fields:
            gap(identity,'VOICE_JOB_EXECUTION_VERSION_UNSUPPORTED');return False
        segment=segments[index//2];config=segment['config'];text_sha=hashlib.sha256(config['tts_text'].encode()).hexdigest()
        expected_event=dict(schema_version=1,executor_version='tts-controlled-execution-v1',phase='intent' if index%2==0 else 'result',event_index=index,segment_id=segment['segment_id'],profile_config_digest=private['profile_config_digest'],compiled_config_fingerprint=config['fingerprint'],tts_text_sha256=text_sha,actual_input_digest=_digest(dict(parameters=parameters,tts_text_sha256=text_sha)),parameters=parameters)
        if any(event.get(k)!=v for k,v in expected_event.items()):_fail('VOICE_JOB_BINDING_INVALID')
        if event.get('status') not in {'unknown','succeeded','failed'} or event.get('dispatch_state') not in {'not_sent','possibly_sent','sent','response_received'}:
            gap(identity,'VOICE_JOB_EXECUTION_VERSION_UNSUPPORTED');return False
        if not isinstance(event.get('client_request_uuid'),str) or not event['client_request_uuid'] or (index%2 and event['client_request_uuid']!=events[index-1]['client_request_uuid']):_fail('VOICE_JOB_BINDING_INVALID')
        if event.get('raw_audio') is not None:
            segment_id=event['segment_id']
            if not isinstance(segment_id,str) or re.fullmatch(r'seg[0-9]+',segment_id) is None:_fail('VOICE_JOB_BINDING_INVALID')
            path=_address(str(generation/'raw'/(segment_id+'.mp3')))
            if not path.is_file():gap(identity,'VOICE_JOB_PARTIAL_FILE_UNAVAILABLE');return False
            bind('voice_generation_runs',private['request_id'],'execution/events/'+str(index)+'/raw_audio',str(path),_signature(event['raw_audio']))
    if result is not None:
        return _terminal_result(result,execution,expected,segments,parameters,generation,identity,gap)
    return True


def _terminal_result(result, execution, expected, segments, parameters, directory, identity, gap):
    """Check only the saved executor prefix; never promote it to a ready bundle."""
    fields=set(expected)|{'run_id','status','code','failed_segment_id','sample_rate','channels',
                          'total_frame_count','ready_for_application','segments'}
    if set(result)!=fields or result.get('status') not in {'running','succeeded','failed','unknown'}:
        gap(identity,'VOICE_JOB_RESULT_VERSION_UNSUPPORTED');return False
    if (any(result.get(k)!=v for k,v in expected.items()) or not isinstance(result['run_id'],str)
            or not result['run_id'] or result['run_id']!=execution['run_id']):_fail('VOICE_JOB_BINDING_INVALID')
    rows=result['segments'];events=execution['events'];status=result['status']
    if (not isinstance(rows,list) or len(rows)>len(segments) or len(events)<2*len(rows)
            or result['ready_for_application'] is not False or result['sample_rate']!=48000
            or result['channels']!=1 or type(result['total_frame_count']) is not int):_fail('VOICE_JOB_BINDING_INVALID')
    if status in {'running','succeeded'}:
        if result['code'] is not None or result['failed_segment_id'] is not None:_fail('VOICE_JOB_BINDING_INVALID')
    elif (len(rows)>=len(segments) or result['failed_segment_id']!=segments[len(rows)]['segment_id']
          or not isinstance(result['code'],str) or not result['code']):_fail('VOICE_JOB_BINDING_INVALID')
    if status=='succeeded' and (len(rows)!=len(segments) or len(events)!=2*len(rows)):_fail('VOICE_JOB_BINDING_INVALID')
    # Each stored row is produced only after that segment's successful receipt
    # and raw file. A later failed/unknown segment must not invalidate this prefix.
    cursor=0;seen=set()
    for index,row in enumerate(rows):
        if not isinstance(row,dict):_fail('VOICE_JOB_BINDING_INVALID')
        segment=segments[index];config=segment['config'];finished=events[2*index+1]
        decoded=finished.get('decoded');raw=finished.get('raw_audio');response=finished.get('response')
        if not isinstance(decoded,dict) or set(decoded)!={'sample_rate','channels','frame_count','duration_seconds'}:_fail('VOICE_JOB_BINDING_INVALID')
        frames=decoded['frame_count']
        if (type(frames) is not int or not 0<frames<=48000*3600 or decoded['sample_rate']!=48000
                or decoded['channels']!=1 or decoded['duration_seconds']!=frames/48000 or finished['status']!='succeeded'
                or finished['dispatch_state']!='response_received' or finished['error_code'] is not None
                or not isinstance(response,dict) or not isinstance(response.get('http_status'),int)
                or not 200<=response['http_status']<300):_fail('VOICE_JOB_BINDING_INVALID')
        codes=response.get('protocol_codes');protocol=parameters.get('protocol')
        valid=isinstance(codes,list) and all(type(code) is int for code in codes)
        if protocol=='volcengine_v3':valid=valid and 20000000 in codes and set(codes)<={0,20000000}
        elif protocol=='volcengine':valid=valid and codes==[3000]
        else:valid=valid and codes==[]
        if not valid or finished['client_request_uuid'] in seen:_fail('VOICE_JOB_BINDING_INVALID')
        seen.add(finished['client_request_uuid'])
        text_sha=hashlib.sha256(config['tts_text'].encode()).hexdigest()
        expected_row=dict(segment_id=segment['segment_id'],original_start=segment['original_start'],original_end=segment['original_end'],
            original_text=config['script'],compiled_config_fingerprint=config['fingerprint'],tts_text_sha256=text_sha,
            actual_input_digest=_digest(dict(parameters=parameters,tts_text_sha256=text_sha)),client_request_uuid=finished['client_request_uuid'],
            event_indexes=[2*index,2*index+1],raw_path='raw/'+segment['segment_id']+'.mp3',decoded=decoded,
            start_frame=cursor,end_frame=cursor+frames,timing_kind='generated_segment_boundary')
        if set(row)!=set(expected_row)|{'raw_sha256','raw_size_bytes'} or any(row.get(k)!=v for k,v in expected_row.items()):_fail('VOICE_JOB_BINDING_INVALID')
        from .upgrade_packaging_references import _signature, _address
        raw=_signature(raw);path=_address(str(directory/row['raw_path']))
        if not path.is_file():gap(identity,'VOICE_JOB_PARTIAL_FILE_UNAVAILABLE');return False
        if (not 0<path.stat().st_size<=64*1024*1024 or path.stat().st_size!=raw['size_bytes']
                or hashlib.sha256(path.read_bytes()).hexdigest()!=raw['sha256']
                or row['raw_sha256']!=raw['sha256'] or row['raw_size_bytes']!=raw['size_bytes']):_fail('VOICE_JOB_BINDING_INVALID')
        cursor+=frames
    if result['total_frame_count']!=cursor or cursor>48000*3600:_fail('VOICE_JOB_BINDING_INVALID')
    return True


def inspect_voice_job_references(db, source_root, tables, bind, pending) -> dict:
    """Preserve the existing five-argument adapter/callback contract.

    ``bind`` receives only raw-file signatures with size AND SHA recorded in the
    original execution. Other registered files keep their original SHA in the
    report and belong to the public managed-file inventory; no size is invented.
    """
    from .voice_bundle_service import read_registered_voice, _service, _generation_root
    from .voice_registered_reuse import _snapshot

    root = _plain_path(source_root)
    report = {'phase': 'voice-job-reference-inspection', 'jobs': [], 'recognizedJobIds': [],
              'bindings': [], 'managedFileReferences': [], 'unresolved': [],
              'auditComplete': False, 'canReplay': False}

    def gap(identity, code):
        report['unresolved'].append({'table': 'job_payloads', 'rowId': identity, 'code': code})
        pending('job_payloads', identity, code)

    job_columns = {r[1] for r in db.connection.execute('PRAGMA table_info(jobs)')} if 'jobs' in tables else set()
    if 'jobs' in tables and not {'job_id', 'job_type'} <= job_columns:
        gap('jobs', 'VOICE_JOB_SCHEMA_UNSUPPORTED')
        return report
    jobs = {r['job_id']: dict(r) for r in db.connection.execute('SELECT * FROM jobs')} if 'jobs' in tables else {}
    if 'job_payloads' not in tables:
        for identity, job in jobs.items():
            if job.get('job_type') == 'tts':
                gap(identity, 'VOICE_JOB_PAYLOAD_UNAVAILABLE')
        return report
    payload_columns = {row[1] for row in db.connection.execute('PRAGMA table_info(job_payloads)')}
    if not {'job_id', 'payload_json', 'payload_digest'} <= payload_columns:
        gap('job_payloads', 'VOICE_JOB_SCHEMA_UNSUPPORTED')
        return report
    payloads = [dict(row) for row in db.connection.execute('SELECT * FROM job_payloads ORDER BY job_id')]
    payload_ids = {r['job_id'] for r in payloads}
    for identity, job in jobs.items():
        if job.get('job_type') == 'tts' and identity not in payload_ids:
            gap(identity, 'VOICE_JOB_PAYLOAD_UNAVAILABLE')
    missing = set(_COLUMNS) - set(tables)
    incompatible = {name for name, columns in _COLUMNS.items() if name in tables and
                    not columns <= {r[1] for r in db.connection.execute(f'PRAGMA table_info("{name}")')}}

    for row in payloads:
        identity = row['job_id']
        job = jobs.get(identity)
        # A malformed unrelated request belongs to its own adapter.
        if job is not None and job.get('job_type') != 'tts':
            try:
                payload = _object(row['payload_json'])
            except RecoveryPlanError:
                continue
            if payload.get('action') != 'voice_editing_v1':
                continue
        else:
            payload = _object(row['payload_json'])
        if job is None:
            if payload.get('action') == 'voice_editing_v1':
                if 'jobs' not in tables:
                    gap(identity, 'VOICE_JOB_TABLE_UNAVAILABLE')
                    continue
                _fail('VOICE_JOB_BINDING_INVALID')
            continue
        summary = {'jobId': identity, 'taskId': job.get('task_id'), 'jobType': job.get('job_type'),
                   'status': job.get('status'), 'cursor': job.get('cursor'),
                   'payloadDigest': row['payload_digest'], 'adapter': None,
                   'providerEvidence': 'unverified', 'costEvidence': 'unverified',
                   'outcomeStatus': row.get('outcome_status'), 'costStatus': row.get('cost_status'),
                   'providerState': row.get('provider_state'), 'frozenBusinessReuseVerified': False}
        report['jobs'].append(summary)
        if job.get('job_type') != 'tts':
            gap(identity, 'VOICE_JOB_TYPE_UNSUPPORTED'); continue
        if payload.get('action') != 'voice_editing_v1':
            gap(identity, 'VOICE_JOB_ACTION_UNSUPPORTED'); continue
        if set(payload) != {'action', 'request_id'} or not isinstance(payload.get('request_id'), str) or not _REQUEST.fullmatch(payload['request_id']):
            gap(identity, 'VOICE_JOB_PAYLOAD_VERSION_UNSUPPORTED'); continue
        if not _sha(row['payload_digest']):
            gap(identity, 'VOICE_JOB_PAYLOAD_SIGNATURE_UNAVAILABLE'); continue
        if _digest(payload) != row['payload_digest']:
            _fail('VOICE_JOB_BINDING_INVALID')
        if missing:
            gap(identity, 'VOICE_JOB_TABLE_UNAVAILABLE'); continue
        if incompatible:
            gap(identity, 'VOICE_JOB_SCHEMA_UNSUPPORTED'); continue
        if job['status'] not in {'completed', 'failed', 'cancelled', 'interrupted'}:
            gap(identity, 'VOICE_JOB_NOT_TERMINAL'); continue
        if job['status'] == 'completed' and (job['cursor'] != 'voice_editing:ready' or type(job['progress_current']) is not int
                or type(job['progress_total']) is not int or job['progress_current'] != 1 or job['progress_total'] != 1):
            gap(identity, 'VOICE_JOB_RESULT_VERSION_UNSUPPORTED'); continue
        task = db.connection.execute('SELECT task_type FROM tasks WHERE task_id=?', (job['task_id'],)).fetchone()
        if task is None:
            _fail('VOICE_JOB_BINDING_INVALID')
        if task['task_type'] != 'ad_mix':
            gap(identity, 'VOICE_JOB_TASK_ROUTE_UNSUPPORTED'); continue
        runs = db.connection.execute('SELECT * FROM voice_generation_runs WHERE request_id=?', (payload['request_id'],)).fetchall()
        if len(runs) != 1 or runs[0]['job_id'] != identity:
            _fail('VOICE_JOB_BINDING_INVALID')
        run = runs[0]
        private = _object(run['input_json'])
        schema = private.get('schema_version')
        if type(schema) is not int or schema not in (1, 2) or set(private) != (_INPUT_FIELDS | (_REUSE_FIELDS if schema == 2 else set())):
            gap(identity, 'VOICE_JOB_INPUT_VERSION_UNSUPPORTED'); continue
        if private['task_id'] != job['task_id'] or private['request_id'] != payload['request_id']:
            _fail('VOICE_JOB_BINDING_INVALID')
        if job['status'] != 'completed' and run['status'] != 'ready':
            if _inspect_terminal_voice(db, root, job, row, dict(run), private, bind, report, gap):
                summary.update(adapter='voice-editing-terminal-history/v1',requestId=run['request_id'],inputDigest=_digest(private))
                report['recognizedJobIds'].append(identity)
            continue
        if run['status'] != 'ready' or not _sha(run['bundle_id']):
            gap(identity, 'VOICE_JOB_REGISTRATION_UNAVAILABLE'); continue
        registered = db.connection.execute('SELECT * FROM voice_registered_bundles WHERE bundle_id=?', (run['bundle_id'],)).fetchall()
        if not registered:
            gap(identity, 'VOICE_JOB_REGISTRATION_UNAVAILABLE'); continue
        if len(registered) != 1 or registered[0]['task_id'] != job['task_id'] or registered[0]['request_id'] != payload['request_id']:
            _fail('VOICE_JOB_BINDING_INVALID')
        try:
            record = _snapshot(db, job['task_id'], run['bundle_id'])['bundle']
        except ValueError:
            _fail('VOICE_JOB_BINDING_INVALID')
        if record is None or record['job_id'] != identity or private['draft_id'] != record['draft_id']:
            _fail('VOICE_JOB_BINDING_INVALID')
        config = _object(record['draft_config_json'])
        audio = _object(record['audio_json'])
        if (private['config'] != config or private['base_revision'] != record['draft_base_revision']
                or private['context_digest'] != record['context_digest']
                or record['draft_context_digest'] != record['context_digest']):
            _fail('VOICE_JOB_BINDING_INVALID')
        inventory = _json_value(record['files_json'])
        if (not isinstance(inventory, list) or not inventory or len(inventory) > 405
                or any(not isinstance(item, dict) or set(item) != {'path', 'sha256'}
                       or not isinstance(item['path'], str) or not item['path']
                       or not _sha(item['sha256']) for item in inventory)):
            gap(identity, 'VOICE_JOB_FILE_RECORD_VERSION_UNSUPPORTED'); continue
        try:
            checked = read_registered_voice(db, root, job['task_id'], audio, config, record['context_digest'])
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            _fail('VOICE_JOB_VOICE_UNAVAILABLE')
        execution = _object(record['execution_json'])
        service = _service(db, root, job['task_id'])
        generation = _generation_root(service, run['request_id'])
        raw_bindings = []
        for index, event in enumerate(execution['events']):
            if event.get('phase') != 'result':
                continue
            raw = generation/'raw'/(event['segment_id']+'.mp3')
            relative = raw.relative_to(service.audio_root).as_posix()
            entry_index = next((i for i, item in enumerate(inventory) if item['path'] == relative), None)
            expected = event['raw_audio']
            if entry_index is None or inventory[entry_index]['sha256'] != expected['sha256']:
                _fail('VOICE_JOB_BINDING_INVALID')
            field = f'files_json/{entry_index}/path'
            bind('voice_registered_bundles', record['bundle_id'], field, str(raw), dict(expected))
            raw_bindings.append({'table': 'voice_registered_bundles', 'rowId': record['bundle_id'],
                                 'field': field, 'path': str(raw), 'signature': dict(expected),
                                 'evidenceGrade': 'recorded_execution_raw_signature'})
        summary.update(adapter='voice-editing-request/v1', requestId=run['request_id'], draftId=record['draft_id'],
                       bundleId=record['bundle_id'], contextDigest=record['context_digest'],
                       inputSchemaVersion=schema, inputDigest=_digest(private), executionDigest=record['execution_digest'],
                       configFingerprint=record['config_fingerprint'], sourceBundleId=private.get('source_bundle_id'),
                       reusePlanDigest=private.get('reuse_plan_digest'),
                       generatedSegmentIds=private.get('generation_segment_ids', [p['segment_id'] for p in private['segments']]),
                       durationUs=checked['timeline']['duration_us'], resultPersistence='cursor_progress_only')
        report['bindings'].extend(raw_bindings)
        report['managedFileReferences'].extend({'bundleId': record['bundle_id'], 'path': item['path'],
                                                'sha256': item['sha256'], 'sizeEvidence': 'not_recorded'}
                                               for item in inventory)
        report['recognizedJobIds'].append(identity)
    return report
