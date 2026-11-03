"""Existing durable TTS queue -> controlled sentence executor -> full registration."""
import json
import os
from pathlib import Path
import shutil
from .voice_bundle_service import (_run,_claim,_service,_generation_root,_load,_json,
    begin_voice_generation,register_voice_generation,_execution_segments)
from .workbench_worker import ProviderOutcomeUnknown


def run_voice_editing_job(db,job,project_root,runner):
    if job.payload.get('action')!='voice_editing_v1':raise ValueError('VOICE_JOB_INVALID')
    root=Path(project_root).resolve();run=_run(db,job.task_id,job.payload['request_id'])
    request=_service(db,root,job.task_id).request(run['request_id'])
    if (request.get('cost') or {}).get('localRecovery') is True:
        from .voice_unknown_recovery import run_local_recovery
        return run_local_recovery(db,root,job)
    # The selected voice/resource comes only from the already frozen server draft.
    config=json.loads(run['input_json'])['config']
    from .model_runtime import task_model_environment
    environment,binding=task_model_environment(db,job.task_id,'tts')
    environment.update(CUTROOM_TTS_PROVIDER=binding['provider'],CUTROOM_TTS_PROFILE_DIGEST=binding['configDigest'],
        TTS_VOICE_TYPE=config['voice']['voice'],CUTROOM_TTS_RESOURCE_ID=config['voice']['resource'])
    if environment['TTS_PROTOCOL']=='volcengine_v3':environment['TTS_RESOURCE_ID']=config['voice']['resource']
    elif environment['TTS_PROTOCOL']=='openai':environment['TTS_MODEL']=config['voice']['resource']
    for key in ('ARK_API_KEY','ARK_MODEL','ARK_BASE_URL','LLM_API_KEY','OPENAI_API_KEY','OPENAI_BASE_URL','GENERAL_TEXT_API_KEY','HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):
        environment.pop(key,None)
    private=begin_voice_generation(db,root,job,environment)
    service=_service(db,root,job.task_id);directory=_generation_root(service,run['request_id'])
    input_root=service.audio_root/'inputs';input_root.mkdir(parents=True,exist_ok=True)
    input_path=input_root/(directory.name+'.json')
    if input_path.exists():raise ProviderOutcomeUnknown('VOICE_INPUT_ALREADY_EXISTS')
    with input_path.open('x',encoding='utf-8') as output:output.write(_json(private))
    node=shutil.which('node')
    from .runtime_artifacts import node_script_command
    command=node_script_command(node or root/'node.exe',root,'scripts/voice-editing-tts.ts') + [
      '--input',str(input_path),'--output-root',str(directory)]
    from .workbench_actions import _run_checked
    try:
        _run_checked(runner,command,cwd=root,env=environment,timeout_seconds=min(3600,130*len(_execution_segments(private))+30),max_output_bytes=65536)
        value=register_voice_generation(db,root,job)
    except Exception as error:
        # Never include raw provider errors, credentials or text in queue errors.
        status='unknown';execution=None
        try:
            execution=_load(directory/'execution.json');result=_load(directory/'result.json')
            if result.get('status')=='failed' and execution.get('events') and all(e.get('phase')!='result' or e.get('status')!='unknown' for e in execution['events']):status='failed'
        except (ValueError,OSError):pass
        try:
            with db.transaction(immediate=True) as conn:
                _claim(conn,run,job)
                conn.execute("UPDATE voice_generation_runs SET status=?,execution_json=? WHERE request_id=? AND status='running'",(status,_json(execution) if execution is not None else None,run['request_id']))
                conn.execute("UPDATE voice_edit_requests SET status=?,error_code=?,cost_json=? WHERE request_id=? AND status='running'",(status,'VOICE_GENERATION_'+status.upper(),_json({'status':'unknown'}),run['request_id']))
        except (ValueError,RuntimeError):pass
        if status=='unknown':raise ProviderOutcomeUnknown('VOICE_GENERATION_OUTCOME_UNKNOWN',provider_state='voice_result_unknown') from None
        raise RuntimeError('VOICE_GENERATION_FAILED') from None
    return dict(cursor='voice_editing:ready',progress={'current':1,'total':1},phase_label='新版完整声音已就绪',result=value)


def require_registered_voice_job_completion(connection,job_id,worker_id):
    row=connection.execute('SELECT j.task_id,p.payload_json FROM jobs j JOIN job_payloads p ON p.job_id=j.job_id WHERE j.job_id=? AND j.job_type=\'tts\'',(job_id,)).fetchone()
    if row is None:return
    payload=json.loads(row['payload_json'])
    if payload.get('action')!='voice_editing_v1':return
    receipt=connection.execute("SELECT 1 FROM voice_generation_runs r JOIN voice_registered_bundles b ON b.bundle_id=r.bundle_id AND b.request_id=r.request_id JOIN voice_edit_requests q ON q.request_id=r.request_id WHERE r.job_id=? AND r.status='ready' AND q.status='ready' AND b.task_id=?",(job_id,row['task_id'])).fetchone()
    if receipt is None:raise ProviderOutcomeUnknown('VOICE_GENERATION_REGISTRATION_MISSING',provider_state='voice_result_unknown')
