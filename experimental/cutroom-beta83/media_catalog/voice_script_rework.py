"""Rebind an editor only for a provable, formally confirmed script rework.

This does not generate, register, restore or activate audio. Prior audio and
requests stay immutable; the ordinary context-recovery endpoint stays strict.
"""
from contextlib import nullcontext
import json

from .provenance import canonical_digest
from .tasks import get_task
from .voice_task_context import resolve_voice_task_context, compile_task_voice_request, VoiceTaskContextError
from .voice_ownership import read_voice_ownership


class _ConfirmationTransaction:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def transaction(self, **kwargs):
        if not self.connection.in_transaction:
            raise RuntimeError('VOICE_CONFIRMATION_TRANSACTION_REQUIRED')
        return nullcontext(self.connection)


def confirmed_script_voice_plan(db, root, task_id, *, previous_gate_id=None, _source_head=None):
    """Read-only proof for a confirmed script rework; does not start a transaction.

    A historical applied rework alone is not sufficient: it must invalidate the
    immediately preceding brief gate and the voice head must belong to that gate
    with the same frozen service. Further draft edits within that rework are valid.
    """
    from .task_script import ScriptDraftError

    def fail(code='CONTEXT_CHANGED'):
        message = ('旧配音生成请求尚未核清，请先核对请求结果后再继续。'
                   if code == 'VOICE_GENERATION_UNRESOLVED' else
                   '配音记录与本次正式改稿不一致，请重新核对文案及配音服务。')
        raise ScriptDraftError(409, code, message)

    task = get_task(db, task_id)
    if not task or task['task_type'] != 'ad_mix' or (task.get('brief') or {}).get('voice_mode') != 'ai_tts':
        return None
    if not db.connection.execute('SELECT 1 FROM voice_edit_heads WHERE task_id=?', (task_id,)).fetchone():
        return None
    rework = db.connection.execute(
        "SELECT impact_json FROM rework_requests WHERE task_id=? AND target_stage='brief_lock' "
        "AND status='applied' AND result_candidate_id IS NULL ORDER BY rowid DESC LIMIT 1", (task_id,),
    ).fetchone()
    if rework is None:
        return None
    change = json.loads(rework['impact_json'] or '{}')
    if not change.get('script_revised'):
        return None
    from .voice_bundle_service import _service
    service = _service(db, root, task_id)
    live_head = service.current(task_id)
    head = _source_head or live_head
    try:
        context = resolve_voice_task_context(db, root, task_id)
        if head['context_digest'] == context.context_digest:
            return None
        prior = change.get('prior_brief_gate_event_id')
        gate = db.connection.execute(
            "SELECT rowid,event_id,gate_digest FROM task_gate_events "
            "WHERE task_id=? AND event_id=? AND gate_type='brief'", (task_id, prior),
        ).fetchone()
        latest = db.connection.execute(
            "SELECT rowid,event_id FROM task_gate_events WHERE task_id=? "
            "AND gate_type='brief' ORDER BY rowid DESC LIMIT 1", (task_id,),
        ).fetchone()
        # Newly confirmed: caller observed prior. Upgrade repair: caller observed
        # the already-confirmed latest. Neither permits an unrelated gate chain.
        if (gate is None or latest is None or latest['rowid'] <= gate['rowid']
                or (previous_gate_id is not None and previous_gate_id not in (prior, latest['event_id']))
                or db.connection.execute(
                    "SELECT 1 FROM task_gate_events WHERE task_id=? AND gate_type='brief' "
                    "AND rowid>? AND rowid<? LIMIT 1", (task_id, gate['rowid'], latest['rowid']),
                ).fetchone()):
            fail()
        expected = canonical_digest(dict(task_id=task_id, route='ad_mix',
            script_sha256=head['config']['script_sha256'], gate_event_id=prior,
            gate_digest=gate['gate_digest'], tts_binding_digest=context.voice['revision'],
            frozen_voice=context.voice))
        if expected != head['context_digest']:
            fail()
        ownership = read_voice_ownership(db, task_id, context_digest=live_head['context_digest'])
        if ownership.branch == 'blocked' and not (
                ownership.reason == 'CURRENT_AUDIO_INVALID' and live_head['audio'] is None):
            fail()
        if db.connection.execute(
            "SELECT 1 FROM voice_edit_requests q JOIN voice_edit_drafts d ON d.draft_id=q.draft_id "
            "WHERE d.task_id=? AND q.status IN ('prepared','running','unknown') AND COALESCE(q.error_code,'')!='VOICE_REQUEST_RELEASED' LIMIT 1", (task_id,),
        ).fetchone():
            fail('VOICE_GENERATION_UNRESOLVED')
        config = compile_task_voice_request(db, root, task_id, dict(
            context_digest=context.context_digest, voice_id=head['config']['voice']['voice'], rules=[],
        ))['config']
        return dict(head=head, context=context, config=config, previous_gate_id=latest['event_id'])
    except VoiceTaskContextError as error:
        raise ScriptDraftError(409, error.code, error.message) from error


def rebind_confirmed_script_voice(db, root, task_id, *, previous_gate_id):
    """Join the caller's confirmation write transaction; never commit it here."""
    if not db.connection.in_transaction:
        raise RuntimeError('VOICE_CONFIRMATION_TRANSACTION_REQUIRED')
    plan = confirmed_script_voice_plan(db, root, task_id, previous_gate_id=previous_gate_id)
    if plan is None:
        return None
    from .voice_bundle_service import _service
    service = _service(_ConfirmationTransaction(db), root, task_id)
    head = plan['head']
    return service.rebind_upstream(task_id, head['revision'], head['context_digest'],
        plan['context'].context_digest, plan['config'], route='ad_mix', discard_audio=True,
        apply_effects=lambda *args: None)


def recover_confirmed_script_voice(db, root, task_id, body):
    """Explicit editor POST; recheck the read-only offer under the writer lock."""
    from .task_script import ScriptDraftError
    from .voice_bundle_service import _service

    def conflict():
        raise ScriptDraftError(409, 'CONTEXT_CHANGED', '文案或配音版本已变化，请重新打开配音编辑器。')

    with db.transaction(immediate=True):
        service = _service(db, root, task_id)
        head = service.current(task_id)
        context = resolve_voice_task_context(db, root, task_id)
        if context.context_digest != body['contextDigest']:
            conflict()
        if head['context_digest'] == context.context_digest:
            # A replay must prove the same one-step transition, not merely a
            # matching target script. Validate its original version and rework.
            if head['revision'] != body['expectedRevision'] + 1 or head['audio'] is not None:
                conflict()
            versions = service.versions(task_id)
            source = next((row for row in versions if row['revision'] == body['expectedRevision']), None)
            if (source is None or source['context_digest'] != body['previousContextDigest']
                    or versions[-1]['reason'] != 'upstream_rebind'):
                conflict()
            plan = confirmed_script_voice_plan(db, root, task_id, _source_head=source)
            if plan is None or plan['config'] != head['config']:
                conflict()
            return True
        plan = confirmed_script_voice_plan(db, root, task_id)
        if plan is None:
            return False
        if (head['revision'] != body['expectedRevision']
                or head['context_digest'] != body['previousContextDigest']):
            conflict()
        rebind_confirmed_script_voice(db, root, task_id, previous_gate_id=plan['previous_gate_id'])
        return True
