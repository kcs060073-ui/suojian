"""Task-owned recovery: reuse verified local results or explicitly release an unknown attempt.
No provider query, bill settlement or automatic new paid generation occurs here.
"""
import json
from .voice_bundle_service import (_service,_run,_current,_generation_root,_verify_execution,_load,_error,_json,_safe,get_voice_preview)
from .voice_draft_service import _owned_draft

RELEASED='VOICE_REQUEST_RELEASED'
TERMINAL={'interrupted','failed','cancelled'}

def local_result(db,root,task_id,run):
    service=_service(db,root,task_id)
    directory=_generation_root(service,run['request_id'])
    try:
        result=_load(_safe(directory/'result.json',directory));execution=_load(_safe(directory/'execution.json',directory))
        _verify_execution(json.loads(run['input_json']),json.loads(run['parameters_json']),result,execution,directory)
        return True
    except (ValueError,OSError,TypeError,KeyError,AttributeError,OverflowError):return False

def recovery_action(db,root,task_id,run,request):
    if request.get('error_code')==RELEASED:return None
    job=db.connection.execute('SELECT status FROM jobs WHERE job_id=?',(run['job_id'],)).fetchone()
    if not job or job['status'] not in TERMINAL:return None
    if request['status'] not in ('unknown','running'):return None
    # Obsolete requests may only release their wait; never restore old audio into a new version.
    from .voice_task_context import resolve_voice_task_context
    service=_service(db,root,task_id);draft=_owned_draft(db,task_id,run['draft_id'],service)
    head=service.current(task_id);context=resolve_voice_task_context(db,root,task_id)
    if (draft['status'] not in ('draft','cancelled') or draft['base_revision']!=head['revision']
        or draft['context_digest']!=head['context_digest'] or draft['context_digest']!=context.context_digest):return 'release'
    return 'restore' if local_result(db,root,task_id,run) else 'release'

def recover_voice_preview(db,root,task_id,draft_id,body):
    if (not isinstance(body,dict) or set(body)-{'requestId','action','acknowledgeUnknownCost'}
        or body.get('action') not in ('release','restore') or not isinstance(body.get('requestId'),str)):
        _error('VOICE_REQUEST_INVALID',status=400)
    service=_service(db,root,task_id);draft=_owned_draft(db,task_id,draft_id,service)
    request=service.request(body['requestId'])
    if request['draft_id']!=draft_id:_error('VOICE_PREVIEW_NOT_FOUND',status=404)
    if body['action']=='release' and body.get('acknowledgeUnknownCost') is not True:_error('VOICE_UNKNOWN_ACK_REQUIRED')
    def released():return dict(taskId=task_id,draftId=draft_id,requestId=body['requestId'],released=True,costStatus='unknown')
    with db.transaction(immediate=True) as conn:
        request=service.request(body['requestId']);run=_run(db,task_id,body['requestId'])
        if request.get('error_code')==RELEASED:
            if body['action']!='release':_error('VOICE_RECOVERY_CHANGED')
            return released()
        from .voice_task_context import resolve_voice_task_context
        head=service.current(task_id);context=resolve_voice_task_context(db,root,task_id)
        if draft['status']=='applied':_error('VOICE_RECOVERY_CHANGED')
        if body['action']=='restore' and (head['revision']!=draft['base_revision'] or head['context_digest']!=draft['context_digest']
            or context.context_digest!=draft['context_digest']):_error('VOICE_RECOVERY_CHANGED')
        latest=conn.execute('SELECT request_id FROM voice_edit_requests WHERE draft_id=? ORDER BY rowid DESC LIMIT 1',(draft_id,)).fetchone()
        if not latest or latest[0]!=body['requestId']:_error('VOICE_RECOVERY_CHANGED')
        cost=request.get('cost') or {'status':'unknown'}
        if body['action']=='restore' and cost.get('localRecovery') is True and run['status'] in ('queued','running','ready'):
            return get_voice_preview(db,root,task_id,draft_id)
        action=recovery_action(db,root,task_id,run,request)
        if action!=body['action']:_error('VOICE_RECOVERY_CHANGED')
        if action=='release':
            cost={**cost,'status':'unknown','recovery':{'kind':'released_by_user','providerOutcome':'unknown','acknowledgeUnknownCost':True}}
            conn.execute("UPDATE voice_edit_requests SET status='unknown',error_code=?,cost_json=? WHERE request_id=?",(RELEASED,_json(cost),body['requestId']))
            conn.execute("UPDATE voice_generation_runs SET status='unknown' WHERE request_id=?",(body['requestId'],))
            conn.execute("UPDATE voice_edit_drafts SET status='cancelled' WHERE draft_id=?",(draft_id,))
            return released()
        conn.execute("UPDATE voice_edit_drafts SET status='draft' WHERE draft_id=?",(draft_id,))
        # Requeue the same job solely for local registration, retaining its attempt
        # history and original provider identity. The worker checks this durable flag
        # before loading credentials or invoking TTS.
        conn.execute("UPDATE voice_edit_requests SET status='prepared',cost_json=? WHERE request_id=?",(_json({**cost,'localRecovery':True}),body['requestId']))
        conn.execute("UPDATE voice_generation_runs SET status='queued' WHERE request_id=?",(body['requestId'],))
        conn.execute("UPDATE job_attempts SET status='interrupted',finished_at=datetime('now') WHERE job_id=? AND status='running'",(run['job_id'],))
        conn.execute("UPDATE jobs SET status='queued',worker_id=NULL,cancel_requested=0,cancel_reason=NULL,lease_expires_at=NULL,finished_at=NULL,error_summary=NULL,updated_at=datetime('now') WHERE job_id=?",(run['job_id'],))
        conn.execute('DELETE FROM job_queue_entries WHERE job_id=?',(run['job_id'],))
        conn.execute('INSERT INTO job_queue_entries(job_id) VALUES(?)',(run['job_id'],))
        conn.execute("INSERT INTO job_controls(job_id,state) VALUES(?,'active') ON CONFLICT(job_id) DO UPDATE SET state='active',requested_at=NULL,paused_at=NULL",(run['job_id'],))
    return get_voice_preview(db,root,task_id,draft_id)

def run_local_recovery(db,root,job):
    from .voice_bundle_service import _claim,register_voice_generation
    from .workbench_worker import ProviderOutcomeUnknown
    run=_run(db,job.task_id,job.payload['request_id'])
    try:
        with db.transaction(immediate=True) as conn:
            _claim(conn,run,job);_current(db,root,job.task_id,run['draft_id'])
            if run['status']!='queued' or not local_result(db,root,job.task_id,run):_error('VOICE_RECOVERY_CHANGED')
            conn.execute("UPDATE voice_generation_runs SET status='running' WHERE request_id=?",(run['request_id'],))
            conn.execute("UPDATE voice_edit_requests SET status='running' WHERE request_id=?",(run['request_id'],))
        value=register_voice_generation(db,root,job)
        return dict(cursor='voice_editing:ready',progress={'current':1,'total':1},phase_label='已找回本机配音，无新增调用',result=value)
    except Exception:
        with db.transaction(immediate=True) as conn:
            _claim(conn,run,job)
            conn.execute("UPDATE voice_generation_runs SET status='unknown' WHERE request_id=? AND status!='ready'",(run['request_id'],))
            conn.execute("UPDATE voice_edit_requests SET status='unknown' WHERE request_id=? AND status!='ready'",(run['request_id'],))
        raise ProviderOutcomeUnknown('VOICE_LOCAL_RECOVERY_FAILED') from None
