"""Strict read-only inventory of free workflow requests and persisted task outputs.

Historical task artifacts are inventoried as task-owned evidence, never assigned
an invented job ownership and never used to authorize replay or paid execution.
"""
import hashlib
import json
import math
from pathlib import Path

from .upgrade_packaging_references import _object, _digest, _address, _fail
from .upgrade_recovery import _plain_path

KINDS = {"initial_source_check", "sync_sources", "transcribe"}

def canonical(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def inspect_workflow_job_references(db, source_root, tables, bind, pending, *, original_reference_root=None):
    report = dict(jobs=[], recognizedJobIds=[], unresolved=[], managedFileReferences=[], managedDirectoryReferences=[], canReplay=False)
    rows = lambda name: [dict(r) for r in db.connection.execute('SELECT * FROM "'+name+'"')] if name in tables else []
    payloads = {r['job_id']:r for r in rows('job_payloads')}
    tasks = {r['task_id']:r for r in rows('tasks')}
    stages = {(r['task_id'],r['stage_key']):r for r in rows('task_stage_states')}
    snapshots = {r['snapshot_id']:r for r in rows('task_source_snapshots')}
    scopes = {r['scope_id']:r for r in rows('task_source_scopes')}
    settings = {r['task_id']:r for r in rows('task_source_settings')}
    checks = rows('task_source_checks')
    jobs = [dict(r) for r in db.connection.execute('SELECT * FROM jobs ORDER BY rowid')] if 'jobs' in tables else []
    job_order = {job['job_id']: index for index,job in enumerate(jobs)}
    transcription_checks = []
    recovery_records = []
    pending_transcript_tasks = set()
    recovery_history_locations = set()
    recovery_complete = {}
    recovery_sources = {}
    recovery_inherited = {}
    def gap(identity, code):
        item=dict(table='job_payloads',rowId=identity,code=code)
        report['unresolved'].append(item);pending('job_payloads',identity,code)
    def invalid():
        _fail('FROZEN_BINDING_INVALID','Workflow frozen input or output binding does not match.')
    def file_ref(identity, field, path, sha=None):
        path=_address(str(path))
        if not path.is_file(): _fail('FROZEN_FILE_UNAVAILABLE','Workflow evidence file is missing.')
        if sha is not None and (not _digest(sha) or hashlib.sha256(path.read_bytes()).hexdigest()!=sha):
            _fail('FROZEN_FILE_CHANGED','Workflow evidence file differs from its recorded digest.')
        item=dict(table='job_payloads',rowId=identity,field=field,path=str(path))
        if sha is not None:item['sha256']=sha
        report['managedFileReferences'].append(item)
    def recovery_inventory(identity, task_id, payload):
        recovery=payload.get('recovery')
        extension=payload.get('storyboard_recovery')
        if 'recovery' not in payload:
            if 'storyboard_recovery' in payload:invalid()
            return True
        names={'transcript.srt','transcript.words.json','transcription.json',
               'transcript-review.json','.transcription-promotion.json'}
        state_names={'scene_cards','script_segments','storyboard_feedback',
                     'reshoot_requirements','task_reshoot_current'}
        if (not isinstance(recovery,dict)
                or set(recovery) not in ({'files','source_sha256','brief_event_id'},
                                        {'files','source_sha256','brief_event_id','storyboard_state'})
                or not _digest(recovery.get('source_sha256'))
                or not isinstance(recovery.get('brief_event_id'),str)
                or not recovery['brief_event_id']
                or not {'input_digest','script_sha256'}<=set(payload)):
            invalid()
        if ('storyboard_recovery' in payload and extension is None
                or Path(task_id).name!=task_id or task_id in {'.','..'}):invalid()
        gate=next((g for g in rows('task_gate_events') if g.get('task_id')==task_id
                   and g.get('gate_type')=='brief' and g.get('event_id')==recovery['brief_event_id']
                   and g.get('gate_digest')==payload['input_digest']),None)
        if gate is None:invalid()
        brief=_object(gate.get('details_json'))
        if (canonical(brief)!=payload['input_digest']
                or not isinstance(brief.get('final_script'),dict)
                or brief['final_script'].get('sha256')!=payload['script_sha256']
                or not isinstance(brief.get('digital_human_source'),dict)):invalid()
        file_ref(identity,'transcript_recovery/source',
                 _address(brief['digital_human_source'].get('path')),recovery['source_sha256'])
        task=_address(str(Path(source_root)/'runtime/workbench/tasks'/task_id))
        archive=task/'history/transcript-recoveries'/hashlib.sha256(identity.encode('utf8')).hexdigest()[:24]
        sources={}
        relocated_locations=set()
        successors=[]
        for newer,new_task,new_payload in recovery_records:
            if (new_task==task_id and job_order[newer]>job_order[identity]
                    and recovery_complete.get(newer)
                    and (new_payload.get('model') or 'small')==(payload.get('model') or 'small')
                    and all(new_payload.get(key)==payload.get(key) for key in ('input_digest','script_sha256'))
                    and all(new_payload['recovery'][key]==recovery[key] for key in ('brief_event_id','source_sha256'))):
                successors.append(newer)

        def checked_shape(data, extra=False):
            expected={'files','storyboard_state'} if extra else set(recovery)
            if not isinstance(data,dict) or set(data)!=expected:invalid()
            files=data.get('files')
            allowed=[{'storyboard'}] if extra else [names, names|{'storyboard'}]
            if not isinstance(files,dict) or set(files) not in allowed:invalid()
            if not extra and ('storyboard' in files)!=('storyboard_state' in data):invalid()
            if 'storyboard_state' in data:
                state=data['storyboard_state']
                if not isinstance(state,dict) or set(state)!=state_names:invalid()
                for table, records in state.items():
                    if not isinstance(records,list):invalid()
                    columns={r[1] for r in db.connection.execute('PRAGMA table_info("'+table+'")')}
                    for record in records:
                        if (not isinstance(record,dict) or set(record)!=columns
                                or record.get('task_id')!=task_id):invalid()
            for name, signature in files.items():
                if signature is None:continue
                if name!='storyboard':
                    if not _digest(signature):invalid()
                    continue
                if (not isinstance(signature,dict) or set(signature)!={'type','entries'}
                        or signature['type']!='directory' or not isinstance(signature['entries'],dict)):invalid()
                for relative, entry in signature['entries'].items():
                    if (not isinstance(relative,str) or not relative or
                            Path(relative).as_posix()!=relative or relative in {'.','..'}):invalid()
                    _address(relative,archive/'storyboard',allowed=(archive/'storyboard',),relative_only=True)
                    if (not isinstance(entry,dict) or not (
                            entry=={'type':'directory'} or
                            set(entry)=={'type','sha256'} and entry['type']=='file' and _digest(entry['sha256']))):invalid()
            return files

        def signature_matches(path, signature):
            path=_plain_path(path)
            if signature is None:return not path.exists()
            if isinstance(signature,str):
                return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==signature
            if not path.is_dir():return False
            actual={}
            def visit(directory):
                for child in directory.iterdir():
                    child=_plain_path(child);relative=child.relative_to(path).as_posix()
                    if child.is_dir():actual[relative]={'type':'directory'};visit(child)
                    elif child.is_file():actual[relative]={'type':'file','sha256':hashlib.sha256(child.read_bytes()).hexdigest()}
                    else:invalid()
            visit(path)
            return actual==signature['entries']

        def formal_archives():
            # Only a committed product rework may relocate a pending recovery's
            # originals. Never discover ownership by walking arbitrary history.
            result=[]
            reference_task=_plain_path(Path(original_reference_root or source_root)/'runtime/workbench/tasks'/task_id)
            for row in rows('rework_requests'):
                if row.get('task_id')!=task_id or row.get('status')!='applied':continue
                impact=_object(row.get('impact_json'))
                family=None;registered=None
                if impact.get('script_revised') is True:
                    family='script-revisions';registered=impact.get('script_archive')
                    receipt=impact.get('script_commit_receipt')
                elif impact.get('source_revised') is True:
                    family='source-revisions';receipt=impact.get('source_commit_receipt')
                    registered=receipt.get('archive') if isinstance(receipt,dict) else None
                else:continue
                if (not isinstance(receipt,dict) or receipt.get('taskId')!=task_id
                        or receipt.get('reworkId')!=row['rework_id'] or receipt.get('status')!='applied'):invalid()
                declared=_address(registered)
                if (declared.parent!=reference_task/'history'/family or len(declared.name)!=32
                        or any(c not in '0123456789abcdef' for c in declared.name)):invalid()
                actual=_plain_path(task/'history'/family/declared.name)
                metadata=[]
                if family=='script-revisions':
                    previous=_plain_path(actual/'previous-script.txt')
                    if not previous.is_file() or hashlib.sha256(previous.read_bytes()).hexdigest()!=payload['script_sha256']:continue
                    committed=impact.get('script_commit_task')
                    if not isinstance(committed,dict) or committed.get('task_id')!=task_id:invalid()
                    prior=dict(committed.get('brief') or {});prior['final_script']=brief['final_script']
                    if canonical(prior)!=payload['input_digest']:continue
                    cards=_plain_path(actual/'scene-cards.json')
                    if _object(cards.read_text(encoding='utf8')).get('rework_id')!=row['rework_id']:invalid()
                    metadata=[previous,cards]
                else:
                    revision=_plain_path(actual/'revision.json')
                    recorded=_object(revision.read_text(encoding='utf8'))
                    if recorded.get('rework_id')!=row['rework_id']:invalid()
                    if (recorded.get('previous_brief_digest')!=payload['input_digest']
                            or recorded.get('previous_source')!=brief['digital_human_source']):continue
                    metadata=[revision]
                result.append((actual/'artifacts',metadata))
            return result

        def inventory(folder, data, *, extra=False):
            files=checked_shape(data,extra)
            folder=_plain_path(folder)
            receipt=_plain_path(folder/'recovery.json')
            original_root=_plain_path(task/'artifacts')
            archived=False
            if receipt.exists():
                if not receipt.is_file():invalid()
                recorded=_object(receipt.read_text(encoding='utf8'))
                if (set(recorded)!={'input_digest','recovery','archived'}
                        or recorded['input_digest']!=payload['input_digest']
                        or recorded['recovery']!=data or type(recorded['archived']) is not bool):invalid()
                archived=recorded['archived']
            allowed=set(files)|{'recovery.json'}
            if not archived:allowed.add('recovery.tmp')
            if not extra and extension is not None:allowed.add('storyboard-extension')
            if folder.exists() and (not folder.is_dir() or any(child.name not in allowed for child in folder.iterdir())):invalid()
            def historical(path,sha):
                file_ref(identity,'transcript_recovery/'+path.relative_to(task).as_posix(),path,sha)
                report['managedFileReferences'][-1]['evidenceGrade']='historical_recovery_bytes'
            if receipt.exists():historical(receipt,hashlib.sha256(receipt.read_bytes()).hexdigest())
            temporary=_plain_path(folder/'recovery.tmp')
            if temporary.exists():
                # A partial atomic-write temp is preserved as bytes, never parsed as authority.
                if archived or not temporary.is_file():invalid()
                historical(temporary,hashlib.sha256(temporary.read_bytes()).hexdigest())
            relocated=None
            if not archived:
                matches=[]
                for candidate,metadata in formal_archives():
                    valid=True
                    for name,signature in files.items():
                        saved=_plain_path(folder/name)
                        if saved.exists():
                            if not receipt.exists() or (candidate/name).exists() or not signature_matches(saved,signature):
                                valid=False;break
                        elif not signature_matches(candidate/name,signature):
                            valid=False;break
                    if valid:matches.append((candidate,metadata))
                if len(matches)>1:invalid()
                if matches:
                    relocated,metadata=matches[0]
                    recovery_history_locations.add(relocated)
                    relocated_locations.add(relocated)
                    for meta in metadata:historical(meta,hashlib.sha256(meta.read_bytes()).hexdigest())
            for name,signature in files.items():
                saved=_plain_path(folder/name)
                original=_plain_path(original_root/name)
                if archived:
                    path=saved
                elif relocated is not None:
                    path=saved if saved.exists() else _plain_path(relocated/name)
                else:
                    # A later, fully checked archive may own bytes from a cancelled
                    # request. Match each file, never infer ownership from task alone.
                    inherited=None
                    for newer in successors:
                        newer_files=recovery_sources[newer]
                        if name not in newer_files:continue
                        new_signature,new_path=newer_files[name]
                        if saved.exists():
                            # A saved old file must still be checked. A later snapshot
                            # must prove it was absent before replacement output appeared.
                            if new_signature is None:
                                inherited=newer;break
                        elif new_signature==signature:
                            inherited=newer;break
                    if not receipt.exists() and saved.exists():invalid()
                    if inherited is not None:
                        recovery_inherited.setdefault(identity,set()).add(inherited)
                        if not saved.exists():
                            sources[name]=recovery_sources[inherited][name]
                            continue  # Its actual owner already supplied checked references.
                        path=saved
                    else:
                        if saved.exists() and original.exists():invalid()
                        path=saved if saved.exists() else original
                sources[name]=(signature,path if signature is not None else None)
                if signature is None:
                    if path.exists():invalid()
                elif name!='storyboard':historical(path,signature)
                else:
                    if not path.is_dir():invalid()
                    actual={}
                    directories=[path]
                    def visit(directory):
                        for child in sorted(directory.iterdir()):
                            child=_plain_path(child)
                            relative=child.relative_to(path).as_posix()
                            if child.is_dir():
                                actual[relative]={'type':'directory'};directories.append(child);visit(child)
                            elif child.is_file():
                                sha=hashlib.sha256(child.read_bytes()).hexdigest()
                                actual[relative]={'type':'file','sha256':sha}
                                historical(child,sha)
                            else:invalid()
                    visit(path)
                    if actual!=signature['entries']:invalid()
                    report['managedDirectoryReferences'].extend(dict(
                        table='job_payloads',rowId=identity,
                        field='transcript_recovery/'+directory.relative_to(task).as_posix(),
                        path=str(directory),evidenceGrade='historical_recovery_bytes')
                        for directory in directories)
            return archived
        archived=inventory(archive,recovery)
        complete=archived
        if extension is not None:
            if 'storyboard_state' in recovery:invalid()
            complete=inventory(archive/'storyboard-extension',extension,extra=True) and complete
        if not archived and not successors and not relocated_locations:
            pending_transcript_tasks.add(task_id)
        recovery_complete[identity]=complete
        recovery_sources[identity]=sources
        recovery_records.append((identity,task_id,payload))
        return True

    def transcript(identity, task_id, model, completed, script_digest=None, source_digest=None):
        if Path(task_id).name!=task_id or task_id in {'.','..'}:invalid()
        task=_plain_path(Path(source_root)/'runtime/workbench/tasks'/task_id)
        locations=[task/'artifacts']
        for family in ('script-revisions','source-revisions'):
            parent=_plain_path(task/'history'/family)
            if parent.exists():
                for version in sorted(parent.iterdir()):
                    _plain_path(version)
                    if version.is_dir():locations.append(version/'artifacts')
        scripts=[task/'inputs/final-script.txt']
        scripts += [loc.parent/'previous-script.txt' for loc in locations[1:]]
        matched=False
        for loc in locations:
            if loc in recovery_history_locations or loc==task/'artifacts' and task_id in pending_transcript_tasks:
                continue  # Exact frozen recovery inputs were verified separately, not successful outputs.
            meta=_plain_path(loc/'transcription.json')
            receipt=_plain_path(loc/'.transcription-promotion.json')
            if receipt.exists():
                gap(identity,'TRANSCRIPTION_PROMOTION_UNRESOLVED');return False
            if not meta.exists():continue
            data=_object(meta.read_text(encoding='utf-8'))
            base={'task_id','script_sha256','source_path','source_size_bytes','source_mtime_ns','model'}
            allowed=base|{'input_digest','source_sha256','srt_sha256','words_sha256','word_count','reconciliation'}
            if set(data)-allowed or not base|{'input_digest'} <= set(data):
                gap(identity,'WORKFLOW_JOB_RESULT_UNSUPPORTED');return False
            if data['task_id']!=task_id or canonical({k:data[k] for k in base})!=data['input_digest']:invalid()
            if (not _digest(data['script_sha256']) or not isinstance(data['model'],str)
                    or type(data['source_size_bytes']) is not int or data['source_size_bytes']<0
                    or type(data['source_mtime_ns']) is not int or data['source_mtime_ns']<0):invalid()
            source=_address(data['source_path'])
            if not source.is_file():_fail('FROZEN_FILE_UNAVAILABLE','Original transcription source is unavailable.')
            stat=source.stat()
            if stat.st_size!=data['source_size_bytes']:invalid()
            if 'source_sha256' not in data and stat.st_mtime_ns!=data['source_mtime_ns']:invalid()
            script=None
            for candidate in scripts:
                _plain_path(candidate)
                if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest()==data['script_sha256']:
                    script=candidate;break
            if script is None:_fail('FROZEN_FILE_CHANGED','Original transcription script is unavailable or changed.')
            prefix='transcription/'+str(meta.relative_to(task)).replace(chr(92),'/')
            file_ref(identity,prefix,meta)
            file_ref(identity,prefix+'/script',script,data['script_sha256'])
            file_ref(identity,prefix+'/source_path',source,data.get('source_sha256'))
            srt=_plain_path(loc/'transcript.srt');words=_plain_path(loc/'transcript.words.json')
            file_ref(identity,prefix+'/srt',srt,data.get('srt_sha256'))
            file_ref(identity,prefix+'/words',words,data.get('words_sha256'))
            if not srt.read_text(encoding='utf-8').strip():invalid()
            try:content=json.loads(words.read_text(encoding='utf-8'))
            except (ValueError,OSError):invalid()
            if not isinstance(content,list) or not content:invalid()
            previous=-1
            for word in content:
                if not isinstance(word,dict) or not isinstance(word.get('w'),str):invalid()
                start,end=word.get('start'),word.get('end')
                if (type(start) not in (int,float) or type(end) not in (int,float)
                        or not math.isfinite(start) or not math.isfinite(end) or start<previous or end<start):invalid()
                previous=end
            if 'word_count' in data and data['word_count']!=len(content):invalid()
            if 'reconciliation' in data and not isinstance(data['reconciliation'],dict):invalid()
            matched=matched or (data['model']==model and (script_digest is None or script_digest==data['script_sha256'])
                               and (source_digest is None or hashlib.sha256(source.read_bytes()).hexdigest()==source_digest))
        if completed and not matched:
            gap(identity,'WORKFLOW_JOB_RESULT_UNAVAILABLE');return False
        return True
    for job in jobs:
        kind=job.get('job_type')
        if kind not in KINDS:continue
        identity=job['job_id']; task_id=job.get('task_id'); row=payloads.get(identity)
        summary=dict(jobId=identity,jobType=kind,taskId=task_id,status=job.get('status'),
                     frozenBusinessReuseVerified=False,outputBindingEvidence='task_owned_inventory')
        report['jobs'].append(summary)
        if row is None:gap(identity,'WORKFLOW_JOB_PAYLOAD_UNAVAILABLE');continue
        payload=_object(row.get('payload_json'))
        if canonical(payload)!=row.get('payload_digest'):invalid()
        allowed={'action','task_id'}|({'model','input_digest','script_sha256','recovery','storyboard_recovery'} if kind=='transcribe' else {'impact_token'} if kind=='sync_sources' else set())
        if (set(payload)-allowed or not {'action','task_id'}<=set(payload)
                or payload['action']!=kind or payload['task_id']!=task_id):
            gap(identity,'WORKFLOW_JOB_PAYLOAD_UNSUPPORTED');continue
        if task_id not in tasks or tasks[task_id].get('task_type') not in {'ad_mix','digital_human'}:invalid()
        if row.get('is_paid')!=0 or row.get('cost_status') not in {'waived','unknown'} or row.get('provider_request_id'):
            gap(identity,'WORKFLOW_JOB_OUTCOME_UNRESOLVED');continue
        if job.get('status') not in {'completed','failed','cancelled','interrupted'}:
            gap(identity,'JOB_NOT_TERMINAL');continue
        completed=job['status']=='completed'
        if completed and (job.get('cursor')!=kind+':complete' or job.get('progress_current')!=1 or job.get('progress_total')!=1):
            gap(identity,'WORKFLOW_JOB_RESULT_UNSUPPORTED');continue
        stage=stages.get((task_id,'material_sync'))
        stage_data=_object(stage['detail_json']) if stage and stage.get('detail_json') else {}
        if kind=='initial_source_check':
            matches=[r for r in checks if r.get('job_id')==identity]
            if len(matches)!=1:
                gap(identity,'WORKFLOW_JOB_RESULT_UNAVAILABLE');continue
            check=matches[0]
            if check['task_id']!=task_id:invalid()
            revision=check.get('source_revision')
            if type(revision) is not int or revision<0:invalid()
            if task_id in settings and revision>settings[task_id]['revision']:invalid()
            summary['sourceRevision']=revision
            if completed or check.get('summary_json'):
                if not check.get('summary_json') or not check.get('checked_at'):
                    gap(identity,'WORKFLOW_JOB_RESULT_UNAVAILABLE');continue
                result=_object(check['summary_json'])
                if (set(result)!={'reason','scope_count','changed','health_status','observation_digest'}
                        or not isinstance(result['reason'],str) or not result['reason']
                        or type(result['scope_count']) is not int or result['scope_count']<0
                        or type(result['changed']) is not bool or result['health_status'] not in {'healthy','warning','blocking'}
                        or not _digest(result['observation_digest']) or check['status'] not in {'ready','offline','review_required','failed'}):
                    gap(identity,'WORKFLOW_JOB_RESULT_UNSUPPORTED');continue
                details=stage_data.get('details',{})
                if details.get('initial_source_check_job_id')==identity:
                    if stage_data.get('input_digest')!=result['observation_digest'] or details!={'initial_source_check_job_id':identity,**result}:invalid()
                else:summary['stageEvidence']='shared_stage_superseded'
            elif check.get('status') not in {'failed','queued','running','cancelled','interrupted'}:
                gap(identity,'WORKFLOW_JOB_RESULT_UNSUPPORTED');continue
        elif kind=='sync_sources':
            if payload.get('impact_token') is not None and not isinstance(payload['impact_token'],str):
                gap(identity,'WORKFLOW_JOB_PAYLOAD_UNSUPPORTED');continue
            details=stage_data.get('details',{})
            if 'snapshot_ids' in details:
                if (set(details)!={'changed','configuration_changed','health_changed','snapshot_ids','sources'}
                        or any(type(details.get(k)) is not bool for k in ('changed','configuration_changed','health_changed'))
                        or not isinstance(details['snapshot_ids'],list) or not isinstance(details['sources'],dict)):
                    gap(identity,'WORKFLOW_JOB_RESULT_UNSUPPORTED');continue
                if stage_data.get('input_digest')!=canonical(details):invalid()
                for sid in details['snapshot_ids']:
                    if not isinstance(sid,str):invalid()
                    snapshot=snapshots.get(sid)
                    if snapshot is None or snapshot.get('task_id')!=task_id:invalid()
                    if snapshot.get('scope_id') not in scopes or scopes[snapshot['scope_id']].get('task_id')!=task_id:invalid()
            else:summary['stageEvidence']='shared_stage_superseded'
        else:
            model=payload.get('model') or 'small'
            if not isinstance(model,str) or not model:gap(identity,'WORKFLOW_JOB_PAYLOAD_UNSUPPORTED');continue
            if ('input_digest' in payload) != ('script_sha256' in payload) or any(not _digest(payload[k]) for k in ('input_digest','script_sha256') if k in payload):
                gap(identity,'WORKFLOW_JOB_PAYLOAD_UNSUPPORTED');continue
            if 'input_digest' in payload and 'task_gate_events' in tables:
                gates=rows('task_gate_events')
                if not any(g.get('task_id')==task_id and g.get('gate_type')=='brief' and g.get('gate_digest')==payload['input_digest'] for g in gates):invalid()
            transcription_checks.append((identity,task_id,payload,model,completed,summary))
            continue
        report['recognizedJobIds'].append(identity)
    # Validate newest archives first so cancelled predecessors can refer only to
    # already verified, exactly bound successor bytes.
    for identity,task_id,payload,model,completed,summary in reversed(transcription_checks):
        recovery_inventory(identity,task_id,payload)
        if identity in recovery_inherited:
            summary['recoveryArchiveInheritedFrom']=sorted(recovery_inherited[identity])
    # All recovery inputs must be verified before an earlier ordinary job scans the
    # same task. A damaged original can be preserved history, never success proof.
    for identity,task_id,payload,model,completed,summary in transcription_checks:
        superseded=False
        for newer,new_task,new_payload in recovery_records:
            if (new_task!=task_id or job_order[newer]<=job_order[identity]
                    or new_payload.get('model','small')!=model
                    or not all(payload.get(key)==new_payload.get(key) and _digest(payload.get(key))
                               for key in ('input_digest','script_sha256'))):continue
            prior=payload.get('recovery')
            if prior is not None and any(prior[key]!=new_payload['recovery'][key]
                                        for key in ('brief_event_id','source_sha256')):continue
            superseded=True;break
        if completed and identity in recovery_complete and not recovery_complete[identity]:
            gap(identity,'TRANSCRIPTION_RECOVERY_UNRESOLVED');continue
        if not transcript(identity,task_id,model,completed and not superseded,payload.get('script_sha256'),
                          (payload.get('recovery') or {}).get('source_sha256')):continue
        if superseded:
            summary['outputBindingEvidence']='superseded_by_transcription_recovery'
        report['recognizedJobIds'].append(identity)
    report['auditComplete']=not report['unresolved']
    return report
