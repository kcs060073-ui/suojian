"""Task-local voice drafts. No provider calls, public migrations or gate approvals.

The coordinator supplies a current authoritative context digest and performs
downstream invalidation in the same transaction through ``apply_effects``.
Provider substitution capabilities must be server-owned, verified allowlists.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from .ad_voice_preview import measured_voice_duration


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def compile_voice_input(script, rules, voice, capabilities):
    """Compile explicit character spans without changing authoritative copy.

    Numeric reading substitutions deliberately fail closed until a separately
    verified provider phoneme adapter can preserve their meaning.
    """
    if not isinstance(script, str) or not script.strip() or len(script) > 100_000:
        raise ValueError('SCRIPT_INVALID')
    if not isinstance(voice, dict) or any(not isinstance(voice.get(k), str) or not voice[k].strip()
                                        for k in ('provider', 'resource', 'voice', 'revision')):
        raise ValueError('VOICE_VERSION_REQUIRED')
    if not isinstance(rules, list) or len(rules) > 500:
        raise ValueError('RULES_INVALID')
    checked = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError('RULE_INVALID')
        start, end = rule.get('start'), rule.get('end')
        if any(type(v) is not int for v in (start, end)) or not 0 <= start < end <= len(script):
            raise ValueError('RULE_SPAN_INVALID')
        original, reading = rule.get('original'), rule.get('reading')
        if script[start:end] != original or not isinstance(reading, str) or not reading.strip():
            raise ValueError('RULE_TEXT_INVALID')
        if type(rule.get('version')) is not int or rule['version'] < 1 or rule.get('source') not in ('user', 'confirmed_dictionary'):
            raise ValueError('RULE_VERSION_REQUIRED')
        if original == reading:
            continue
        if re.search(r'[0-9０-９零〇一二两三四五六七八九十百千万亿%％]', original + reading):
            raise ValueError('NUMERIC_READING_REQUIRES_PHONEME_ADAPTER')
        if reading not in capabilities.get('substitutions', {}).get(original, []):
            raise ValueError('PROVIDER_READING_UNSUPPORTED')
        checked.append({k: rule[k] for k in ('start', 'end', 'original', 'reading', 'version', 'source')})
    checked.sort(key=lambda v: (v['start'], v['end']))
    previous = cursor = 0
    pieces, mapping = [], []
    for rule in checked:
        if rule['start'] < previous:
            raise ValueError('RULE_OVERLAP')
        prefix = script[previous:rule['start']]
        pieces.append(prefix)
        if prefix:
            mapping.append(dict(original_start=previous, original_end=rule['start'], tts_start=cursor, tts_end=cursor+len(prefix)))
        cursor += len(prefix)
        pieces.append(rule['reading'])
        mapping.append(dict(original_start=rule['start'], original_end=rule['end'], tts_start=cursor, tts_end=cursor+len(rule['reading'])))
        cursor += len(rule['reading'])
        previous = rule['end']
    pieces.append(script[previous:])
    if previous < len(script):
        mapping.append(dict(original_start=previous, original_end=len(script), tts_start=cursor, tts_end=cursor+len(script[previous:])))
    config = dict(script=script, script_sha256=hashlib.sha256(script.encode('utf-8')).hexdigest(),
                  rules=checked, voice=dict(voice), tts_text=''.join(pieces), span_map=mapping,
                  compiler_version='span-substitution-v1')
    # Global dictionary metadata is deliberately excluded: only consumed rules
    # and the actual provider input invalidate a task's audio.
    config['fingerprint'] = _digest(config)
    return config


def _check_config(config):
    value = dict(config)
    fingerprint = value.pop('fingerprint', None)
    if fingerprint != _digest(value):
        raise ValueError('CONFIG_DIGEST_INVALID')
    return config


class VoiceEditing:
    def __init__(self, db, audio_root: Path, *, duration_reader=None):
        self.db = db
        self.audio_root = Path(audio_root).resolve()
        self.duration_reader = duration_reader or measured_voice_duration

    def _one(self, table, key, value):
        row = self.db.connection.execute(f'SELECT * FROM {table} WHERE {key}=?', (value,)).fetchone()
        if row is None:
            raise ValueError('VOICE_RECORD_NOT_FOUND')
        result = dict(row)
        for field in ('config', 'audio', 'cost'):
            if field + '_json' in result:
                raw = result.pop(field + '_json')
                result[field] = json.loads(raw) if raw else None
        return result

    def current(self, task_id):
        return self._one('voice_edit_heads', 'task_id', task_id)

    def draft(self, draft_id):
        return self._one('voice_edit_drafts', 'draft_id', draft_id)

    def request(self, request_id):
        return self._one('voice_edit_requests', 'request_id', request_id)

    def versions(self, task_id):
        rows=self.db.connection.execute('SELECT * FROM voice_edit_versions WHERE task_id=? ORDER BY revision',(task_id,)).fetchall()
        return [{**dict(row),'config':json.loads(row['config_json']),'audio':json.loads(row['audio_json']) if row['audio_json'] else None} for row in rows]

    def _record_version(self, conn, task_id, reason):
        conn.execute('INSERT INTO voice_edit_versions SELECT task_id,revision,context_digest,config_json,audio_json,? FROM voice_edit_heads WHERE task_id=?',
                     (reason,task_id))

    def initialize(self, task_id, route, context_digest, config, *, audio=None, pre_write_check=None):
        """Optional pure server check runs in this writer transaction before insert.

        The check must only read authoritative inputs: no nested transaction,
        network, file write or commit. Raising aborts this entire transaction.
        An existing matching head is a read-only return and skips the check.
        """
        if pre_write_check is not None and not callable(pre_write_check):
            raise ValueError('PRE_WRITE_CHECK_INVALID')
        if route != 'ad_mix':
            raise ValueError('DIGITAL_ORIGINAL_VOICE_PRESERVED')
        _check_config(config)
        if not task_id or not context_digest:
            raise ValueError('TASK_CONTEXT_REQUIRED')
        if audio:
            audio = self._verify_audio(audio, config['fingerprint'], config['script'])
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute('SELECT * FROM voice_edit_heads WHERE task_id=?', (task_id,)).fetchone()
            if row is not None:
                current = self.current(task_id)
                if current['context_digest'] != context_digest or current['config']['fingerprint'] != config['fingerprint']:
                    raise ValueError('VOICE_ALREADY_INITIALIZED')
                return current
            if pre_write_check is not None:
                pre_write_check()
            conn.execute('INSERT INTO voice_edit_heads VALUES(?,0,?,?,?)',
                         (task_id, context_digest, _json(config), _json(audio) if audio else None))
            self._record_version(conn,task_id,'initial')
        return self.current(task_id)

    def rebind_upstream(self, task_id, expected_revision, previous_context_digest,
                        new_context_digest, config, *, route, apply_effects, pre_write_check=None, discard_audio=False):
        """Confirmed upstream workflow creates a new head; old versions remain readable.

        The caller supplies/rechecks the authoritative new script and service
        context inside apply_effects, never values forwarded from the browser.
        Optional pure pre_write_check runs under the writer lock even for replay.
        Confirmed script rework can discard the active audio without deleting its
        historical version: a matching script does not rebind an old bundle receipt.
        """
        if type(discard_audio) is not bool:
            raise ValueError('VOICE_REQUEST_INVALID')
        if route!='ad_mix':
            raise ValueError('DIGITAL_ORIGINAL_VOICE_PRESERVED')
        _check_config(config)
        if not new_context_digest or not callable(apply_effects):
            raise ValueError('ATOMIC_APPLY_ADAPTER_REQUIRED')
        if pre_write_check is not None and not callable(pre_write_check):
            raise ValueError('PRE_WRITE_CHECK_INVALID')
        with self.db.transaction(immediate=True) as conn:
            if pre_write_check is not None:pre_write_check()
            head=self.current(task_id)
            previous=conn.execute('SELECT context_digest FROM voice_edit_versions WHERE task_id=? AND revision=?',
                                  (task_id,expected_revision)).fetchone()
            if (head['revision']==expected_revision+1 and head['context_digest']==new_context_digest
                    and head['config']['fingerprint']==config['fingerprint'] and previous is not None
                    and previous['context_digest']==previous_context_digest):
                return head
            self._guard(head,expected_revision,previous_context_digest)
            if new_context_digest==previous_context_digest:
                raise ValueError('UPSTREAM_CONTEXT_NOT_CHANGED')
            audio=head['audio'] if not discard_audio and head['config']['fingerprint']==config['fingerprint'] else None
            if audio:audio=self.verify_audio(config,audio)
            apply_effects(conn,head,config,audio)
            conn.execute('UPDATE voice_edit_heads SET revision=?,context_digest=?,config_json=?,audio_json=? WHERE task_id=?',
                         (head['revision']+1,new_context_digest,_json(config),_json(audio) if audio else None,task_id))
            self._record_version(conn,task_id,'upstream_rebind')
        return self.current(task_id)

    def restore_version(self, task_id, target_revision, expected_revision, context_digest, apply_effects):
        """Explicitly restore verified audio as a NEW version, never revive approvals."""
        if not callable(apply_effects):
            raise ValueError('ATOMIC_APPLY_ADAPTER_REQUIRED')
        with self.db.transaction(immediate=True) as conn:
            head=self.current(task_id)
            self._guard(head,expected_revision,context_digest)
            row=conn.execute('SELECT config_json,audio_json,context_digest FROM voice_edit_versions WHERE task_id=? AND revision=?',
                             (task_id,target_revision)).fetchone()
            if row is None:
                raise ValueError('VOICE_VERSION_NOT_FOUND')
            config=json.loads(row['config_json'])
            if row['context_digest']!=context_digest or config['script_sha256']!=head['config']['script_sha256']:
                raise ValueError('CONTEXT_CHANGED')
            if not row['audio_json']:
                raise ValueError('AUDIO_NOT_READY')
            audio=self.verify_audio(config,json.loads(row['audio_json']))
            if config['fingerprint']==head['config']['fingerprint'] and head['audio'] is not None:
                try:
                    current_audio=self.verify_audio(head['config'],head['audio'])
                except ValueError:
                    # A valid target repairs an invalid current receipt/file.
                    current_audio=None
                # Location is not identity. All other verified receipt fields,
                # including measured duration and timing/provenance, must match.
                if current_audio is not None and head['audio']==current_audio and (
                        {k:v for k,v in audio.items() if k!='path'} ==
                        {k:v for k,v in current_audio.items() if k!='path'}):
                    return head
            apply_effects(conn,head,config,audio)
            conn.execute('UPDATE voice_edit_heads SET revision=?,config_json=?,audio_json=? WHERE task_id=?',
                         (head['revision']+1,_json(config),_json(audio),task_id))
            self._record_version(conn,task_id,'restore:'+str(target_revision))
        return self.current(task_id)

    def create_draft(self, task_id, base_revision, context_digest, config, *, draft_id=None, pre_write_check=None, initial_config=None, route=None):
        """A server-owned stable ID replays its original draft, never a new head.

        Explicit IDs are lowercase SHA-256 hex. Omission keeps the UUID contract.
        Access authorization and deriving IDs from task + operation belong to the
        public adapter; browser values must not be forwarded as trusted IDs.
        pre_write_check follows initialize's pure callback contract. First insert
        invokes it under this writer transaction; consistent replay skips it.
        Optional initial_config creates a revision-zero bookkeeping head and the
        first draft atomically, only for explicit ad_mix. It never activates audio.
        """
        if pre_write_check is not None and not callable(pre_write_check):
            raise ValueError('PRE_WRITE_CHECK_INVALID')
        if draft_id is not None and (not isinstance(draft_id, str) or not re.fullmatch(r'[0-9a-f]{64}', draft_id)):
            raise ValueError('DRAFT_ID_INVALID')
        if type(base_revision) is not int or base_revision < 0:
            raise ValueError('VERSION_CONFLICT')
        _check_config(config)
        if initial_config is not None:
            if route != 'ad_mix':raise ValueError('DIGITAL_ORIGINAL_VOICE_PRESERVED')
            _check_config(initial_config)
            if not task_id or not context_digest:raise ValueError('TASK_CONTEXT_REQUIRED')
        with self.db.transaction(immediate=True) as conn:
            if draft_id is not None:
                owner = conn.execute('SELECT task_id FROM voice_edit_drafts WHERE draft_id=?', (draft_id,)).fetchone()
                if owner is not None:
                    # Check ownership before reading another task's config/status.
                    if owner['task_id'] != task_id:
                        raise ValueError('REQUEST_ID_CONFLICT')
                    existing = self.draft(draft_id)
                    if (existing['base_revision'] != base_revision or existing['context_digest'] != context_digest
                            or existing['config']['fingerprint'] != config['fingerprint']):
                        raise ValueError('REQUEST_ID_CONFLICT')
                    return existing
            exists=conn.execute('SELECT 1 FROM voice_edit_heads WHERE task_id=?',(task_id,)).fetchone()
            if exists is None and initial_config is not None:
                head=dict(task_id=task_id,revision=0,context_digest=context_digest,config=initial_config,audio=None)
            else:
                head=self.current(task_id)
            self._guard(head, base_revision, context_digest)
            if config['script_sha256'] != head['config']['script_sha256']:
                raise ValueError('SCRIPT_CHANGE_REQUIRES_SCRIPT_WORKFLOW')
            if pre_write_check is not None:
                pre_write_check()
            if exists is None:
                conn.execute('INSERT INTO voice_edit_heads VALUES(?,0,?,?,NULL)',
                             (task_id,context_digest,_json(initial_config)))
                self._record_version(conn,task_id,'initial')
            draft_id = draft_id if draft_id is not None else uuid.uuid4().hex
            conn.execute('INSERT INTO voice_edit_drafts VALUES(?,?,?,?,?,\'draft\',NULL)',
                         (draft_id, task_id, base_revision, context_digest, _json(config)))
        return self.draft(draft_id)

    @staticmethod
    def _guard(head, revision, context_digest):
        if head['context_digest'] != context_digest:
            raise ValueError('CONTEXT_CHANGED')
        if head['revision'] != revision:
            raise ValueError('VERSION_CONFLICT')

    def cancel(self, draft_id):
        with self.db.transaction(immediate=True) as conn:
            draft = self.draft(draft_id)
            if draft['status'] == 'applied':
                raise ValueError('ALREADY_APPLIED')
            conn.execute("UPDATE voice_edit_drafts SET status='cancelled' WHERE draft_id=?", (draft_id,))
        return self.draft(draft_id)

    def prepare(self, draft_id, request_id, *, generate_missing_audio=False):
        with self.db.transaction(immediate=True) as conn:
            return self._prepare_in_transaction(conn,draft_id,request_id,generate_missing_audio=generate_missing_audio)

    def _prepare_in_transaction(self, conn, draft_id, request_id, *, generate_missing_audio=False):
        # Root queue uses this inside its existing writer transaction: preparation
        # and enqueue either both commit or neither leaves a stranded request.
        if conn is not self.db.connection or not conn.in_transaction:
            raise ValueError('VOICE_WRITER_TRANSACTION_REQUIRED')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
            raise ValueError('REQUEST_ID_INVALID')
        draft = self.draft(draft_id)
        if draft['status'] != 'draft':
            raise ValueError('DRAFT_CLOSED')
        head = self.current(draft['task_id'])
        self._guard(head, draft['base_revision'], draft['context_digest'])
        if head['config']['fingerprint'] == draft['config']['fingerprint']:
            usable = False
            if head['audio']:
                try:
                    self.verify_audio(head['config'],head['audio'])
                    usable = True
                except ValueError:
                    pass
            if generate_missing_audio is not True or usable:
                raise ValueError('UNCHANGED_NO_GENERATION')
        row = conn.execute('SELECT request_id FROM voice_edit_requests WHERE draft_id=? ORDER BY rowid DESC LIMIT 1', (draft_id,)).fetchone()
        if row:
            if row['request_id'] != request_id:
                raise ValueError('REQUEST_ALREADY_EXISTS')
            return self.request(request_id)
        if conn.execute('SELECT 1 FROM voice_edit_requests WHERE request_id=?', (request_id,)).fetchone():
            raise ValueError('REQUEST_ID_CONFLICT')
        unresolved = conn.execute(
            "SELECT 1 FROM voice_edit_requests r JOIN voice_edit_drafts d ON d.draft_id=r.draft_id "
            "WHERE d.task_id=? AND r.status IN ('running','unknown') AND COALESCE(r.error_code,'')!='VOICE_REQUEST_RELEASED'", (draft['task_id'],)
        ).fetchone()
        if unresolved:
            raise ValueError('UNRESOLVED_REQUEST_REQUIRES_RECONCILIATION')
        conn.execute("INSERT INTO voice_edit_requests VALUES(?,?,?,'prepared',NULL,NULL,NULL)",
                     (request_id, draft_id, draft['config']['fingerprint']))
        return self.request(request_id)

    def retry_failed(self, request_id, new_request_id):
        """Explicit retry keeps attempts on the same draft; never retries unknown."""
        if not isinstance(new_request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',new_request_id):
            raise ValueError('REQUEST_ID_INVALID')
        with self.db.transaction(immediate=True) as conn:
            previous=self.request(request_id)
            if previous['status']!='failed':
                raise ValueError('REQUEST_NOT_FAILED')
            draft=self.draft(previous['draft_id'])
            if draft['status']!='draft':
                raise ValueError('DRAFT_CLOSED')
            self._guard(self.current(draft['task_id']),draft['base_revision'],draft['context_digest'])
            latest=conn.execute('SELECT request_id FROM voice_edit_requests WHERE draft_id=? ORDER BY rowid DESC LIMIT 1',(draft['draft_id'],)).fetchone()['request_id']
            if latest==new_request_id and new_request_id!=request_id:
                return self.request(new_request_id)
            if latest!=request_id:
                raise ValueError('REQUEST_ALREADY_SUPERSEDED')
            if conn.execute('SELECT 1 FROM voice_edit_requests WHERE request_id=?',(new_request_id,)).fetchone():
                raise ValueError('REQUEST_ID_CONFLICT')
            if conn.execute("SELECT 1 FROM voice_edit_requests r JOIN voice_edit_drafts d ON d.draft_id=r.draft_id WHERE d.task_id=? AND r.status IN ('running','unknown') AND COALESCE(r.error_code,'')!='VOICE_REQUEST_RELEASED'",(draft['task_id'],)).fetchone():
                raise ValueError('UNRESOLVED_REQUEST_REQUIRES_RECONCILIATION')
            conn.execute("INSERT INTO voice_edit_requests VALUES(?,?,?,'prepared',NULL,NULL,NULL)",
                         (new_request_id,draft['draft_id'],previous['fingerprint']))
        return self.request(new_request_id)

    def start(self, request_id):
        """Claim once. Unknown results never implicitly retry or create charges."""
        with self.db.transaction(immediate=True) as conn:
            req = self.request(request_id)
            draft = self.draft(req['draft_id'])
            if draft['status'] != 'draft':
                raise ValueError('DRAFT_CLOSED')
            self._guard(self.current(draft['task_id']), draft['base_revision'], draft['context_digest'])
            if req['status'] != 'prepared':
                return False
            if conn.execute(
                "SELECT 1 FROM voice_edit_requests r JOIN voice_edit_drafts d ON d.draft_id=r.draft_id "
                "WHERE d.task_id=? AND r.status IN ('running','unknown') AND COALESCE(r.error_code,'')!='VOICE_REQUEST_RELEASED'", (draft['task_id'],)
            ).fetchone():
                raise ValueError('UNRESOLVED_REQUEST_REQUIRES_RECONCILIATION')
            changed = conn.execute("UPDATE voice_edit_requests SET status='running' WHERE request_id=? AND status='prepared'", (request_id,))
            return changed.rowcount == 1

    def fail(self, request_id, error_code, *, unknown=False):
        with self.db.transaction(immediate=True) as conn:
            changed = conn.execute("UPDATE voice_edit_requests SET status=?,error_code=? WHERE request_id=? AND status='running'",
                                   ('unknown' if unknown else 'failed', str(error_code)[:120], request_id))
            if changed.rowcount != 1:
                raise ValueError('REQUEST_STATE_CONFLICT')
        return self.request(request_id)

    def reconcile_failure(self, request_id, *, provider_receipt, cost):
        """Trusted worker records provider-confirmed failure, never a timeout guess.

        This settles the old request only. A new explicit user request is still
        required; no automatic synthesis or charging occurs here.
        """
        if not isinstance(provider_receipt, str) or not provider_receipt.strip():
            raise ValueError('PROVIDER_RECEIPT_REQUIRED')
        if not isinstance(cost, dict) or cost.get('status') not in ('known', 'unknown'):
            raise ValueError('COST_STATUS_REQUIRED')
        if cost['status'] == 'known' and (type(cost.get('amount')) not in (int,float)
                or not math.isfinite(cost['amount']) or cost['amount'] < 0):
            raise ValueError('COST_AMOUNT_INVALID')
        recorded = {**cost, 'reconciliation': {'provider_receipt':provider_receipt, 'result':'failed'}}
        with self.db.transaction(immediate=True) as conn:
            request = self.request(request_id)
            if request['status'] == 'failed' and request['cost'] == recorded:
                return request
            if request['status'] != 'unknown':
                raise ValueError('REQUEST_STATE_CONFLICT')
            conn.execute("UPDATE voice_edit_requests SET status='failed',error_code='PROVIDER_CONFIRMED_FAILURE',cost_json=? WHERE request_id=?",
                         (_json(recorded),request_id))
        return self.request(request_id)

    def _verify_audio(self, audio, fingerprint, script):
        if audio.get('fingerprint') != fingerprint:
            raise ValueError('AUDIO_BINDING_INVALID')
        raw_path = Path(audio.get('path', ''))
        path = self.audio_root / raw_path
        if raw_path.is_absolute() or '..' in raw_path.parts or not raw_path.parts:
            raise ValueError('AUDIO_PATH_INVALID')
        if not path.resolve().is_relative_to(self.audio_root) or any(p.is_symlink() for p in [path, *path.parents] if p != self.audio_root):
            raise ValueError('AUDIO_PATH_INVALID')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != audio.get('sha256'):
            raise ValueError('AUDIO_INVALID')
        duration = self.duration_reader(path)
        if duration is None:
            raise ValueError('AUDIO_INVALID')
        timing = audio.get('timing')
        if audio.get('timing_kind') not in ('measured_sentence', 'measured_word') or not isinstance(timing, list) or not timing:
            raise ValueError('MEASURED_TIMING_REQUIRED')
        previous = 0
        for part in timing:
            start, end = part.get('start'), part.get('end')
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end))
                    or not previous <= start < end <= duration + .05 or not isinstance(part.get('text'), str)):
                raise ValueError('AUDIO_TIMING_INVALID')
            previous = end
        if ''.join(p['text'] for p in timing) != script:
            raise ValueError('AUDIO_TIMING_ORIGINAL_TEXT_REQUIRED')
        if not audio.get('provider_receipt'):
            raise ValueError('PROVIDER_RECEIPT_REQUIRED')
        return {**audio, 'duration': duration}

    def verify_audio(self, config, audio):
        """Validate an existing segment receipt without creating a generation job."""
        _check_config(config)
        return self._verify_audio(audio,config['fingerprint'],config['script'])

    def finish(self, request_id, audio, *, cost):
        with self.db.transaction(immediate=True) as conn:
            req = self.request(request_id)
            if audio.get('request_id') != request_id:
                raise ValueError('AUDIO_REQUEST_INVALID')
            draft = self.draft(req['draft_id'])
            checked = self._verify_audio(audio, req['fingerprint'], draft['config']['script'])
            if req['status'] == 'ready':
                if req['audio'] == checked and req['cost'] == cost:
                    return req
                raise ValueError('REQUEST_RESULT_CONFLICT')
            if req['status'] not in ('running', 'unknown') or req.get('error_code') == 'VOICE_REQUEST_RELEASED':
                raise ValueError('REQUEST_STATE_CONFLICT')
            if not isinstance(cost, dict) or cost.get('status') not in ('known', 'unknown'):
                raise ValueError('COST_STATUS_REQUIRED')
            if cost['status'] == 'known' and (type(cost.get('amount')) not in (int, float) or not math.isfinite(cost['amount']) or cost['amount'] < 0):
                raise ValueError('COST_AMOUNT_INVALID')
            conn.execute("UPDATE voice_edit_requests SET status='ready',audio_json=?,cost_json=?,error_code=NULL WHERE request_id=?",
                         (_json(checked), _json(cost), request_id))
        return self.request(request_id)

    def apply(self, draft_id, current_context_digest, apply_effects):
        """Caller must recompute context and invalidate actual downstream rows.

        ``apply_effects(connection, old_head, new_config, new_audio)`` MUST only
        mutate this SQLite transaction; no file writes, network or commit.
        """
        if not callable(apply_effects):
            raise ValueError('ATOMIC_APPLY_ADAPTER_REQUIRED')
        with self.db.transaction(immediate=True) as conn:
            draft = self.draft(draft_id)
            head = self.current(draft['task_id'])
            if head['context_digest'] != current_context_digest:
                raise ValueError('CONTEXT_CHANGED')
            if draft['status'] == 'applied':
                if head['revision'] != draft['applied_revision']:
                    raise ValueError('VERSION_CONFLICT')
                return head
            if draft['status'] != 'draft':
                raise ValueError('DRAFT_CLOSED')
            self._guard(head, draft['base_revision'], current_context_digest)
            changed = head['config']['fingerprint'] != draft['config']['fingerprint']
            revision = head['revision']
            row = conn.execute("SELECT request_id FROM voice_edit_requests WHERE draft_id=? AND status='ready' ORDER BY rowid DESC LIMIT 1", (draft_id,)).fetchone()
            if row is None and conn.execute('SELECT 1 FROM voice_edit_requests WHERE draft_id=?',(draft_id,)).fetchone():
                raise ValueError('AUDIO_NOT_READY')
            ready_audio = self.request(row['request_id'])['audio'] if row else None
            if changed or (ready_audio is not None and ready_audio != head['audio']):
                if row is None:
                    raise ValueError('AUDIO_NOT_READY')
                audio = self._verify_audio(ready_audio, draft['config']['fingerprint'], draft['config']['script'])
                apply_effects(conn, head, draft['config'], audio)
                revision += 1
                conn.execute('UPDATE voice_edit_heads SET revision=?,config_json=?,audio_json=? WHERE task_id=?',
                             (revision, _json(draft['config']), _json(audio), draft['task_id']))
                self._record_version(conn,draft['task_id'],'voice_apply')
            conn.execute("UPDATE voice_edit_drafts SET status='applied',applied_revision=? WHERE draft_id=?", (revision, draft_id))
        return self.current(draft['task_id'])
