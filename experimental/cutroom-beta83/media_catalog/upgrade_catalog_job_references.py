"""Read-only backup validation for known local catalog job producers.

These records prove a historical request, never a reusable business approval or
permission to replay it. Files and disposable preview caches are independently
inventoried by upgrade_catalog_references; this module does not re-sign them.
"""
import hashlib
import json
from pathlib import PureWindowsPath
import re

from .upgrade_packaging_references import _object
from .upgrade_recovery import RecoveryPlanError

_CLI_KINDS = {'scan', 'probe', 'preview', 'audio', 'segments', 'privacy'}
_KINDS = _CLI_KINDS | {'generate_preview', 'scene_detection', 'packaging_catalog_check'}
_TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and '\0' not in value


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _relative(value):
    return (_text(value) and not PureWindowsPath(value).is_absolute()
            and not PureWindowsPath(value).drive and ':' not in value
            and '..' not in PureWindowsPath(value).parts and not value.startswith(('/', '\\')))


def inspect_catalog_job_references(db, source_root, tables, bind, pending):
    """Validate persisted requests without querying a provider or mutating data."""
    report = dict(jobs=[], recognizedJobIds=[], unresolved=[], managedFileReferences=[],
                  auditComplete=False, canReplay=False)
    if not {'jobs', 'job_payloads'} <= set(tables):
        return report
    def rows(table):
        return [dict(row) for row in db.connection.execute('SELECT * FROM '+table)] if table in tables else []
    payloads = {row['job_id']: row for row in rows('job_payloads')}
    tasks = {row['task_id'] for row in rows('tasks')}
    libraries = {row['library_id'] for row in rows('libraries')}
    assets = {row['asset_id'] for row in rows('assets')}
    checks = {row['check_id']: row for row in rows('packaging_catalog_checks')}
    scene_rows = rows('scene_detection_items')
    for job in rows('jobs'):
        kind, identity = job['job_type'], job['job_id']
        if kind not in _KINDS:
            continue
        try:
            record = payloads.get(identity)
            _require(record is not None, 'CATALOG_JOB_PAYLOAD_MISSING')
            payload = _object(record.get('payload_json'))
            canonical = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
            _require(hashlib.sha256(canonical.encode()).hexdigest() == record.get('payload_digest'), 'CATALOG_JOB_DIGEST_MISMATCH')
            _require(record.get('is_paid') == 0 and not record.get('provider_request_id'), 'CATALOG_JOB_PAYMENT_UNEXPECTED')
            _require(job.get('status') in _TERMINAL, 'CATALOG_JOB_NOT_TERMINAL')
            task, library = job.get('task_id'), job.get('library_id')
            _require(task is None or task in tasks, 'CATALOG_JOB_TASK_MISSING')
            _require(library is None or library in libraries, 'CATALOG_JOB_LIBRARY_MISSING')
            completed = job['status'] == 'completed'
            cursor = job.get('cursor')
            if kind in _CLI_KINDS:
                _require(task is None and library in libraries and payload.get('library_id') == library, 'CATALOG_JOB_LIBRARY_BINDING')
                if payload.get('entrypoint') == 'media_catalog_cli':
                    _require(set(payload) == {'entrypoint','library_id','operation_key'}, 'CATALOG_JOB_PAYLOAD_FIELDS')
                    operation = payload['operation_key']
                    _require(operation == kind+':'+library and (identity == operation or re.fullmatch(re.escape(operation)+':[0-9a-f]{32}',identity)), 'CATALOG_JOB_CLI_BINDING')
                    if completed and cursor is not None:
                        if kind == 'scan':
                            prefix, separator, relative = cursor.partition(':') if isinstance(cursor,str) else ('','','')
                            valid = separator and prefix in {'scan','probe'} and (relative == '' or _relative(relative))
                        elif kind == 'privacy':
                            valid = isinstance(cursor,str) and cursor.startswith('privacy:') and cursor.removeprefix('privacy:') in assets
                        else:
                            valid = isinstance(cursor,str) and re.fullmatch(re.escape(kind)+r':\d+:\d+',cursor)
                        _require(valid, 'CATALOG_JOB_CURSOR_INVALID')
                else:
                    _require(kind == 'scan' and set(payload) == {'action','library_id','catalog_only'} and payload['action'] == 'scan' and payload['catalog_only'] is True, 'CATALOG_JOB_PAYLOAD_FIELDS')
                    if completed:
                        _require(cursor == 'scan:complete', 'CATALOG_JOB_CURSOR_INVALID')
            elif kind == 'generate_preview':
                _require(set(payload) == {'action','task_id','asset_id'} and payload['action'] == kind and payload['task_id'] == task and task in tasks and library is None and payload['asset_id'] in assets, 'CATALOG_PREVIEW_BINDING_INVALID')
                if completed:
                    _require(cursor == 'generate_preview:complete', 'CATALOG_JOB_CURSOR_INVALID')
                # Cache can have been legitimately cleaned/replaced since this job.
                # Current preview_cache inventory separately proves backup coverage.
            elif kind == 'scene_detection':
                _require(set(payload) == {'items','selections','library_id'} and payload['library_id'] == library and ((task is None) != (library is None)), 'CATALOG_SCENE_SCOPE_INVALID')
                items, selections = payload['items'],payload['selections']
                _require(type(items) is list and 1 <= len(items) <= 500 and type(selections) is list and len(selections)==len(items), 'CATALOG_SCENE_ITEMS_INVALID')
                seen=set()
                for item, selection in zip(items,selections):
                    _require(type(item) is dict and set(item)=={'assetId','libraryId','relativePath','assetFingerprint'} and type(selection) is dict and set(selection)=={'assetId','libraryId','relativePath'}, 'CATALOG_SCENE_ITEMS_INVALID')
                    _require(all(item[k]==v for k,v in selection.items()) and item['assetId'] in assets and item['libraryId'] in libraries and (library is None or item['libraryId']==library) and _relative(item['relativePath']) and _sha(item['assetFingerprint']), 'CATALOG_SCENE_ITEM_BINDING')
                    key=(item['assetId'],item['libraryId'],item['relativePath'])
                    _require(key not in seen,'CATALOG_SCENE_DUPLICATE_ITEM');seen.add(key)
                recorded=[row for row in scene_rows if row['job_id']==identity]
                indexes=set()
                for row in recorded:
                    index=row.get('item_index')
                    _require(type(index) is int and 0 <= index < len(items) and index not in indexes,'CATALOG_SCENE_RESULT_INDEX')
                    indexes.add(index);item=items[index]
                    _require(row.get('asset_id')==item['assetId'] and row.get('asset_fingerprint')==item['assetFingerprint'] and row.get('status') in {'succeeded','failed'} and type(row.get('window_count')) is int and row['window_count']>=0, 'CATALOG_SCENE_RESULT_BINDING')
                if completed:
                    _require(cursor=='scene-detection-complete' and len(recorded)==len(items) and job.get('progress_current')==len(items) and job.get('progress_total')==len(items),'CATALOG_SCENE_RESULT_INCOMPLETE')
            else:
                _require(set(payload)=={'check_id'} and _text(payload['check_id']) and task is None and library is None, 'CATALOG_CHECK_PAYLOAD_INVALID')
                check=checks.get(payload['check_id'])
                _require(check is not None and _sha(check.get('scope_digest')), 'CATALOG_CHECK_MISSING')
                _require(check.get('status') in {'queued','running','completed','failed'}, 'CATALOG_CHECK_STATE_INVALID')
                if completed:
                    _require(cursor=='packaging_catalog_check:complete' and check['status']=='completed','CATALOG_CHECK_STATE_INVALID')
                if check['status']=='completed':
                    result=_object(check.get('result_json'))
                    statuses={'added','updated','missing','invalid','unchanged'}
                    _require(set(result)=={'counts','items'} and type(result['counts']) is dict and set(result['counts'])==statuses and type(result['items']) is list, 'CATALOG_CHECK_RESULT_INVALID')
                    for item in result['items']:
                        _require(type(item) is dict and item.get('status') in statuses and _relative(item.get('relativePath')), 'CATALOG_CHECK_RESULT_INVALID')
                    for status,count in result['counts'].items():
                        _require(type(count) is int and count==sum(item['status']==status for item in result['items']), 'CATALOG_CHECK_RESULT_COUNTS')
            report['jobs'].append(dict(jobId=identity,jobType=kind,status=job['status'],adapter='catalog-job-history-v1',canReplay=False,businessApprovalReusable=False,evidenceGrade='validated_historical_request'))
            report['recognizedJobIds'].append(identity)
        except (ValueError, TypeError, KeyError, RecoveryPlanError) as error:
            code = str(error).partition(':')[0]
            if re.fullmatch('[A-Z][A-Z0-9_]+',code) is None:
                code='CATALOG_JOB_RECORD_INVALID'
            report['unresolved'].append(dict(table='job_payloads',rowId=identity,code=code))
            pending('job_payloads',identity,code)
    return report
