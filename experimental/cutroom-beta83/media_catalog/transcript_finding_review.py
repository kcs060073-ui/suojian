"""Persist listening decisions in the existing, rework-archived transcript receipt."""
from datetime import datetime, timezone
import json
import os
import uuid

from .tasks import get_task
from .task_script import transcript_has_current_downstream
from .transcript_reconciliation import project_transcript_state, _task_artifacts


def save_finding_review(db, project_root, task_id, body):
    # Rework and transcript recovery use the same database write lock. A stale
    # browser therefore cannot put a receipt into newly replaced artifacts.
    with db.transaction(immediate=True) as connection:
        task = get_task(db, task_id)
        if connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running','pending') LIMIT 1", (task_id,)).fetchone():
            raise ValueError('TASK_BUSY: 任务正在处理，请稍后核对')
        if transcript_has_current_downstream(db, task_id):
            raise ValueError('REWORK_REQUIRED: 此版本已有分镜或成片，请通过返工流程重新核对')
        state = project_transcript_state(project_root, task, verify_source_content=True)
        if not state or state['status'] not in {'needs_review', 'reviewed'}:
            raise ValueError('TRANSCRIPT_REVIEW_UNAVAILABLE: 当前没有可核对的疑点')
        if body.get('expectedDigest') != state['digest']:
            raise ValueError('TRANSCRIPT_REVIEW_STALE: 核对依据已变化，请刷新后回听')
        finding = next((f for f in state['findings'] if f['id'] == body.get('findingId')), None)
        if not finding:
            raise ValueError('TRANSCRIPT_FINDING_NOT_FOUND: 当前版本没有这条疑点')
        revision = body.get('expectedReviewRevision')
        if type(revision) is not int or revision != state['reviewRevision']:
            raise ValueError('TRANSCRIPT_REVIEW_REVISION_CHANGED: 核对记录已更新，请刷新后继续')
        decision, note = body.get('decision'), body.get('note', '')
        if decision not in {'matches_audio', 'needs_fix'} or not isinstance(note, str) or len(note) > 2000:
            raise ValueError('TRANSCRIPT_REVIEW_INVALID: 核对决定或说明无效，说明最多2000字')
        if decision == 'needs_fix' and not note.strip():
            raise ValueError('TRANSCRIPT_NOTE_REQUIRED: 请写明需要修改的内容')
        reviews = {f['id']: f['review'] for f in state['findings'] if f.get('review')}
        reviews[finding['id']] = {'decision': decision, 'note': note.strip(),
                                  'reviewed_at': datetime.now(timezone.utc).isoformat()}
        receipt = {'digest': state['digest'], 'decision': 'pending',
                   'revision': revision + 1, 'findings': reviews}
        artifacts = _task_artifacts(project_root, task)
        temporary = artifacts / f'.transcript-review.{uuid.uuid4().hex}.tmp'
        try:
            temporary.write_text(json.dumps(receipt, ensure_ascii=False) + '\n', encoding='utf8')
            os.replace(temporary, artifacts / 'transcript-review.json')
        finally:
            temporary.unlink(missing_ok=True)
        return project_transcript_state(project_root, task)
