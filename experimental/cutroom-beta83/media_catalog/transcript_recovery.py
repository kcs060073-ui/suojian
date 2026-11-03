"""Explicit local transcript recovery with durable, task-owned backups."""
import hashlib
import json
import os
from pathlib import Path

from .provenance import canonical_digest
from .task_script import ScriptDraftError, read_task_script, _transcription_outputs_current
from .tasks import get_task
from .workflow import project_gates, rollback_to_stage_connection
from .workbench_worker import enqueue_job, get_job

NAMES = ('transcript.srt', 'transcript.words.json', 'transcription.json',
         'transcript-review.json', '.transcription-promotion.json')


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _paths(root, task_id):
    tasks = (Path(root) / 'runtime/workbench/tasks').resolve()
    task_root = (tasks / task_id).resolve()
    artifacts = task_root / 'artifacts'
    history = task_root / 'history/transcript-recoveries'
    if (Path(task_id).name != task_id or task_root == tasks or not task_root.is_relative_to(tasks)
            or _linked(artifacts) or _linked(history.parent) or _linked(history)
            or not artifacts.resolve().is_relative_to(task_root)
            or not history.resolve().is_relative_to(task_root)):
        raise ScriptDraftError(409, 'TASK_INPUT_INVALID', '转写目录或历史目录不可用。')
    return artifacts, history


def _linked(path):
    # Windows junctions are directory redirects too; do not traverse them.
    return path.is_symlink() or (path.exists() and bool(
        getattr(path.lstat(), 'st_file_attributes', 0) & 0x400))


def _entry_signature(path, name):
    if _linked(path):
        raise ScriptDraftError(409, 'TASK_INPUT_INVALID', '转写或分镜路径不能包含链接。')
    if not path.exists():
        return None
    if name != 'storyboard':
        if not path.is_file():
            raise ScriptDraftError(409, 'TASK_INPUT_INVALID', '转写文件路径不可用。')
        return _sha(path)
    if not path.is_dir():
        raise ScriptDraftError(409, 'TASK_INPUT_INVALID', '分镜目录不可用。')
    entries = {}
    def visit(directory):
        for child in sorted(directory.iterdir()):
            if _linked(child):
                raise ScriptDraftError(409, 'TASK_INPUT_INVALID', '分镜备份不能包含链接。')
            relative = child.relative_to(path).as_posix()
            if child.is_dir():
                entries[relative] = {'type': 'directory'}
                visit(child)
            elif child.is_file():
                entries[relative] = {'type': 'file', 'sha256': _sha(child)}
            else:
                raise ScriptDraftError(409, 'TASK_INPUT_INVALID', '分镜备份包含不可用文件。')
    visit(path)
    return {'type': 'directory', 'entries': entries}


def _snapshot(artifacts, names=None):
    names = (*NAMES, 'storyboard') if names is None else names
    if set(names) not in (set(NAMES), set(NAMES) | {'storyboard'}, {'storyboard'}):
        raise ScriptDraftError(409, 'TASK_INPUT_INVALID', '恢复文件清单不可用。')
    return {name: _entry_signature(artifacts / name, name) for name in names}


def _storyboard_state(connection, task_id):
    result = {}
    for table in ('scene_cards', 'script_segments', 'storyboard_feedback',
                  'reshoot_requirements', 'task_reshoot_current'):
        active = " AND script_revision NOT LIKE 'archived:%'" if table in (
            'script_segments', 'reshoot_requirements') else ''
        result[table] = [dict(row) for row in connection.execute(
            f'SELECT * FROM {table} WHERE task_id=?{active} ORDER BY rowid', (task_id,))]
    return result


def _inputs(db, root, task_id, expected):
    task = get_task(db, task_id)
    brief = (task or {}).get('brief') or {}
    script = read_task_script(root, task or {})
    if (not task or task.get('task_type') != 'digital_human'
            or not project_gates(db, task_id)['brief_locked']
            or not script or not script.strip()
            or (brief.get('final_script') or {}).get('sha256') != expected
            or hashlib.sha256(script.encode('utf8')).hexdigest() != expected):
        raise ScriptDraftError(409, 'TRANSCRIPTION_INPUT_CHANGED', '请先确认当前文案，再恢复转写。')
    source = Path(str((brief.get('digital_human_source') or {}).get('path') or '')).resolve()
    if not source.is_file():
        raise ScriptDraftError(409, 'DIGITAL_HUMAN_SOURCE_MISSING', '原口播视频不可访问，请先恢复素材连接。')
    record = brief.get('digital_human_source') or {}
    if all(record.get(key) is not None for key in ('path','size_bytes','mtime_ns','quick_fingerprint')):
        from .digital_workflow import _default_file_snapshot, _source_snapshot_identity
        try:
            matches = _source_snapshot_identity(_default_file_snapshot(source)) == _source_snapshot_identity(record)
        except (OSError, ValueError, TypeError, KeyError):
            matches = False
        if not matches:
            raise ScriptDraftError(409, 'TRANSCRIPTION_INPUT_CHANGED', '口播视频已变化，请通过更换口播视频流程重新确认。')
    else:
        artifacts, _ = _paths(root, task_id)
        try:
            metadata = json.loads((artifacts / 'transcription.json').read_text('utf8'))
        except (OSError, ValueError):
            metadata = None
        prior_sha = metadata.get('source_sha256') if isinstance(metadata, dict) else None
        expected_source = record.get('sha256') or prior_sha
        if not expected_source:
            gate = db.connection.execute("SELECT event_id FROM task_gate_events WHERE task_id=? AND gate_type='brief' ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
            for row in db.connection.execute("SELECT p.payload_json FROM job_payloads p JOIN jobs j USING(job_id) WHERE j.task_id=? AND j.job_type='transcribe' ORDER BY j.rowid DESC", (task_id,)):
                payload = json.loads(row['payload_json'])
                recovery = payload.get('recovery') or {}
                if payload.get('input_digest') == canonical_digest(brief) and recovery.get('brief_event_id') == gate['event_id']:
                    expected_source = recovery.get('source_sha256')
                    break
        if not expected_source or expected_source != _sha(source):
            raise ScriptDraftError(409, 'TRANSCRIPTION_INPUT_CHANGED', '无法核验旧任务的原口播视频，请先通过更换口播视频流程重新确认。')
    return task, source


def request_recovery(db, root, task_id, expected):
    task, source = _inputs(db, root, task_id, expected)
    artifacts, _ = _paths(root, task_id)
    input_digest = canonical_digest(task['brief'])
    gate_id = db.connection.execute("SELECT event_id FROM task_gate_events WHERE task_id=? AND gate_type='brief' ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()['event_id']
    # A replay while processing reuses the same request; it never invalidates twice.
    rows = db.connection.execute("SELECT j.job_id,j.status,p.payload_json FROM jobs j JOIN job_payloads p ON p.job_id=j.job_id WHERE j.task_id=? AND j.job_type='transcribe' ORDER BY j.rowid DESC", (task_id,)).fetchall()
    for row in rows:
        payload = json.loads(row['payload_json'])
        if payload.get('input_digest') == input_digest and row['status'] in ('queued', 'running'):
            if not payload.get('recovery') or payload['recovery'].get('brief_event_id') == gate_id:
                return get_job(db, row['job_id'])
        if payload.get('input_digest') == input_digest and payload.get('recovery', {}).get('brief_event_id') == gate_id and row['status'] in ('failed', 'paused', 'interrupted'):
            return get_job(db, row['job_id'])  # Existing explicit retry retains its archive.
    try:
        if _transcription_outputs_current(Path(root), task):
            return None
    except ScriptDraftError as error:
        if error.code != 'TRANSCRIPTION_OUTPUTS_CHANGED':
            raise
    snapshot = _snapshot(artifacts)
    source_sha = _sha(source)
    # When readable, the former binding must agree with the current source.
    # Replacing a source video still belongs to the normal source-rework flow.
    try:
        prior = json.loads((artifacts / 'transcription.json').read_text('utf8'))
    except (OSError, ValueError):
        prior = None
    recorded_sha = (task['brief'].get('digital_human_source') or {}).get('sha256')
    prior_sha = prior.get('source_sha256') if isinstance(prior, dict) else None
    if any(value and value != source_sha for value in (recorded_sha, prior_sha)):
        raise ScriptDraftError(409, 'TRANSCRIPTION_INPUT_CHANGED', '口播视频内容已变化，请通过更换口播视频流程重新确认。')
    storyboard_state = _storyboard_state(db.connection, task_id)
    recovery = {'files': snapshot, 'source_sha256': source_sha, 'brief_event_id': gate_id,
                'storyboard_state': storyboard_state}
    previous_id = rows[0]['job_id'] if rows else None
    job_id = 'transcribe-repair-' + canonical_digest({'task':task_id, 'input':input_digest, 'recovery':recovery, 'previous':previous_id})
    def prepare(connection):
        current, current_source = _inputs(db, root, task_id, expected)
        latest_gate = connection.execute("SELECT event_id FROM task_gate_events WHERE task_id=? AND gate_type='brief' ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
        if (latest_gate['event_id'] != gate_id or canonical_digest(current['brief']) != input_digest or _sha(current_source) != source_sha
                or _snapshot(artifacts) != snapshot
                or _storyboard_state(connection, task_id) != storyboard_state):
            raise ScriptDraftError(409, 'TRANSCRIPTION_INPUT_CHANGED', '输入或转写文件已变化，请刷新后重试。')
        if connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running','pending') LIMIT 1", (task_id,)).fetchone():
            raise ScriptDraftError(409, 'TASK_BUSY', '任务正在处理，请等待处理结束后恢复转写。')
        # The job payload persists the exact old choices in this same transaction.
        # A crash before file archival therefore cannot lose manual decisions.
        connection.execute("DELETE FROM scene_cards WHERE task_id=?", (task_id,))
        from .script_segments import archive_script_segments
        archive_script_segments(connection, task_id, job_id)
        connection.execute("UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?", (task_id,))
        connection.execute("UPDATE tasks SET status='brief_locked',updated_at=datetime('now') WHERE task_id=?", (task_id,))
        rollback_to_stage_connection(connection, task_id, 'material_sync', reason='transcript_recovery')
    try:
        enqueue_job(db, job_id=job_id, job_type='transcribe', task_id=task_id,
            payload={'action':'transcribe','task_id':task_id,'script_sha256':expected,
                     'input_digest':input_digest,'recovery':recovery}, cost_status='waived', pre_enqueue_check=prepare)
    except ValueError:
        existing = get_job(db, job_id)
        if existing is None:
            raise
    return get_job(db, job_id)


def prepare_legacy_storyboard_recovery(db, root, job):
    """Extend an old recovery without changing its original five-file receipt."""
    if 'storyboard_state' in job.payload['recovery']:
        return job
    from dataclasses import replace
    from .workbench_worker import _canonical_payload
    artifacts, _ = _paths(root, job.task_id)
    with db.transaction(immediate=True) as connection:
        active = connection.execute(
            "SELECT j.status,j.worker_id,j.cancel_requested,p.payload_json,"
            "COALESCE(c.state,'active') AS control_state FROM jobs j "
            "JOIN job_payloads p USING(job_id) LEFT JOIN job_controls c USING(job_id) "
            "WHERE j.job_id=? AND j.task_id=? AND j.job_type='transcribe'",
            (job.job_id, job.task_id)).fetchone()
        attempt = connection.execute(
            "SELECT attempt_no,worker_id,status FROM job_attempts WHERE job_id=? "
            "ORDER BY attempt_no DESC LIMIT 1", (job.job_id,)).fetchone()
        if (active is None or active['status'] != 'running' or active['worker_id'] != job.worker_id
                or active['cancel_requested'] or active['control_state'] != 'active'
                or attempt is None or attempt['attempt_no'] != job.attempt_no
                or attempt['worker_id'] != job.worker_id or attempt['status'] != 'running'):
            raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 恢复尝试已失效，请刷新任务状态')
        payload = json.loads(active['payload_json'])
        if ({k:v for k,v in payload.items() if k != 'storyboard_recovery'} !=
                {k:v for k,v in job.payload.items() if k != 'storyboard_recovery'}):
            raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 恢复请求已变化')
        current, source = _inputs(db, root, job.task_id, payload['script_sha256'])
        gate = connection.execute(
            "SELECT event_id FROM task_gate_events WHERE task_id=? AND gate_type='brief' "
            "ORDER BY rowid DESC LIMIT 1", (job.task_id,)).fetchone()
        if (gate['event_id'] != payload['recovery']['brief_event_id']
                or canonical_digest(current['brief']) != payload['input_digest']
                or _sha(source) != payload['recovery']['source_sha256']):
            raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 恢复输入已变化')
        if 'storyboard_recovery' not in payload:
            files = _snapshot(artifacts, ('storyboard',))
            state = _storyboard_state(connection, job.task_id)
            if files['storyboard'] is None and not any(state.values()):
                return job
            payload['storyboard_recovery'] = {'files':files, 'storyboard_state':state}
            serialized, digest = _canonical_payload(payload)
            connection.execute('UPDATE job_payloads SET payload_json=?,payload_digest=? WHERE job_id=?',
                               (serialized, digest, job.job_id))
            connection.execute('DELETE FROM scene_cards WHERE task_id=?', (job.task_id,))
            from .script_segments import archive_script_segments
            archive_script_segments(connection, job.task_id, job.job_id)
            connection.execute('UPDATE picture_lock SET changed_since_lock=1 WHERE task_id=?', (job.task_id,))
            connection.execute("UPDATE tasks SET status='brief_locked',updated_at=datetime('now') WHERE task_id=?", (job.task_id,))
            rollback_to_stage_connection(connection, job.task_id, 'material_sync', reason='transcript_recovery')
    return replace(job, payload=payload)


def archive_for_recovery(root, job, source_path):
    """Resume a partially moved archive before producing new files; never overwrite."""
    recovery = job.payload['recovery']
    artifacts, history = _paths(root, job.task_id)
    if _sha(source_path) != recovery['source_sha256']:
        raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 恢复期间原口播视频发生变化')
    token = hashlib.sha256(job.job_id.encode('utf8')).hexdigest()[:24]
    archive = history / token
    if not archive.resolve().is_relative_to(history.resolve()):
        raise RuntimeError('TASK_INPUT_INVALID: 恢复历史路径不可用')
    _archive_snapshot(artifacts, archive, recovery, job.payload['input_digest'])
    extension = job.payload.get('storyboard_recovery')
    if extension is not None:
        _archive_snapshot(artifacts, archive / 'storyboard-extension', extension, job.payload['input_digest'])


def _archive_snapshot(artifacts, archive, recovery, input_digest):
    if set(recovery['files']) not in (set(NAMES), set(NAMES) | {'storyboard'}, {'storyboard'}):
        raise RuntimeError('TASK_INPUT_INVALID: 恢复文件清单不可用')
    if _linked(archive):
        raise RuntimeError('TASK_INPUT_INVALID: 恢复历史不能指向其他目录')
    archive.mkdir(parents=True, exist_ok=True)
    receipt_path = archive / 'recovery.json'
    if _linked(receipt_path) or _linked(receipt_path.with_suffix('.tmp')):
        raise RuntimeError('TASK_INPUT_INVALID: 恢复历史回执不能包含链接')
    receipt = {'input_digest':input_digest, 'recovery':recovery, 'archived':False}
    if receipt_path.exists():
        recorded = json.loads(receipt_path.read_text('utf8'))
        if (recorded.get('input_digest') != receipt['input_digest'] or recorded.get('recovery') != recovery):
            raise RuntimeError('TRANSCRIPTION_RECOVERY_CHANGED: 恢复历史不匹配')
        if recorded.get('archived') is True:
            for name, expected in recovery['files'].items():
                saved = archive / name
                if _entry_signature(saved, name) != expected:
                    raise RuntimeError('TRANSCRIPTION_RECOVERY_CHANGED: 转写备份缺失或发生变化')
            return
    else:
        if _snapshot(artifacts, recovery['files']) != recovery['files']:
            raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 待恢复转写文件已变化')
        _save_receipt(receipt_path, receipt)
    # Verify all source/backup pairs before moving any remaining file.
    pending = []
    for name in recovery['files']:
        original, saved = artifacts / name, archive / name
        expected = recovery['files'][name]
        if _linked(original) or _linked(saved):
            raise RuntimeError('TASK_INPUT_INVALID: 转写文件不能指向其他目录')
        if expected is None:
            if original.exists() or saved.exists():
                raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 恢复过程中出现新的转写文件')
        elif saved.exists():
            if original.exists() or _entry_signature(saved, name) != expected:
                raise RuntimeError('TRANSCRIPTION_RECOVERY_CHANGED: 转写备份发生变化')
        elif _entry_signature(original, name) != expected:
            raise RuntimeError('TRANSCRIPTION_INPUT_CHANGED: 待恢复转写文件已变化')
        else:
            pending.append((original, saved))
    for original, saved in pending:
        os.replace(original, saved)
    if _snapshot(archive, recovery['files']) != recovery['files']:
        raise RuntimeError('TRANSCRIPTION_RECOVERY_CHANGED: 恢复历史在归档时发生变化')
    receipt['archived'] = True
    _save_receipt(receipt_path, receipt)


def _save_receipt(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding='utf8')
    os.replace(temporary, path)
