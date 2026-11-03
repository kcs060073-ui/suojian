"""Explicit, atomic revisions of current advice/user requirements. No forced closure."""
from .creative_assistance import (
    _context, _digest, _evaluate, _build_reshoot_list_locked, get_reshoot_list,
)


def revision_fields(db, need, cards):
    explicit = str(need.get('origin_key') or '').startswith(('advice:', 'user-revise:'))
    card = next((c for c in cards if c['card_id'] == need.get('card_id')), None)
    allowed = bool(need.get('active') and explicit and card is not None
                   and card['script_segment_id'] == need['script_segment_id'])
    result = {'canRevise': allowed}
    row = db.connection.execute(
        'SELECT old_query,reason FROM reshoot_requirement_revisions WHERE task_id=? AND new_gap_id=?',
        (need['task_id'], need['gap_id']),
    ).fetchone()
    if row:
        result['revision'] = {'previousQuery': row['old_query'], 'reason': row['reason']}
    return result


def _current_report(db, task_id, report_id, expected_revision):
    report = get_reshoot_list(db, task_id, report_id)
    if report['taskRevision'] != expected_revision:
        raise RuntimeError('RESHOOT_REVISION_CHANGED: 清单不属于当前任务版本')
    current = db.connection.execute('SELECT report_id FROM task_reshoot_current WHERE task_id=?', (task_id,)).fetchone()
    if current is None or current['report_id'] != report_id:
        raise RuntimeError('RESHOOT_REVISION_STALE: 请使用当前缺口清单')
    revision, digest, _ = _evaluate(db, task_id, expected_revision)
    if revision != report['taskRevision'] or digest != report['inputsDigest']:
        raise RuntimeError('RESHOOT_INPUTS_CHANGED: 任务、素材或观察已变化，请重新核验缺口')
    return report


def _response(row, report):
    return {'revisionId': row['revision_id'], 'oldGapId': row['old_gap_id'],
            'gapId': row['new_gap_id'], 'status': 'revised', 'report': report}


def revise_reshoot_need(db, task_id, *, expected_revision, report_id, gap_id, query, reason,
                        project_root=None):
    for value in (expected_revision, report_id, gap_id):
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError('RESHOOT_REVISION_INPUT: 缺少当前任务、清单或需求版本')
    if (not isinstance(query, str) or not isinstance(reason, str)
            or not 1 <= len(query.strip()) <= 500 or not 1 <= len(reason.strip()) <= 500):
        raise ValueError('RESHOOT_REVISION_INPUT: 需求和修改原因均须为1至500字')
    query, reason = query.strip(), reason.strip()
    revision_id = 'gap-revision-' + _digest([task_id, expected_revision, report_id, gap_id, query, reason])[:32]
    with db.transaction(immediate=True) as connection:
        task, _, cards, projection, _ = _context(db, task_id, expected_revision)
        existing = connection.execute('SELECT * FROM reshoot_requirement_revisions WHERE revision_id=? AND task_id=?', (revision_id, task_id)).fetchone()
        if existing:
            active = connection.execute('SELECT * FROM reshoot_requirements WHERE gap_id=? AND task_id=?', (existing['new_gap_id'], task_id)).fetchone()
            if active is None or not active['active'] or active['query'] != query or not existing['result_report_id']:
                raise RuntimeError('RESHOOT_REVISION_STALE: 这次修改已成为历史，不会复活旧需求')
            report = _current_report(db, task_id, existing['result_report_id'], expected_revision)
            return _response(existing, report)
        report = _current_report(db, task_id, report_id, expected_revision)
        old = connection.execute('SELECT * FROM reshoot_requirements WHERE task_id=? AND gap_id=?', (task_id, gap_id)).fetchone()
        if old is None:
            raise KeyError(gap_id)
        old = dict(old)
        item = next((i for i in report['items'] if i['gapId'] == gap_id), None)
        if not old['active'] or item is None:
            raise RuntimeError('RESHOOT_REVISION_STALE: 需求已失效或不属于当前清单')
        if (old['script_revision'] != projection['scriptRevision']
                or not revision_fields(db, old, cards)['canRevise']):
            raise ValueError('RESHOOT_REVISION_NOT_ALLOWED: 仅能修改当前建议或用户修订的需求，不能修改自动生成需求')
        if query == old['query']:
            raise ValueError('RESHOOT_REVISION_UNCHANGED: 新需求与原需求相同')
        new_gap = 'gap-' + _digest([task_id, revision_id])[:24]
        connection.execute('UPDATE reshoot_requirements SET active=0 WHERE task_id=? AND gap_id=? AND active=1', (task_id, gap_id))
        connection.execute('''INSERT INTO reshoot_requirements(
            gap_id,task_id,script_revision,script_segment_id,origin_key,card_id,query,media_kind,start_us,end_us,timing_basis,active)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,1)''',
            (new_gap, task_id, old['script_revision'], old['script_segment_id'], 'user-revise:'+revision_id,
             old['card_id'], query, old['media_kind'], item['startUs'], item['endUs'], old['timing_basis']))
        connection.execute('''INSERT INTO reshoot_requirement_revisions(
            revision_id,task_id,task_revision,source_report_id,source_inputs_digest,old_gap_id,new_gap_id,old_query,new_query,reason)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (revision_id, task_id, expected_revision, report_id, report['inputsDigest'], gap_id, new_gap, old['query'], query, reason))
        result = _build_reshoot_list_locked(db, task_id, expected_revision, project_root=project_root)
        # Database writers are excluded; recheck filesystem/context changes before final acceptance.
        revision, digest, _ = _evaluate(db, task_id, expected_revision)
        if revision != result['taskRevision'] or digest != result['inputsDigest']:
            raise RuntimeError('RESHOOT_INPUTS_CHANGED: 修改期间依据发生变化，请刷新后重试')
        connection.execute('UPDATE reshoot_requirement_revisions SET result_report_id=? WHERE revision_id=?', (result['reportId'], revision_id))
        row = connection.execute('SELECT * FROM reshoot_requirement_revisions WHERE revision_id=?', (revision_id,)).fetchone()
        return _response(row, result)