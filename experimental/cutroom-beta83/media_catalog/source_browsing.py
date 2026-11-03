"""Cheap page access projection. Never a source authorization or content proof."""
from .initial_source_check import get_initial_source_check
from .task_sources import (_require_task, _preview_scope_rows, _preview_health,
                           _scope_root, filesystem_path)


def source_browsing_status(db, task_id: str) -> dict:
    _require_task(db, task_id)
    scopes = []
    for row in _preview_scope_rows(db, task_id):
        item = dict(row)
        try:
            item['online'] = bool(item['online']) and filesystem_path(
                _scope_root(item['mount_path'], item['relative_path'])).is_dir()
        except (OSError, ValueError):
            item['online'] = False
        scopes.append(item)
    health = _preview_health([s for s in scopes if not s['pending_removal']])
    initial = get_initial_source_check(db, task_id)
    setting = db.connection.execute('SELECT revision FROM task_source_settings WHERE task_id=?',
                                    (task_id,)).fetchone()
    current = bool(initial and setting and initial['source_revision'] == setting['revision'])
    status, reason, can_browse = 'available', 'last_check_only', True
    message = '可查看已登记素材；修改、确认和交付前仍会重新核验源文件。'
    if health['status'] == 'blocking':
        status, reason, can_browse = 'blocked', 'primary_offline', False
        message = '主素材目录当前不可访问，请先恢复连接。'
    elif initial and initial['status'] in {'queued', 'running'}:
        status, reason, can_browse = 'checking', 'initial_check_pending', False
        message = '创建时的素材检查尚未完成。'
    elif initial and initial['status'] == 'failed':
        status, reason, can_browse = 'error', 'initial_check_failed', False
        message = '创建时的素材检查失败，请在后台作业中查看原因并明确恢复；刷新不会重跑检查。'
    elif initial and initial['status'] == 'offline':
        status, reason = 'unverified', 'reconnected_unverified'
        message = '目录已可访问；上次检查发现离线，实际操作前仍须重新核验。'
    elif initial and (not current or initial['status'] == 'review_required'):
        status, reason = 'unverified', 'source_state_changed'
        message = '素材范围或检查状态已有变化；可查看现有分镜，实际操作前须核对变化。'
    elif not initial:
        status, reason = 'unverified', 'no_initial_receipt'
        message = '可查看已登记素材；没有创建初检回执，实际操作前须核验当前源文件。'
    return {'taskId': task_id, 'browseOnly': True, 'canBrowse': can_browse,
            'status': status, 'reason': reason, 'message': message, 'health': health,
            'checkedAt': initial['checked_at'] if initial else None,
            'initialCheckCurrent': current, 'sourceRevision': setting['revision'] if setting else None,
            'initialCheck': None if initial is None else {
                'jobId': initial['job_id'], 'status': initial['status'],
                'checkedAt': initial['checked_at'], 'summary': initial['summary']}}