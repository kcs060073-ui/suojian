"""Inventory frozen creative requests without dispatch, adoption or reapproval.

Recognized IDs authorize backup only. Unknown paid outcomes remain unknown in
the copied queue; neither this reader nor its report authorizes retry.
"""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import sqlite3

from .upgrade_recovery import RecoveryPlanError, _plain_path


TYPES = frozenset({'visual_analysis', 'creative_visual_advice', 'edit_intent', 'delivery_copy_suggestion'})
_FIELDS = {
    'visual_analysis': {'profile_id', 'library_id', 'selections', 'preview_digest', 'method_version', 'items'},
    'creative_visual_advice': {'task_id', 'context', 'profile_id', 'profile_digest', 'model', 'prompt_version', 'prompt_digest'},
    'edit_intent': {'task_id', 'inputKey', 'prepared', 'model', 'promptDigest'},
    'delivery_copy_suggestion': {'task_id', 'candidate_id', 'request_id', 'profile_id', 'profile_digest',
                                 'model_version', 'prompt_version', 'prompt_sha256'},
}
_SHA = re.compile(r'[0-9a-f]{64}')


class _Gap(Exception):
    pass


def _fail():
    raise RecoveryPlanError('CREATIVE_JOB_BINDING_INVALID',
                            'CREATIVE_JOB_BINDING_INVALID: 模型作业原输入、结果或文件绑定不一致。')


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                _fail()
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _fail())
    except (ValueError, TypeError, RecursionError):
        _fail()


def _object(raw):
    value = _json(raw)
    if not isinstance(value, dict):
        _fail()
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _one(db, table, field, value):
    rows = db.connection.execute(f'SELECT * FROM "{table}" WHERE "{field}"=?', (value,)).fetchall()
    if len(rows) > 1:
        _fail()
    return dict(rows[0]) if rows else None


def _edit(db, job, payload, summary):
    from .edit_intent import validate_intent_response, _prompt_digest
    if payload['promptDigest'] != _prompt_digest():
        raise _Gap('CREATIVE_JOB_PROMPT_VERSION_UNSUPPORTED')
    prepared = payload['prepared']
    fields = {'taskRevision', 'binding', 'options', 'request', 'anchors', 'mediaKinds', 'wire', 'limitations', 'parameters'}
    if (not isinstance(prepared, dict) or set(prepared) != fields
            or not isinstance(payload['model'], dict)
            or set(payload['model']) != {'profileId', 'configDigest', 'model'}
            or not all(_text(v) for v in payload['model'].values())
            or not _sha(payload['inputKey']) or not _sha(payload['promptDigest'])
            or not _sha(prepared['binding']) or not _sha(prepared['taskRevision'])
            or not isinstance(prepared['options'], list)):
        raise _Gap('CREATIVE_JOB_INPUT_VERSION_UNSUPPORTED')
    if payload['inputKey'] != _digest([job['task_id'], prepared['parameters'], prepared['binding']]):
        _fail()
    if job['job_id'] != 'edit-intent-' + payload['inputKey'][:40]:
        _fail()
    # Revalidate frozen server options, never recompute them from today's media.
    for option in prepared['options']:
        validate_intent_response(prepared, {'optionIds': [option['optionId']], 'clarification': ''})
    row = _one(db, 'edit_proposals', 'request_id', 'intent:' + job['job_id'])
    if row is None:
        if job['status'] == 'completed':
            raise _Gap('CREATIVE_JOB_RESULT_UNAVAILABLE')
        return
    if (row['task_id'] != job['task_id'] or row['proposal_id'] != 'intent-receipt-' + job['job_id']
            or row['request_digest'] != payload['inputKey'] or row['task_revision'] != prepared['taskRevision']
            or row['inputs_digest'] != prepared['binding']):
        _fail()
    result = _object(row['result_json'])
    if (result.get('jobId') != job['job_id'] or result.get('taskRevision') != prepared['taskRevision']
            or result.get('operations') != [] or result.get('limitations') != prepared['limitations']):
        _fail()
    phase = result.get('_phase')
    if phase not in {'saved', 'confirmed_failed', 'dispatched'}:
        raise _Gap('CREATIVE_JOB_RESULT_VERSION_UNSUPPORTED')
    if job['status'] == 'completed' and phase != 'saved':
        _fail()
    if phase == 'saved':
        if result.get('aiEvaluated') is not True or not isinstance(result.get('options'), list):
            _fail()
        answer = {'optionIds': [o['optionId'] for o in result['options']], 'clarification': result['clarification']}
        checked = validate_intent_response(prepared, answer)
        if any(result[key] != checked[key] for key in ('options', 'clarification', 'limitations')):
            _fail()
    summary.update(resultPersistence='edit_proposals', receiptId=row['proposal_id'], resultPhase=phase)


def _creative(db, job, payload, summary):
    from .creative_visual_advice import PROMPT, advice_schema
    if payload['prompt_digest'] != _digest([PROMPT, advice_schema()]):
        raise _Gap('CREATIVE_JOB_PROMPT_VERSION_UNSUPPORTED')
    context = payload['context']
    fields = {'taskRevision', 'inputsDigest', 'shots', 'totalCards', 'unobservedCardIds', 'contextVersion',
              'route', 'orientation', 'voice', 'packaging', 'quality', 'contentReview', 'audioLimitation', 'evidenceDigest'}
    if (not isinstance(context, dict) or set(context) != fields
            or context['contextVersion'] != 'creative-context-v1'
            or payload['prompt_version'] != 'creative-context-v4-evidence-guard'
            or not _sha(payload['prompt_digest']) or not _text(payload['profile_digest'])
            or not isinstance(context['shots'], list) or not 1 <= len(context['shots']) <= 4):
        raise _Gap('CREATIVE_JOB_INPUT_VERSION_UNSUPPORTED')
    expected = _digest({k: v for k, v in context.items() if k not in ('shots', 'taskRevision', 'inputsDigest', 'evidenceDigest')})
    if context['evidenceDigest'] != expected:
        _fail()
    if job['job_id'] != 'creative-advice-' + _digest([job['task_id'], context,
                                                     payload.get('retry_of_job_id') or payload['prompt_digest']])[:40]:
        _fail()
    if payload.get('retry_of_job_id'):
        parent = _one(db, 'jobs', 'job_id', payload['retry_of_job_id'])
        if parent is None or parent['task_id'] != job['task_id'] or parent['job_type'] != job['job_type']:
            _fail()
    shots = {shot['cardId']: shot for shot in context['shots']}
    if len(shots) != len(context['shots']):
        _fail()
    row = _one(db, 'creative_visual_advice_results', 'job_id', job['job_id'])
    if row is None:
        if job['status'] == 'completed':
            raise _Gap('CREATIVE_JOB_RESULT_UNAVAILABLE')
        return
    if row['state'] not in {'saved', 'dispatched'}:
        raise _Gap('CREATIVE_JOB_RESULT_VERSION_UNSUPPORTED')
    result = _object(row['result_json'] or '{}')
    if job['status'] == 'completed' and row['state'] != 'saved':
        _fail()
    if row['state'] == 'saved':
        for result_key, source_key in [('taskRevision', 'taskRevision'), ('inputsDigest', 'inputsDigest')]:
            if result[result_key] != context[source_key]:
                _fail()
        if result['modelVersion'] != payload['model'] or result['promptVersion'] != payload['prompt_version']:
            _fail()
        coverage = result['coverage']
        if (coverage['sampledCards'] != len(shots) or coverage['totalCards'] != context['totalCards']
                or coverage['unobservedCardIds'] != context['unobservedCardIds']):
            _fail()
        frames = {frame['ref']: frame for frame in coverage['frames']}
        if len(frames) != len(coverage['frames']) or not frames:
            _fail()
        for frame in frames.values():
            shot = shots.get(frame['cardId'])
            if (shot is None or frame['outputStartUs'] != shot['startUs']
                    or frame['outputEndUs'] != shot['endUs'] or 'jpeg_bytes' in frame):
                _fail()
        if not isinstance(result['items'], list) or len(result['items']) > 3:
            _fail()
        for item in result['items']:
            shot = shots.get(item['cardId'])
            if (shot is None or item['kind'] not in {'rhythm', 'reshoot'}
                    or any(item[key] != shot[key] for key in ('startUs', 'endUs', 'text'))
                    or not item['frameRefs'] or any(ref not in frames or frames[ref]['cardId'] != item['cardId'] for ref in item['frameRefs'])):
                _fail()
    summary.update(resultPersistence='creative_visual_advice_results', resultPhase=row['state'])


def _delivery(db, job, payload, summary):
    from .model_requests import _context_from_json
    from .model_contracts import validate_envelope, validate_result
    request = _one(db, 'model_contract_requests', 'request_id', payload['request_id'])
    if request is None:
        raise _Gap('CREATIVE_JOB_REQUEST_UNAVAILABLE')
    if (request['task_id'] != job['task_id'] or request['template_id'] != 'P11'
            or request['client_id'] != 'provider:' + payload['profile_id']
            or request['prompt_version'] != payload['prompt_version'] or request['prompt_sha256'] != payload['prompt_sha256']):
        _fail()
    req = _object(request['request_json'])
    _object(request['context_json'])  # Reject duplicate JSON keys before context decoding.
    digest = _digest([job['task_id'], request['client_id'], request['request_json'], request['context_json'],
                      request['prompt_version'], request['prompt_sha256']])
    if (request['registration_digest'] != digest or req['request_id'] != payload['request_id']
            or req['input_digest'] != request['input_digest'] or req['base_revision'] != request['base_revision']
            or req['schema_version'] != request['schema_version'] or req['template_id'] != request['template_id']):
        _fail()
    context = _context_from_json(request['context_json'])
    validate_envelope(req, context)
    # P11 freezes candidate identity and QA, not merely request/client ownership.
    # Use the referenced historical candidate and script record, never today's
    # current candidate, approval, draft or model configuration.
    candidate = _one(db, 'candidate_versions', 'candidate_id', payload['candidate_id'])
    if (candidate is None or candidate['task_id'] != job['task_id']
            or not candidate['qa_snapshot_id'] or not candidate['qa_report_id']):
        _fail()
    scripts = [record for record in req['records'] if record.get('ref') == 'S1']
    if len(scripts) != 1 or scripts[0].get('kind') != 'script' or not isinstance(scripts[0].get('text'), str):
        _fail()
    script = scripts[0]
    if not _sha(script.get('version')) or hashlib.sha256(script['text'].encode('utf-8')).hexdigest() != script['version']:
        _fail()
    frozen_digest = _digest({
        'taskId': job['task_id'], 'candidateId': payload['candidate_id'],
        'candidateInput': candidate['input_digest'],
        'candidateOutput': _object(candidate['output_signature_json']),
        'qaSnapshotId': candidate['qa_snapshot_id'], 'qaReportId': candidate['qa_report_id'],
        'scriptSha256': script['version'], 'copyRevision': req['base_revision'],
    })
    if frozen_digest != req['input_digest']:
        _fail()
    result = _one(db, 'model_result_drafts', 'request_id', payload['request_id'])
    if result is None:
        if job['status'] == 'completed':
            raise _Gap('CREATIVE_JOB_RESULT_UNAVAILABLE')
        return
    if result['state'] not in {'draft', 'stale', 'accepted', 'rejected'}:
        raise _Gap('CREATIVE_JOB_RESULT_VERSION_UNSUPPORTED')
    response = _object(result['response_json'])
    if result['response_digest'] != _digest(response):
        _fail()
    validate_result(req, response, context)
    summary.update(resultPersistence='model_result_drafts', receiptId=result['receipt_id'], resultPhase=result['state'])


def _visual(db, source_root, job, payload, summary, report, bind):
    fields = {'assetId', 'libraryId', 'relativePath', 'mediaKind', 'sourceStartUs', 'sourceEndUs',
              'frameCount', 'status', 'assetFingerprint'}
    items = payload['items']
    if (not _sha(payload['preview_digest']) or not isinstance(payload['method_version'], str)
            or re.fullmatch(r'visual-content-v2-[0-9a-f]{24}', payload['method_version']) is None
            or not isinstance(items, list) or not items or not isinstance(payload['selections'], list)):
        raise _Gap('CREATIVE_JOB_INPUT_VERSION_UNSUPPORTED')
    if payload['library_id'] != job['library_id'] or (job['task_id'] is None and not _text(job['library_id'])):
        _fail()
    for item in items:
        if (not isinstance(item, dict) or set(item) != fields or item['status'] != 'pending'
                or item['mediaKind'] not in {'image', 'video'} or not _text(item['assetFingerprint'])
                or type(item['sourceStartUs']) is not int or type(item['sourceEndUs']) is not int
                or not 0 <= item['sourceStartUs'] <= item['sourceEndUs']
                or type(item['frameCount']) is not int or item['frameCount'] < 1):
            raise _Gap('CREATIVE_JOB_INPUT_VERSION_UNSUPPORTED')
        relative = PurePosixPath(item['relativePath'])
        if relative.is_absolute() or '..' in relative.parts or '\\' in item['relativePath'] or ':' in item['relativePath']:
            _fail()
        if job['task_id'] is None and item['libraryId'] != job['library_id']:
            _fail()
    rows = [dict(r) for r in db.connection.execute('SELECT * FROM visual_batch_items WHERE job_id=? ORDER BY item_index', (job['job_id'],))]
    if job['status'] == 'completed' and (job['cursor'] != 'visual-analysis-complete'
            or job['progress_current'] != len(rows) or job['progress_total'] != len(items) or len(rows) != len(items)):
        raise _Gap('CREATIVE_JOB_RESULT_UNAVAILABLE')
    indexes = set()
    base = Path(db.path).resolve().parent if getattr(db, 'path', None) else Path(source_root)/'runtime/workbench'
    scope = hashlib.sha256(json.dumps([job['task_id'], job['job_id']], ensure_ascii=False).encode()).hexdigest()
    for row in rows:
        index = row['item_index']
        if type(index) is not int or not 0 <= index < len(items) or index in indexes:
            _fail()
        indexes.add(index)
        item = items[index]
        if any(row[column] != item[field] for column, field in (
                ('asset_id', 'assetId'), ('asset_fingerprint', 'assetFingerprint'),
                ('source_start_us', 'sourceStartUs'), ('source_end_us', 'sourceEndUs'))):
            _fail()
        if row['status'] not in {'succeeded', 'failed', 'outcome_unknown'} or row['provider_dispatched'] not in (0, 1):
            raise _Gap('CREATIVE_JOB_RESULT_VERSION_UNSUPPORTED')
        if row['status'] == 'succeeded':
            observed = _one(db, 'visual_observations', 'observation_id', row['observation_id'])
            if observed is None:
                raise _Gap('CREATIVE_JOB_OBSERVATION_UNAVAILABLE')
            if (row['provider_dispatched'] != 1 or not _sha(row['input_digest'])
                    or observed['asset_id'] != item['assetId'] or observed['asset_fingerprint'] != item['assetFingerprint']
                    or observed['method_version'] != payload['method_version'] or observed['origin'] != 'model'
                    or not item['sourceStartUs'] <= observed['source_start_us'] <= observed['source_end_us'] <= item['sourceEndUs']):
                _fail()
        elif row['observation_id'] is not None:
            _fail()
        folder = _plain_path(base/'private/visual-responses'/scope/str(index))
        receipt, response = folder/'receipt.json', folder/'response.bin'
        # Production writes the immutable receipt before parsing any success.
        # Absence of both files is missing evidence, not an older success shape.
        if row['status'] == 'succeeded' and not receipt.exists():
            raise _Gap('CREATIVE_JOB_RESPONSE_UNAVAILABLE')
        if receipt.exists():
            meta = _object(_plain_path(receipt).read_text(encoding='utf-8'))
            if (set(meta) != {'taskId', 'jobId', 'itemIndex', 'assetFingerprint', 'inputDigest', 'sampleTimesUs',
                             'responseSha256', 'responseBytes', 'oversized', 'version'}
                    or meta['version'] != 1 or not _sha(meta['responseSha256'])
                    or type(meta['responseBytes']) is not int or meta['responseBytes'] < 0
                    or type(meta['oversized']) is not bool
                    or meta['oversized'] != (meta['responseBytes'] > 2 * 1024 * 1024)
                    or not isinstance(meta['sampleTimesUs'], list)
                    or any(type(t) is not int or not item['sourceStartUs'] <= t <= item['sourceEndUs'] for t in meta['sampleTimesUs'])):
                raise _Gap('CREATIVE_JOB_RESPONSE_VERSION_UNSUPPORTED')
            if (meta['jobId'] != job['job_id'] or meta['taskId'] != job['task_id'] or meta['itemIndex'] != index
                    or meta['assetFingerprint'] != item['assetFingerprint'] or meta['inputDigest'] != row['input_digest']):
                _fail()
            if row['status'] == 'succeeded' and meta['oversized']:
                _fail()
            if meta['oversized'] is not True:
                data = _plain_path(response).read_bytes()
                signature = {'size_bytes': meta['responseBytes'], 'sha256': meta['responseSha256']}
                if len(data) != signature['size_bytes'] or hashlib.sha256(data).hexdigest() != signature['sha256']:
                    _fail()
                bind('visual_batch_items', job['job_id'] + ':' + str(index), 'response.bin', str(response), signature)
            elif response.exists():
                _fail()
            report['managedFileReferences'].append({'table': 'visual_batch_items', 'rowId': job['job_id'] + ':' + str(index),
                                                    'field': 'receipt.json', 'path': str(receipt),
                                                    'evidenceGrade': 'current_backup_observation'})
        elif response.exists():
            _fail()
    summary.update(resultPersistence='visual_batch_items', itemCount=len(items), retainedItemCount=len(rows))


def inspect_creative_job_references(db, source_root, tables, bind, pending) -> dict:
    report = {'phase': 'creative-job-inspection', 'recognizedJobIds': [], 'jobs': [],
              'unresolved': [], 'managedFileReferences': [], 'canReplay': False, 'auditComplete': False}
    def gap(identity, code):
        report['unresolved'].append({'table': 'job_payloads', 'rowId': identity, 'code': code})
        pending('job_payloads', identity, code)
    if 'jobs' not in tables:
        return report
    jobs = [dict(r) for r in db.connection.execute('SELECT * FROM jobs ORDER BY job_id')]
    for job in jobs:
        kind = job['job_type']
        if kind not in TYPES:
            continue
        identity = job['job_id']
        try:
            row = _one(db, 'job_payloads', 'job_id', identity)
            if row is None:
                raise _Gap('CREATIVE_JOB_PAYLOAD_UNAVAILABLE')
            payload = _object(row['payload_json'])
            allowed = _FIELDS[kind] | ({'retry_of_job_id'} if kind == 'creative_visual_advice' and 'retry_of_job_id' in payload else set())
            if set(payload) != allowed:
                raise _Gap('CREATIVE_JOB_PAYLOAD_VERSION_UNSUPPORTED')
            if not _sha(row['payload_digest']):
                raise _Gap('CREATIVE_JOB_PAYLOAD_SIGNATURE_UNAVAILABLE')
            if _digest(payload) != row['payload_digest']:
                _fail()
            if kind != 'visual_analysis' and payload['task_id'] != job['task_id']:
                _fail()
            if job['task_id'] is not None:
                task = _one(db, 'tasks', 'task_id', job['task_id'])
                if task is None:
                    _fail()
                if task['task_type'] not in {'ad_mix', 'digital_human'}:
                    raise _Gap('CREATIVE_JOB_ROUTE_UNSUPPORTED')
            elif kind != 'visual_analysis':
                _fail()
            if job['status'] not in {'completed', 'failed', 'cancelled', 'interrupted'} or job['worker_id'] is not None or job['lease_expires_at'] is not None:
                raise _Gap('CREATIVE_JOB_NOT_TERMINAL')
            if job['retry_policy'] != 'never' or row['is_paid'] != 1 or row['requested_cost_status'] != 'approved':
                raise _Gap('CREATIVE_JOB_EXECUTION_VERSION_UNSUPPORTED')
            if row['outcome_status'] not in {'not_started', 'in_progress', 'unknown', 'confirmed'} or row['cost_status'] not in {'approved', 'incurred', 'unknown', 'waived', 'estimated'}:
                raise _Gap('CREATIVE_JOB_OUTCOME_VERSION_UNSUPPORTED')
            expected_provider = (identity if kind in {'creative_visual_advice', 'edit_intent'} else
                                 f"p11:{job['task_id']}:{payload['request_id']}" if kind == 'delivery_copy_suggestion' else None)
            if (expected_provider is not None and row['provider_request_id'] != expected_provider) or not _text(row['provider_request_id']):
                _fail()
            if kind != 'visual_analysis' and job['status'] == 'completed' and (
                    job['cursor'] != kind + ':complete' or job['progress_current'] != 1 or job['progress_total'] != 1
                    or row['outcome_status'] != 'confirmed'):
                raise _Gap('CREATIVE_JOB_RESULT_VERSION_UNSUPPORTED')
            summary = {'jobId': identity, 'taskId': job['task_id'], 'jobType': kind, 'status': job['status'],
                       'outcomeStatus': row['outcome_status'], 'costStatus': row['cost_status'],
                       'providerState': row['provider_state'], 'payloadDigest': row['payload_digest'],
                       'frozenBusinessReuseVerified': False, 'canReplay': False,
                       'evidenceStatus': 'unresolved_provider_outcome' if row['outcome_status'] in {'unknown', 'in_progress'} else 'recorded_terminal_state',
                       'resultPersistence': 'no_success_result_required'}
            if kind == 'edit_intent':
                _edit(db, job, payload, summary)
            elif kind == 'creative_visual_advice':
                _creative(db, job, payload, summary)
            elif kind == 'delivery_copy_suggestion':
                _delivery(db, job, payload, summary)
            else:
                _visual(db, source_root, job, payload, summary, report, bind)
            report['jobs'].append(summary)
            report['recognizedJobIds'].append(identity)
        except _Gap as error:
            gap(identity, str(error))
        except (sqlite3.OperationalError, IndexError, KeyError, TypeError):
            gap(identity, 'CREATIVE_JOB_SCHEMA_UNSUPPORTED')
        except (OSError, ValueError) as error:
            if isinstance(error, RecoveryPlanError):
                raise
            _fail()
    report['auditComplete'] = not report['unresolved']
    return report
