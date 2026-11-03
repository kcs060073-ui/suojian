"""Conservative unresolved scope and local, evidence-bound protocol certification.

Certification never changes job/outcome/item history. It is a maintenance operation,
not a browser/API override. The operator must first stop and cancel the old worker.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DISPATCH_PROTOCOL = "durable-visual-dispatch-receipt-v1"
PARALLEL_DISPATCH_PROTOCOL = "bounded-parallel-durable-v1"

def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()

def _history(connection, job_id):
    attempts = [dict(r) for r in connection.execute(
        "SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_no", (job_id,))]
    rows = [dict(r) for r in connection.execute(
        "SELECT * FROM visual_batch_items WHERE job_id=? ORDER BY item_index", (job_id,))]
    return attempts, rows

def _serial_valid(rows, size):
    if [r['item_index'] for r in rows] != list(range(len(rows))) or len(rows) > size:
        return False
    if any(r['status'] not in {'succeeded','failed','outcome_unknown'} for r in rows):
        return False
    unknown = [r['item_index'] for r in rows if r['status']=='outcome_unknown']
    return not unknown or unknown == [len(rows)-1]

def _certificate(connection, job_id):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='visual_resume_certifications'").fetchone():
        return None
    return connection.execute('SELECT * FROM visual_resume_certifications WHERE job_id=?', (job_id,)).fetchone()

def parallel_execution_payload_digest(payload):
    """Bind the queued inputs; the executing worker's evidence cannot hash itself."""
    return _digest({key: value for key, value in payload.items()
                    if key != "execution_protocol_evidence"})


def _source_identity(item):
    asset_id, fingerprint = item["assetId"], item["assetFingerprint"]
    start, end = item["sourceStartUs"], item["sourceEndUs"]
    if (not isinstance(asset_id, str) or not asset_id
            or not isinstance(fingerprint, str) or not fingerprint
            or type(start) is not int or type(end) is not int
            or start < 0 or end < start):
        raise ValueError("invalid visual source identity")
    return asset_id, fingerprint, start, end


def _parallel_unresolved_items(connection, job, payload):
    """Absence proves unsent only under a stopped attempt's durable protocol.

    A queue-side protocol label alone cannot certify an older worker. Missing,
    malformed or contradictory evidence conservatively blocks the whole scope.
    """
    items = payload.get("items", [])
    try:
        evidence = payload.get("execution_protocol_evidence")
        if not isinstance(evidence, dict) or payload.get("execution_protocol") != PARALLEL_DISPATCH_PROTOCOL:
            return items
        attempts, rows = _history(connection, job["job_id"])
        if len(attempts) != 1:
            return items
        attempt = attempts[0]
        if (attempt["status"] not in {"interrupted", "cancelled", "failed"}
                or type(attempt["attempt_no"]) is not int or attempt["attempt_no"] < 1
                or not isinstance(attempt["worker_id"], str) or not attempt["worker_id"]
                or not attempt["finished_at"]
                or _utc(attempt["finished_at"]) < _utc(attempt["started_at"])):
            return items
        if (evidence.get("protocol") != PARALLEL_DISPATCH_PROTOCOL
                or evidence.get("job_id") != job["job_id"]
                or type(evidence.get("attempt_no")) is not int
                or evidence.get("attempt_no") != attempt["attempt_no"]
                or evidence.get("worker_id") != attempt["worker_id"]
                or evidence.get("started_at") != attempt["started_at"]
                or evidence.get("payload_digest") != parallel_execution_payload_digest(payload)):
            return items
        if not isinstance(items, list):
            return items
        identities = [_source_identity(item) for item in items]
        seen, unknown = set(), []
        for row in rows:
            index = row["item_index"]
            if type(index) is not int or index < 0 or index >= len(items) or index in seen:
                return items
            seen.add(index)
            identity = (row["asset_id"], row["asset_fingerprint"],
                        row["source_start_us"], row["source_end_us"])
            if identity != identities[index]:
                return items
            status, dispatched = row["status"], row["provider_dispatched"]
            if (status not in {"succeeded", "failed", "outcome_unknown"}
                    or type(dispatched) is not int or dispatched not in (0, 1)
                    or (status in {"succeeded", "outcome_unknown"} and dispatched != 1)):
                return items
            input_digest = row["input_digest"]
            if (dispatched or input_digest is not None) and (
                    not isinstance(input_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", input_digest)):
                return items
            if status == "outcome_unknown":
                unknown.append(index)
        # Receipt indices may have gaps or complete out of order. Under this
        # worker protocol every send commits its marker first, so gaps are unsent.
        return [items[index] for index in sorted(unknown)]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, sqlite3.Error):
        return items


def unresolved_items(connection, job, payload):
    items = payload.get('items', [])
    if job['status'] != 'cancelled' or not job['cancel_requested']:
        return items
    if "execution_protocol" in payload:
        return _parallel_unresolved_items(connection, job, payload)
    attempts = connection.execute('SELECT count(*) FROM job_attempts WHERE job_id=?', (job['job_id'],)).fetchone()[0]
    if attempts != 1:
        return items
    rows = connection.execute('SELECT item_index,status FROM visual_batch_items WHERE job_id=? ORDER BY item_index', (job['job_id'],)).fetchall()
    if not _serial_valid(rows, len(items)):
        return items
    certificate = _certificate(connection, job['job_id'])
    if certificate is not None:
        full_attempts, full_rows = _history(connection, job['job_id'])
        if certificate['protocol'] != DISPATCH_PROTOCOL or certificate['payload_digest'] != _digest(payload) or certificate['history_digest'] != _digest([full_attempts, full_rows]):
            return items  # Changed evidence invalidates the entire exceptional proof.
    unknown = [r['item_index'] for r in rows if r['status']=='outcome_unknown']
    if unknown:
        return [items[unknown[0]]]
    if certificate is not None:
        return []  # Every send first persisted a marker; absent rows were not sent.
    # Legacy serial execution could send before writing its receipt.
    return items[len(rows):len(rows)+1]

def _utc(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

def certify_dispatch_receipt_protocol(db, job_id, *, monitor_path, monitor_sha256,
                                      deployment_note):
    """Append an immutable, job-specific proof after reviewing deployed semantics.

    Requires stopped/cancelled single-attempt history, actual pre-dispatch marker
    snapshots during that attempt and retained provider_dispatched flags. Snapshot
    bytes and their SHA are preserved inside the audit row, so later file edits do
    not silently change the proof. No model request or formal result is produced.
    """
    evidence = Path(monitor_path).read_bytes()
    if hashlib.sha256(evidence).hexdigest() != monitor_sha256:
        raise ValueError('VISUAL_RESUME_EVIDENCE_HASH_MISMATCH')
    if not isinstance(deployment_note, str) or len(deployment_note.strip()) < 20:
        raise ValueError('VISUAL_RESUME_DEPLOYMENT_REVIEW_REQUIRED')
    snapshots = [json.loads(line) for line in evidence.decode('utf-8').splitlines() if line.strip()]
    with db.transaction(immediate=True) as connection:
        job = connection.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if not job or job['job_type'] != 'visual_analysis' or job['status'] != 'cancelled' or not job['cancel_requested']:
            raise ValueError('VISUAL_RESUME_STOPPED_CANCELLED_REQUIRED')
        saved = connection.execute('SELECT payload_json,outcome_status FROM job_payloads WHERE job_id=?', (job_id,)).fetchone()
        if not saved or saved['outcome_status'] != 'unknown':
            raise ValueError('VISUAL_RESUME_UNKNOWN_HISTORY_REQUIRED')
        payload = json.loads(saved['payload_json'])
        attempts, rows = _history(connection, job_id)
        if len(attempts) != 1 or attempts[0]['status'] not in ('interrupted','cancelled','failed') or not attempts[0]['finished_at']:
            raise ValueError('VISUAL_RESUME_SINGLE_STOPPED_ATTEMPT_REQUIRED')
        if not rows or not _serial_valid(rows, len(payload.get('items', []))) or any(r.get('provider_dispatched') != 1 for r in rows):
            raise ValueError('VISUAL_RESUME_DISPATCH_HISTORY_REQUIRED')
        start, end = _utc(attempts[0]['started_at']), _utc(attempts[0]['finished_at'])
        observed = []
        for snapshot in snapshots:
            if snapshot.get('jobId') != job_id:
                raise ValueError('VISUAL_RESUME_WRONG_JOB_EVIDENCE')
            marker = any(r.get('status') == 'outcome_unknown' and r.get('error_code') == 'VISUAL_REQUEST_DISPATCHED' and type(r.get('n')) is int and r['n'] > 0 for r in snapshot.get('counts', []))
            if marker:
                if snapshot.get('job', {}).get('status') != 'running' or not start <= _utc(snapshot['at']) <= end:
                    raise ValueError('VISUAL_RESUME_WRONG_ATTEMPT_EVIDENCE')
                observed.append(snapshot['at'])
        if not observed:
            raise ValueError('VISUAL_RESUME_OBSERVED_DISPATCH_REQUIRED')
        payload_digest, history_digest = _digest(payload), _digest([attempts, rows])
        connection.execute("""CREATE TABLE IF NOT EXISTS visual_resume_certifications(
            job_id TEXT PRIMARY KEY REFERENCES jobs(job_id), protocol TEXT NOT NULL,
            payload_digest TEXT NOT NULL, history_digest TEXT NOT NULL,
            monitor_sha256 TEXT NOT NULL, monitor_path TEXT NOT NULL,
            evidence_jsonl TEXT NOT NULL, deployment_note TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        existing = _certificate(connection, job_id)
        if existing:
            if any(existing[k] != v for k,v in {'protocol':DISPATCH_PROTOCOL,'payload_digest':payload_digest,'history_digest':history_digest,'monitor_sha256':monitor_sha256}.items()):
                raise ValueError('VISUAL_RESUME_CERTIFICATION_CONFLICT')
            return dict(existing)
        connection.execute('INSERT INTO visual_resume_certifications VALUES(?,?,?,?,?,?,?,?,?)',
            (job_id,DISPATCH_PROTOCOL,payload_digest,history_digest,monitor_sha256,
             str(Path(monitor_path).resolve()),evidence.decode('utf-8'),deployment_note,
             datetime.now(timezone.utc).isoformat()))
        return dict(_certificate(connection, job_id))
