"""Isolated, free rea-video analysis for the reference-video workbench."""
import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import jsonify, request

LOCK = threading.Lock()
ACTIVE = set()
WORKER_SLOT = threading.Semaphore(1)


def summarize(data, review=None):
    rows = []
    for shot in data.get('shots', []):
        base = shot.get('base') or {}
        label = base.get('label', 'unknown')
        uncertain = base.get('uncertain')
        routes = []
        if uncertain:
            routes.append('主体素材待复核后决定 AI 生成或实拍')
        elif label in {'ai_video', 'avatar'}:
            routes.append('AI 生成主体画面，剪辑组装')
        elif label in {'live_action', 'film_footage'}:
            routes.append('准备实拍素材，剪辑组装')
        elif label in {'still_motion', 'static_hold', 'screen_capture'}:
            routes.append('剪辑关键帧、录屏或静图')
        elif label in {'cg_3d'}:
            routes.append('3D 制作主体，AE 合成，剪辑组装')
        elif label in {'code_render', 'anim_2d'}:
            routes.append('AE 或代码制作图形与分层动画')
        else:
            routes.append('主体素材制作方式待复核')
        layers = shot.get('layers') or []
        if layers:
            routes.append('字幕与简单包装用剪辑，精确跟踪和遮罩可用 AE')
        rows.append({'id': shot['id'], 'start': shot['start'], 'end': shot['end'],
                     'label': base.get('label_zh') or label,
                     'confidence': base.get('confidence'), 'needs_review': bool(uncertain) or not shot.get('vlm'),
                     'evidence': base.get('evidence') or [], 'uncertainty': uncertain,
                     'recommendations': routes})
    reviewed = (review or {}).get('shots', {})
    for row in rows:
        item = reviewed.get(row['id'])
        if not isinstance(item, dict):continue
        row['tool_label'] = row['label']
        row['label'] = str(item.get('assessment') or row['label'])
        row['evidence'] = list(item.get('visual_evidence') or []) + row['evidence']
        row['recommendations'] = item.get('recommendations') or row['recommendations']
        row['needs_review'] = True
    return {'status': 'completed', 'shots': rows, 'vlm': False,
            'note': (review or {}).get('basis') or '离线初判，需结合画面复核。AE 为复刻建议，原片所用软件待确认。'}


def register_production_methods(app, root, allowed_origins=()):
    root = Path(root)
    output = root / 'work' / 'method-analysis'
    default_cli = root / '.rea-venv' / ('Scripts/rea-video.exe' if os.name == 'nt' else 'bin/rea-video')
    cli = Path(os.environ.get('SUOJIAN_REA_CLI', str(default_cli)))

    def paths(job):
        if not re.fullmatch(r'[0-9a-f]{8}', job):
            raise ValueError('无效任务编号')
        videos = sorted((root / 'uploads').glob(job + '_*'))
        video = next((p for p in videos if p.is_file()), None)
        if not video:
            raise ValueError('请先完成视频导入')
        return video, output / job

    def read_state(job, folder):
        result = folder / 'breakdown.json'
        if result.is_file() and job not in ACTIVE:
            try:
                data = json.loads(result.read_text('utf-8'))
                if data.get('shots') and data.get('summary'):
                    review_path = folder / 'review.json'
                    review = json.loads(review_path.read_text('utf-8')) if review_path.is_file() else None
                    return summarize(data, review)
            except (ValueError, KeyError, TypeError):
                pass
        if job in ACTIVE:
            return {'status': 'running', 'stage': '逐镜分析制作方式'}
        state = folder / 'method-status.json'
        if state.is_file():
            try:
                data = json.loads(state.read_text('utf-8'))
            except (ValueError, OSError):
                return {'status': 'failed', 'error': '制作方式记录无法读取，可重新分析'}
            if data.get('status') == 'running':
                return {'status': 'interrupted', 'error': '制作方式分析已中断，可重新分析'}
            return data
        return {'status': 'idle'}

    def save_state(folder, data):
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / ('status-' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(folder / 'method-status.json')

    @app.route('/api/analyze/<job>/methods', methods=['GET', 'POST'])
    def methods(job):
        origin = request.headers.get('Origin')
        trusted_origin = origin in allowed_origins
        if request.host.split(':')[0] not in {'localhost', '127.0.0.1'} or (request.headers.get('Sec-Fetch-Site') == 'cross-site' and not trusted_origin):
            return jsonify(error='请从本机工作台读取'), 403
        if origin and urlsplit(origin).netloc != request.host and not trusted_origin:
            return jsonify(error='请从本机工作台读取'), 403
        try:
            video, folder = paths(job)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        with LOCK:
            state = read_state(job, folder)
            if request.method == 'GET' or state['status'] in {'running', 'completed'}:
                return jsonify(state)
            if not cli.is_file():
                return jsonify(status='unavailable', error='rea-video 独立环境不可用'), 503
            ACTIVE.add(job)
            save_state(folder, {'status': 'running'})

        def run():
            try:
                env = dict(os.environ, PYTHONUTF8='1',
                           REA_VIDEO_HOME=str(output / 'preferences'),
                           REA_VIDEO_MODELS=os.environ.get('REA_VIDEO_MODELS', str(root / 'model_cache' / 'rea-video')),
                           HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9')
                with WORKER_SLOT, (folder / 'analysis.log').open('w', encoding='utf-8') as log:
                    subprocess.run([str(cli), 'decompose', str(video), '-o', str(folder),
                                    '--no-vlm', '--no-stems', '--no-ocr', '--no-packaging',
                                    '--workers', '1', '--lang', 'zh'], env=env,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=1800, check=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                data = json.loads((folder / 'breakdown.json').read_text('utf-8'))
                if not data.get('shots') or not data.get('summary'):
                    raise ValueError('制作方式分析结果不完整')
                save_state(folder, {'status': 'completed'})
            except Exception:
                save_state(folder, {'status': 'failed', 'error': '制作方式分析未完成，请重试；详细原因已保存在分析日志中。'})
            finally:
                with LOCK:
                    ACTIVE.discard(job)
        threading.Thread(target=run, daemon=True).start()
        return jsonify(status='running', stage='逐镜分析制作方式'), 202

    if allowed_origins:
        @app.after_request
        def local_workbench_cors(response):
            origin = request.headers.get('Origin')
            if origin in allowed_origins:
                response.headers['Access-Control-Allow-Origin'] = origin
                response.headers['Vary'] = 'Origin'
            return response


if __name__ == '__main__':
    from flask import Flask
    service = Flask(__name__)
    register_production_methods(service, Path(__file__).resolve().parents[2],
                                ('http://127.0.0.1:8788', 'http://localhost:8788'))
    service.run(host='127.0.0.1', port=8790, debug=False, use_reloader=False)
