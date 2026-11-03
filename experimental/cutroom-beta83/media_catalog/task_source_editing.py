"""Explicit task-local changes to registered material directories."""
import json
from pathlib import Path
from .task_sources import get_task_sources, set_task_sources
from .tasks import get_task

def _revision(db, task_id):
    row = db.connection.execute("SELECT revision FROM task_source_settings WHERE task_id=?", (task_id,)).fetchone()
    return int(row[0]) if row else 0

def _edit_block(db, task_id):
    task = get_task(db, task_id)
    if db.connection.execute("SELECT 1 FROM jobs WHERE task_id=? AND status IN ('queued','running','paused') LIMIT 1", (task_id,)).fetchone():
        return 'SOURCE_CONFIGURATION_BUSY', '本任务正在处理，请等待完成或取消处理后再修改素材目录。'
    if task['status'] not in ('draft','brief_locked'):
        return 'SOURCE_CONFIGURATION_LOCKED', '本任务已锁定剪辑结果，请先返回粗剪调整并解锁，再修改素材目录。'
    return '', ''

def source_configuration(db, task_id):
    code, reason = _edit_block(db, task_id)
    libraries = [dict(row) for row in db.connection.execute("SELECT library_id,name,mount_path,purpose,online FROM libraries WHERE kind='local' AND purpose IN ('task_specific','generic') ORDER BY name,library_id")]
    for library in libraries:
        library['online'] = bool(library['online']) and Path(library['mount_path']).is_dir()
    return {'revision': _revision(db, task_id), 'sources': get_task_sources(db, task_id),
            'canEdit': not code, 'reason': reason, 'libraries': libraries}

def change_source_configuration(db, task_id, *, primary_library_id, selected_generic_ids, expected_revision):
    if not isinstance(primary_library_id, str) or not primary_library_id or not isinstance(selected_generic_ids, list) or any(not isinstance(x,str) or not x for x in selected_generic_ids) or type(expected_revision) is not int:
        raise ValueError('SOURCE_DIRECTORY_INVALID: 请选择已登记的素材目录。')
    with db.transaction(immediate=True) as connection:
        code, reason = _edit_block(db, task_id)
        if code: raise ValueError(code + ': ' + reason)
        if _revision(db, task_id) != expected_revision:
            raise ValueError('SOURCE_CONFIGURATION_STALE: 素材目录设置已变化，请刷新后重新选择。')
        config = source_configuration(db, task_id)
        libraries = {x['library_id']:x for x in config['libraries']}
        for library_id in [primary_library_id] + selected_generic_ids:
            library = libraries.get(library_id)
            purpose = 'task_specific' if library_id == primary_library_id else 'generic'
            if not library or not library['online'] or library['purpose'] != purpose or (library_id == primary_library_id and library_id in selected_generic_ids):
                raise ValueError('SOURCE_DIRECTORY_INVALID: 所选本地目录未登记、不可访问或用途不符，请到镜头素材中检查。')
        # The frozen brief is part of human script confirmation provenance.
        # Task source scopes are authoritative; changing folders must not revoke it.
        try:
            set_task_sources(db, task_id, primary_library_id=primary_library_id,
                selected_generic_ids=selected_generic_ids, include_default_generic=False)
        except ValueError as error:
            if '任务现有素材引用不在拟议素材范围内' in str(error):
                raise ValueError('SOURCE_REFERENCES_OUTSIDE: 已选镜头仍在使用要移除的目录，请先替换这些镜头，或保留该目录。') from error
            raise
    return source_configuration(db, task_id)
