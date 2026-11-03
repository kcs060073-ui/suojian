"""SuoJian-owned local camera analysis API."""
from urllib.parse import urlsplit

from flask import jsonify, request

from suo_jian.analyzer.motion_planning import scan_folder, plan_motion_shots


def register_native_motion(app):
    @app.post('/api/motion/plan')
    def native_motion_plan():
        if request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
            return jsonify(error='仅限本机工作台'), 403
        origin = request.headers.get('Origin')
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify(error='请求来源不匹配'), 403
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get('folder'), str) or not data['folder'].strip():
            return jsonify(error='请提供素材文件夹'), 400
        try:
            index = scan_folder(data['folder'], window_seconds=float(data.get('window_seconds', 4)))
            plan = None
            if data.get('shots') is not None:
                if not isinstance(data['shots'], list):
                    raise ValueError('参考镜头必须为列表')
                plan = plan_motion_shots(data['shots'], index, ratio=data.get('ratio', '9:16'))
            return jsonify(index=index, plan=plan, engine='suojian-local', requires_cutroom=False)
        except (ValueError, KeyError, TypeError) as error:
            return jsonify(error=str(error)), 400
