"""Read-only backup inventory for the free, explicit storyboard continuation.

The worker persists cursor/progress and shared stage state, not its result dict.
Historical source_binding is only a digest: recognizing a backup record must not
claim that its old media was verified, or authorize replay/business reuse.
"""
import hashlib
import json
import re

from .upgrade_recovery import RecoveryPlanError


_SHA = re.compile(r'[0-9a-f]{64}')
_FIELDS = {'action', 'task_id', 'expected_storyboard_digest', 'source_binding'}
_COLUMNS = {
    'jobs': {'job_id', 'job_type', 'task_id', 'library_id', 'status', 'cursor',
             'progress_current', 'progress_total', 'worker_id', 'lease_expires_at'},
    'job_payloads': {'job_id', 'payload_json', 'payload_digest', 'is_paid', 'cost_status',
                     'requested_cost_status', 'provider_request_id', 'outcome_status',
                     'provider_state', 'estimated_cost', 'actual_cost'},
    'tasks': {'task_id', 'task_type'},
    'task_stage_states': {'task_id', 'stage_key', 'status', 'detail_json'},
}


def _fail():
    raise RecoveryPlanError('SOURCE_CONTINUE_BINDING_INVALID',
                            'SOURCE_CONTINUE_BINDING_INVALID: 素材继续作业原记录或绑定不一致。')


def _object(text):
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                _fail()
            value[key] = item
        return value
    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=lambda _: _fail())
    except (ValueError, TypeError, RecursionError):
        _fail()
    if not isinstance(value, dict):
        _fail()
    return value


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def inspect_source_continue_jobs(db, source_root, tables, bind, pending) -> dict:
    """Use the shared callback interface; this job owns no file references.

    ``bind`` intentionally receives no invented signature: files and current
    source/catalog references belong to their existing inventory adapters.
    """
    report = {'phase': 'source-continue-job-inspection', 'recognizedJobIds': [],
              'jobs': [], 'unresolved': [], 'managedFileReferences': [],
              'canReplay': False, 'auditComplete': False}

    def gap(identity, code):
        report['unresolved'].append({'table': 'job_payloads', 'rowId': identity, 'code': code})
        pending('job_payloads', identity, code)

    columns = {name: {row[1] for row in db.connection.execute(f'PRAGMA table_info("{name}")')}
               for name in _COLUMNS if name in tables}
    if 'jobs' not in tables:
        if {'job_id', 'payload_json'} <= columns.get('job_payloads', set()):
            for row in db.connection.execute('SELECT job_id,payload_json FROM job_payloads ORDER BY job_id'):
                try:
                    payload = _object(row['payload_json'])
                except RecoveryPlanError:
                    continue
                if payload.get('action') == 'source_continue':
                    gap(row['job_id'], 'SOURCE_CONTINUE_JOB_UNAVAILABLE')
        return report
    if not {'job_id', 'job_type'} <= columns['jobs']:
        gap('jobs', 'SOURCE_CONTINUE_SCHEMA_UNSUPPORTED')
        return report
    jobs = {row['job_id']: dict(row) for row in db.connection.execute('SELECT * FROM jobs ORDER BY job_id')}
    candidates = {key for key, job in jobs.items() if job['job_type'] == 'source_continue'}
    if 'job_payloads' not in tables or not _COLUMNS['job_payloads'] <= columns['job_payloads']:
        for identity in sorted(candidates):
            gap(identity, 'SOURCE_CONTINUE_PAYLOAD_UNAVAILABLE')
        return report
    payloads = {row['job_id']: dict(row) for row in db.connection.execute('SELECT * FROM job_payloads')}
    # Also reject a source_continue action attached to an unrelated/missing job.
    for identity, row in payloads.items():
        if identity in candidates:
            continue
        try:
            payload = _object(row['payload_json'])
        except RecoveryPlanError:
            continue  # The owning adapter diagnoses unrelated malformed records.
        if payload.get('action') == 'source_continue':
            _fail()

    for identity in sorted(candidates):
        job = jobs[identity]
        row = payloads.get(identity)
        if row is None:
            gap(identity, 'SOURCE_CONTINUE_PAYLOAD_UNAVAILABLE'); continue
        if any(name not in tables or not required <= columns.get(name, set())
               for name, required in _COLUMNS.items()):
            gap(identity, 'SOURCE_CONTINUE_SCHEMA_UNSUPPORTED'); continue
        payload = _object(row['payload_json'])
        summary = {'jobId': identity, 'taskId': job['task_id'], 'status': job['status'],
                   'adapter': 'source-continue/v1', 'payloadDigest': row['payload_digest'],
                   'sourceBindingEvidence': 'digest_only', 'frozenBusinessReuseVerified': False,
                   'canReplay': False}
        report['jobs'].append(summary)
        if (set(payload) != _FIELDS or payload.get('action') != 'source_continue'
                or not isinstance(payload.get('task_id'), str) or not payload['task_id']
                or not _sha(payload.get('expected_storyboard_digest'))
                or not _sha(payload.get('source_binding'))):
            gap(identity, 'SOURCE_CONTINUE_PAYLOAD_VERSION_UNSUPPORTED'); continue
        if not _sha(row['payload_digest']):
            gap(identity, 'SOURCE_CONTINUE_PAYLOAD_SIGNATURE_UNAVAILABLE'); continue
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        task = db.connection.execute('SELECT task_type FROM tasks WHERE task_id=?', (job['task_id'],)).fetchone()
        if (digest != row['payload_digest'] or payload['task_id'] != job['task_id']
                or job['library_id'] is not None or task is None):
            _fail()
        if task['task_type'] not in {'ad_mix', 'digital_human'}:
            gap(identity, 'SOURCE_CONTINUE_ROUTE_UNSUPPORTED'); continue
        if (row['is_paid'] != 0 or row['cost_status'] != 'waived'
                or row['requested_cost_status'] != 'waived' or row['provider_request_id'] is not None
                or row['outcome_status'] != 'not_started' or row['provider_state'] is not None
                or row['estimated_cost'] is not None or row['actual_cost'] is not None):
            gap(identity, 'SOURCE_CONTINUE_COST_UNRESOLVED'); continue
        if job['status'] not in {'completed', 'failed', 'cancelled', 'interrupted'}:
            gap(identity, 'SOURCE_CONTINUE_NOT_TERMINAL'); continue
        if job['worker_id'] is not None or job['lease_expires_at'] is not None:
            gap(identity, 'SOURCE_CONTINUE_NOT_TERMINAL'); continue
        if 'job_controls' in tables:
            control_columns = {r[1] for r in db.connection.execute('PRAGMA table_info(job_controls)')}
            if not {'job_id', 'state'} <= control_columns:
                gap(identity, 'SOURCE_CONTINUE_SCHEMA_UNSUPPORTED'); continue
            control = db.connection.execute('SELECT state FROM job_controls WHERE job_id=?', (identity,)).fetchone()
            if control is not None and control['state'] != 'active':
                gap(identity, 'SOURCE_CONTINUE_NOT_TERMINAL'); continue
        current, total = job['progress_current'], job['progress_total']
        if (type(current) is not int or type(total) is not int or not 0 <= current <= total
                or job['cursor'] not in {None, '', 'source_continue:stopped', 'source_continue:complete'}
                or (job['status'] == 'completed' and
                    (job['cursor'] != 'source_continue:complete' or current != 1 or total != 1))):
            gap(identity, 'SOURCE_CONTINUE_RESULT_VERSION_UNSUPPORTED'); continue
        summary.update(storyboardDigest=payload['expected_storyboard_digest'],
                       sourceBinding=payload['source_binding'], costEvidence='local_waived',
                       resultPersistence='terminal_without_success_receipt')
        if job['status'] == 'completed':
            stages = {r['stage_key']: dict(r) for r in db.connection.execute(
                "SELECT stage_key,status,detail_json FROM task_stage_states WHERE task_id=? "
                "AND stage_key IN ('ai_storyboard','material_sync')", (job['task_id'],))}
            if set(stages) != {'ai_storyboard', 'material_sync'}:
                gap(identity, 'SOURCE_CONTINUE_STAGE_UNAVAILABLE'); continue
            material = _object(stages['material_sync']['detail_json'])
            storyboard = _object(stages['ai_storyboard']['detail_json'])
            md, sd = material.get('details'), storyboard.get('details')
            if not isinstance(md, dict) or not isinstance(sd, dict):
                gap(identity, 'SOURCE_CONTINUE_STAGE_VERSION_UNSUPPORTED'); continue
            same_material = md.get('source_continue_job_id') == identity
            same_storyboard = sd.get('storyboard_accepted_digest') == payload['expected_storyboard_digest']
            if same_material and (md.get('strict_check') is not True or not _sha(material.get('input_digest'))):
                _fail()
            if same_storyboard and storyboard.get('input_digest') != payload['expected_storyboard_digest']:
                _fail()
            summary['resultPersistence'] = ('shared_stage_current' if same_storyboard
                                            else 'shared_stage_superseded')
            summary['materialStageStillReferencesJob'] = same_material
            summary['storyboardStageStillReferencesDigest'] = same_storyboard
        report['recognizedJobIds'].append(identity)
    report['auditComplete'] = not report['unresolved']
    return report
