"""Read-only home index for the three local production workspaces."""
import json
import re
from pathlib import Path
from flask import jsonify, request


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def project_index(results):
    rows = []
    if not results.is_dir():
        return rows
    for folder in results.iterdir():
        if not folder.is_dir() or not re.fullmatch('[0-9a-f]{8}', folder.name):
            continue
        job = folder.name
        analysis = read_object(folder / 'analysis_status.json')
        blueprint = read_object(folder / 'edit_blueprint.json')
        if not analysis and not blueprint:
            continue
        source = blueprint.get('source') if isinstance(blueprint.get('source'), dict) else {}
        title = str(source.get('file_name') or '参考视频 · ' + job).replace('\\', '/').rsplit('/', 1)[-1]
        if title.startswith(job + '_'):
            title = title[9:]
        def entry(mode, state, label, href, paths, display_title=None):
            stamp = max((p.stat().st_mtime for p in paths if p.is_file()), default=folder.stat().st_mtime)
            rows.append(dict(id=f'{mode}-{job}', job=job, mode=mode, title=display_title or title,
                             status=state, status_label=label, href=href, updated_at=stamp))
        execution = read_object(folder / 'execution.json')
        state = execution.get('status') or analysis.get('status', 'idle')
        labels = {'exported': '草稿已保存', 'ready': '预览待审看', 'qa_blocked': '有质检提示',
                  'failed': '需要处理', 'running': '处理中', 'waiting_worker': '排队中',
                  'needs_check': '需要核对', 'interrupted': '需要继续'}
        presenter = folder / 'presenter'
        motion = folder / 'motion_explainer'
        has_presenter = any((presenter / name).is_file() for name in ('last-render.json', 'plan.json', 'library.json'))
        if execution or not has_presenter and not (motion / 'project.json').is_file():
            entry('feed', state, labels.get(state, '参考片已解析' if blueprint else '等待解析'),
                  f'/?job={job}', [folder / 'analysis_status.json', folder / 'execution.json'])
        manifest = read_object(presenter / 'last-render.json')
        pstate = read_object(presenter / 'state.json')
        if manifest or (presenter / 'plan.json').is_file() or (presenter / 'library.json').is_file():
            render_id = manifest.get('id', '')
            # Only inspect a known manifest location; never follow arbitrary paths.
            draft = read_object(presenter / render_id / 'draft.json') if re.fullmatch('render-[0-9a-f]{10}', render_id) else {}
            registered = bool(draft.get('registration', {}).get('registered'))
            label = '草稿已保存' if registered else '预览待审看' if manifest else '素材已准备'
            if pstate.get('status') == 'failed': label = '需要处理'
            entry('presenter', pstate.get('status', 'idle'), label, f'/presenter?job={job}',
                  [presenter / 'last-render.json', presenter / 'state.json', presenter / 'plan.json'],
                  draft.get('display_name'))
        if (motion / 'project.json').is_file():
            mstate = read_object(motion / 'state.json')
            state = mstate.get('status', 'idle')
            entry('vibe', state, {'completed': '视频已导出', 'failed': '需要处理', 'rendering': '正在生成'}.get(state, '继续创作'),
                  f'/motion?job={job}', [motion / 'state.json', motion / 'project.json', motion / 'music.json'])
    return sorted(rows, key=lambda row: row['updated_at'], reverse=True)[:120]


def register_studio_routes(app, workspace):
    @app.get('/api/studio/projects')
    def studio_projects():
        if request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
            return jsonify(error='仅限本机工作台'), 403
        from .park_vibe import film_projects
        projects = project_index(Path(workspace) / 'results') + film_projects(Path(workspace))
        root = Path(workspace) / 'work' / 'presenter-workspaces'
        if root.is_dir():
            for folder in root.iterdir():
                if not folder.is_dir() or not re.fullmatch(r'p-[0-9a-f]{12}', folder.name):
                    continue
                library = read_object(folder / 'library.json')
                if not library:
                    continue
                state = read_object(folder / 'state.json')
                name = str(library.get('folder', '')).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
                projects.append(dict(id='presenter-'+folder.name, job=folder.name, mode='presenter',
                    title=name or '口播选句', status=state.get('status', 'idle'),
                    status_label='口播素材已扫描', href='/presenter?job='+folder.name,
                    updated_at=max((p.stat().st_mtime for p in (folder/'state.json',folder/'plan.json',folder/'library.json') if p.is_file()),default=folder.stat().st_mtime)))
        return jsonify(projects=sorted(projects, key=lambda row: row['updated_at'], reverse=True))
