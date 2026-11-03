"""Explicit, free 03->04 action with strong source I/O off the HTTP thread."""
import json
import uuid
from datetime import datetime, timezone
from .initial_source_check import get_initial_source_check, _observation_digest
from .workflow import complete_stage
from .script_segments import storyboard_digest
from .storyboard_review import accept_storyboard
from .task_sources import (_current_source_commit_guard, _stable_digest, source_readiness,
                           initial_source_check_pending, task_has_source_configuration)
from .workbench_worker import enqueue_job, get_job


class _Existing(Exception):
    def __init__(self, job_id):
        self.job_id = job_id


class _Stop(Exception):
    pass


def _binding(db, task_id):
    return _stable_digest(_current_source_commit_guard(db, task_id))


def _initial_guard(db, task_id):
    if initial_source_check_pending(db, task_id):
        raise RuntimeError('SOURCE_CHECK_PENDING: 创建后的素材检查尚未完成')
    initial = get_initial_source_check(db, task_id)
    if initial and initial['status'] == 'failed':
        raise RuntimeError('SOURCE_CHECK_FAILED: 请先在后台作业查看并恢复失败的创建初检')


def queue_source_continue(db, task_id, expected_digest, request_id):
    if not all(isinstance(v,str) and v.strip() and len(v)<=200 for v in (expected_digest,request_id)):
        raise ValueError('SOURCE_CONTINUE_INPUT: 缺少当前分镜版本或请求编号')
    _initial_guard(db, task_id)
    if storyboard_digest(db,task_id) != expected_digest:
        raise RuntimeError('STORYBOARD_CHANGED: 分镜已变化，请刷新核对')
    binding = _binding(db,task_id)
    payload = {'action':'source_continue','task_id':task_id,
               'expected_storyboard_digest':expected_digest,'source_binding':binding}
    job_id = 'source-continue-' + uuid.uuid5(uuid.NAMESPACE_URL,task_id+':'+request_id).hex
    def existing_request():
        row=db.connection.execute('SELECT j.task_id,j.job_type,p.payload_json FROM jobs j JOIN job_payloads p USING(job_id) WHERE j.job_id=?',(job_id,)).fetchone()
        if row is None:return None
        if row['task_id']!=task_id or row['job_type']!='source_continue' or json.loads(row['payload_json'])!=payload:
            raise RuntimeError('SOURCE_CONTINUE_CONFLICT: 请求编号已绑定其他内容')
        return job_id
    if existing_request():return job_id
    def check(connection):
        _initial_guard(db,task_id)
        if _binding(db,task_id)!=binding or storyboard_digest(db,task_id)!=expected_digest:
            raise RuntimeError('SOURCE_CONTINUE_STALE: 排队前分镜或素材范围已变化')
        active=connection.execute("SELECT j.job_id,p.payload_json FROM jobs j JOIN job_payloads p USING(job_id) LEFT JOIN job_controls c USING(job_id) WHERE j.task_id=? AND j.job_type='source_continue' AND (j.status IN ('queued','running') OR c.state='paused') ORDER BY j.created_at DESC",(task_id,)).fetchall()
        for row in active:
            if json.loads(row['payload_json'])==payload:raise _Existing(row['job_id'])
            raise RuntimeError('SOURCE_CONTINUE_BUSY: 已有另一版分镜正在核验，请先处理该作业')
    try:
        return enqueue_job(db,job_id=job_id,job_type='source_continue',task_id=task_id,
                           payload=payload,cost_status='waived',pre_enqueue_check=check)
    except _Existing as existing:
        return existing.job_id
    except ValueError:
        if existing_request():return job_id
        raise


def run_source_continue(db, job, project_root=None, runner=None):
    task_id=job.task_id
    def guard(connection=None):
        row=db.connection.execute("SELECT j.status,j.worker_id,j.lease_expires_at,j.cancel_requested,COALESCE(c.state,'active') AS control FROM jobs j LEFT JOIN job_controls c USING(job_id) WHERE j.job_id=?",(job.job_id,)).fetchone()
        if row is None or row['status']!='running' or row['worker_id']!=job.worker_id:
            raise RuntimeError('SOURCE_CONTINUE_LEASE: 当前核验作业已失去执行权')
        try:
            lease = datetime.fromisoformat((row['lease_expires_at'] or '').replace('Z','+00:00'))
            fresh = lease.tzinfo is not None and lease > datetime.now(timezone.utc)
        except (ValueError, TypeError):
            fresh = False
        if not fresh:
            raise RuntimeError('SOURCE_CONTINUE_LEASE: 当前核验租约已过期，请明确恢复作业后再确认')
        if row['cancel_requested'] or row['control']=='pause_requested':raise _Stop()
        _initial_guard(db,task_id)
        if _binding(db,task_id)!=job.payload['source_binding'] or storyboard_digest(db,task_id)!=job.payload['expected_storyboard_digest']:
            raise RuntimeError('SOURCE_CONTINUE_STALE: 排队后分镜或素材范围已变化，请重新查看后确认')
    def progress(current,total):
        if current not in (0,total) and current%64:return
        guard()
        with db.transaction() as connection:
            connection.execute("UPDATE jobs SET progress_current=?,progress_total=?,phase_label=?,updated_at=datetime('now') WHERE job_id=? AND status='running' AND worker_id=?",(current,total+1,f'正在核验当前素材范围 {current}/{total}；不会调用模型',job.job_id,job.worker_id))
    try:
        guard()
        readiness = None
        if task_has_source_configuration(db,task_id):
            readiness=source_readiness(db,task_id,on_progress=progress)
            if not readiness['ready']:
                raise RuntimeError('SOURCE_SYNC_REQUIRED: 素材范围或文件已变化，请先查看变化并明确同步；本次未确认分镜')
        guard()
        def accept_checked_sources(connection):
            guard(connection)
            if readiness is not None:
                # Same transaction as acceptance: no stage completion on a gap,
                # cancellation, stale input or failed picture validation.
                complete_stage(db, task_id, 'material_sync',
                    input_digest=_observation_digest(readiness),
                    details={'source_continue_job_id': job.job_id, 'strict_check': True})
        result=accept_storyboard(db,task_id,job.payload['expected_storyboard_digest'],pre_accept_check=accept_checked_sources)
    except _Stop:
        # The worker handles requested cancellation/pause at its existing safe point.
        return {'cursor':'source_continue:stopped','phase_label':'素材核验已停止，未确认分镜',
                'progress':{'current':0,'total':1},'result':{'accepted':False}}
    return {'cursor':'source_continue:complete','phase_label':'素材核验通过，已进入粗剪',
            'progress':{'current':1,'total':1},'result':result}