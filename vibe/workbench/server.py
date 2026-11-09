"""Independent loopback workbench. Never modifies the shared SuoJian service."""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
# 外部依赖路径统一来自 workbench/paths.py，可用环境变量覆盖（见该模块说明）
from workbench.paths import IMPORT_DIR as IMPORT, NODE, PYTHON, VENDOR
PROJECT = ROOT / 'work/park-fusion'
OUTPUTS = ROOT / 'outputs'
CONFIG = ROOT / 'work/workbench-config.json'
BASELINE = ROOT / 'outputs/园区Vibe-最终选定-写实设备与半身讲解员.mp4'
JOBS: dict[str, dict] = {}
LOCK = threading.RLock()
MUTATION = threading.Lock()
LIBRARY = None
DIRECTOR = None
NATIVE_PROCESSES = {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex[:8] + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def default_config():
    source = ROOT / 'engine/default-config.json'
    if source.exists():
        return json.loads(source.read_text(encoding='utf-8-sig'))
    return {'version': 1, 'name': '园区 · 嘴鼻固定基线', 'format': 'portrait',
            'style': 'fusion', 'background': 'footage', 'narrationMode': 'existing',
            'musicGain': 1, 'voiceGain': 1, 'sfxEnabled': True,
            'camera': 'static', 'typography': 'minimal', 'seed': 42,
            'presenter': {'enabled': True, 'fixedHead': True, 'fixedMouth': True, 'grounded': True},
            'shots': [], 'subtitles': [], 'chapters': [], 'simulation': {'enabled': False, 'equipmentHeight': 4, 'entranceHeight': 3}}


def load_config():
    value=json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.exists() else default_config()
    return importlib.import_module('workbench.card_layers').migrate(value,ROOT)


def save_config(config):
    """Persist the current project and its per-film editable source together."""
    if CONFIG.exists():atomic_json(ROOT/'work/config-history'/f'{time.time_ns()}.json',load_config())
    importlib.import_module('workbench.films').save_editor(ROOT,config)
    atomic_json(CONFIG,config)


def director():
    global DIRECTOR
    with LOCK:
        if DIRECTOR is None:
            def save_directed(c):
                save_config(c)
            DIRECTOR=importlib.import_module('workbench.director').Director(ROOT,load_config,save_directed,validate_config,LOCK)
    return DIRECTOR


def validate_config(c):
    if not isinstance(c, dict):
        raise ValueError('方案必须是 JSON 对象')
    if c.get('format', 'portrait') not in ('portrait', 'landscape'):
        raise ValueError('画幅不支持')
    if c.get('style', 'fusion') not in ('fusion', 'night', 'fable', 'explainer', 'ink'):
        raise ValueError('未知画面风格')
    if c.get('subtitleMode', 'single') not in ('single', 'off'):
        raise ValueError('此工具台仅支持单语字幕或关闭字幕')
    for key in ('musicGain', 'voiceGain'):
        n = c.get(key, 1)
        if type(n) not in (int, float) or not 0 <= n <= 2:
            raise ValueError(f'{key} 范围为 0–2')
    if len(c.get('shots', [])) > 200 or len(c.get('subtitles', [])) > 2000:
        raise ValueError('单个工程超出镜头或字幕上限')
    for shot in c.get('shots', []):
        if not isinstance(shot, dict) or type(shot.get('duration')) not in (int, float) or not 1 <= shot['duration'] <= 18000:
            raise ValueError('镜头时长必须为 1–18000 帧')
        if not .1 <= float(shot.get('speed', 1)) <= 4:
            raise ValueError('镜头速度范围为 0.1–4')
    c = copy.deepcopy(c)
    c['subtitleMode'] = c.get('subtitleMode', 'single')
    # Preserve the latest explicit selection; older fixed-mouth variant stays selectable.
    c['presenter'] = {**c.get('presenter', {})}
    for key,lo,hi in (('backgroundBlur',4,60),('backgroundBrightness',.2,1)):
        if key in c and (type(c[key]) not in (int,float) or not lo<=c[key]<=hi):raise ValueError('背景参数超出范围：'+key)
    for shot in c.get('shots',[]):
        if 'cardEffect' in shot and shot['cardEffect'] not in importlib.import_module('workbench.card_layers').EFFECTS:raise ValueError('此卡片动效尚未接入实时分层')
        for key,lo,hi in (('cardEnterFrames',6,120),('cardIntensity',0,1.5)):
            if key in shot and (type(shot[key]) not in (int,float) or not lo<=shot[key]<=hi):raise ValueError('卡片动效参数超出范围：'+key)
    return importlib.import_module('workbench.card_layers').migrate(c,ROOT)


def library_data():
    global LIBRARY
    if LIBRARY is None:
        lib = importlib.import_module('workbench.library')
        LIBRARY = lib.discover(VENDOR)
    return LIBRARY


def context(out):
    return dict(root=ROOT, workspace=ROOT / 'work', output=out, project=PROJECT,
                vendor=VENDOR, python=str(PYTHON if PYTHON.exists() else sys.executable),
                node=NODE, ffmpeg=shutil.which('ffmpeg') or 'ffmpeg', baseline=BASELINE)


def media_path(value):
    if not value:
        return value
    if value == '__baseline__':
        return str(BASELINE)
    for prefix, root in [('/media/', PROJECT/'public'), ('/vendor/', VENDOR), ('/files/', ROOT)]:
        if str(value).startswith(prefix):
            target = (root / unquote(str(value)[len(prefix):])).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError('素材路径越界')
            return str(target)
    return value


def engine_manifest(config, out):
    atomic_json(out/'config.json', config)
    subprocess.run([NODE, str(ROOT/'engine/bridge.cjs'), 'manifest', '--config', str(out/'config.json'), '--output', str(out/'timeline-manifest.json')], cwd=ROOT, check=True, capture_output=True, timeout=30)
    return json.loads((out/'timeline-manifest.json').read_text(encoding='utf-8'))


def run_engine(action, config, out, extra=()):
    config_file = out / 'config.json'
    atomic_json(config_file, config)
    target = out / ('plate.mp4' if config.get('plate') else 'preview.mp4') if action == 'render' else out / 'still.png'
    command = [NODE, str(ROOT / 'engine/bridge.cjs'), action, '--config', str(config_file), '--output', str(target), *extra]
    proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = []
    for line in proc.stdout:
        lines.append(line.rstrip())
        (out / 'engine.log').write_text('\n'.join(lines[-300:]), encoding='utf-8')
    code = proc.wait()
    if code:
        raise RuntimeError('渲染失败，参见日志：' + '\n'.join(lines[-6:]))
    if not target.exists():
        raise RuntimeError('引擎未生成预期文件，请查看 engine.log')
    return target


def tool_worker(job_id, action, payload, config):
    out = OUTPUTS / 'runs' / job_id
    out.mkdir(parents=True, exist_ok=True)
    job = JOBS[job_id]
    try:
        MUTATION.acquire()
        try:
            job.update(status='running', phase='正在处理', started=time.time())
            atomic_json(out / 'config.json', config)
            if action in ('render', 'still', 'pilot'):
                job['phase'] = 'Remotion 正在生成独立版本'
                args = ['--frame', str(int(payload.get('frame', 100)))] if action == 'still' else []
                if action == 'pilot':
                    args = ['--from', str(int(payload.get('from', 0))), '--to', str(int(payload.get('to', 149)))]
                file = run_engine('still' if action == 'still' else 'render', config, out, args)
                from workbench.production_trace import observe_renderer_return
                job['renderObservation'] = observe_renderer_return(ROOT, out, action, config, file)
                result = {'status': 'completed', 'artifacts': [str(file)], 'warnings': []}
                if action=='render':
                    result['filmId']=importlib.import_module('workbench.films').register_film(ROOT,file,config)
            elif action == 'export_draft':
                job['phase'] = '先生成无字幕与音频的底片，再建剪映轨道'
                plate_config = {**config, 'plate': True}
                plate = run_engine('render', plate_config, out)
                from workbench.production_trace import observe_renderer_return
                job['renderObservation'] = observe_renderer_return(ROOT, out, action, plate_config, plate)
                manifest = engine_manifest(config, out)
                params = {**payload, 'clean_plate': str(plate), 'video': str(plate),
                          'subtitles': manifest.get('subtitles', []),
                          'shots': [{'from':s['from'], 'to':s['from']+s['duration']-1} for s in manifest.get('shots', config.get('shots', [])) if s.get('enabled') is not False],
                          'width': 1080 if config.get('format') == 'portrait' else 1920,
                          'height': 1920 if config.get('format') == 'portrait' else 1080,
                          'fps': 30, 'config': config}
                for track in ('voice','bgm','sfx','reference'):
                    entries = []
                    for event in manifest.get('audioEvents', []):
                        if ('bgm' if event.get('track') == 'music' else event.get('track')) != track:
                            continue
                        e = {'path':media_path(event['source']), 'from':event['from'], 'to':event['from']+event['duration']-1,
                             'sourceFrom':event.get('sourceFrom',0), 'sourceDurationFrames':event.get('sourceDuration',event['duration']),
                             'speed':event.get('playbackRate',1), 'volume':event.get('gain',1), 'loop':event.get('loop',False),'mutedAlternative':event.get('mutedAlternative',False)}
                        if event.get('envelope'):
                            e['envelope'] = event['envelope']
                        entries.append(e)
                    params[track] = entries
                result = importlib.import_module('workbench.tasks').run_tool(action, params, context(out))
            elif action == 'publish':
                normalized_payload={**payload,'video':media_path(payload.get('video'))}
                result=importlib.import_module('workbench.publishing').publish(normalized_payload,context(out))
            elif action == 'demo':
                result = importlib.import_module('workbench.library').run_demo(payload['id'], VENDOR, ROOT, out/'shotcard.mp4', style_id=payload.get('style'))
                media_copy = PROJECT/'public/generated-cards'/(job_id+'.mp4')
                media_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(out/'shotcard.mp4', media_copy)
                result = {'status':'completed', 'artifacts':[str(out/'shotcard.mp4')], 'receipt':result, 'previewAsset':'/media/generated-cards/'+media_copy.name, 'warnings':[]}
            elif action == 'native':
                result = importlib.import_module('workbench.library').run_native(payload.get('id'), payload.get('operation'), payload, VENDOR, ROOT, out)
            else:
                normalized_payload = {**payload, 'config':config}
                for key in ('path','video','clean_plate','audio','model','voices'):
                    if key in normalized_payload:
                        normalized_payload[key] = media_path(normalized_payload[key])
                result = importlib.import_module('workbench.tasks').run_tool(action, normalized_payload, context(out))
            result = json.loads(json.dumps(result, default=str, ensure_ascii=False))
            job.update(status=result.get('status', 'completed'), phase='处理完成', result=result, finished=time.time())
        finally:
            MUTATION.release()
    except Exception as error:
        job.update(status='failed', phase='任务未完成', error=str(error), finished=time.time())
        (out / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
    from workbench.production_trace import safe_record, trace_vibe_run
    job['productionTrace'] = safe_record(ROOT, trace_vibe_run, job_id, action, config,
        job.get('result'), job.get('error'), scope={k:payload[k] for k in ('from','to','frame') if k in payload},
        observation=job.get('renderObservation'), tool={k:payload[k] for k in ('id','operation','style') if k in payload})
    atomic_json(out / 'job.json', job)


def artifact_links(value):
    if isinstance(value, dict):
        result = {k: v if k=='path' else artifact_links(v) for k, v in value.items()}
        if isinstance(value.get('path'), str):
            p = Path(value['path'])
            if p.is_absolute() and p.is_relative_to(ROOT) and p.exists():
                result.update(url='/files/'+p.relative_to(ROOT).as_posix(), name=value.get('name') or p.name)
        return result
    if isinstance(value, list):
        return [artifact_links(v) for v in value]
    if isinstance(value, str):
        try:
            p = Path(value)
            if p.is_absolute() and p.is_relative_to(ROOT) and p.exists():
                return {'path': str(p), 'url': '/files/' + p.relative_to(ROOT).as_posix(), 'name': p.name}
        except (ValueError, OSError):
            pass
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = 'ParkVibe/1.0'

    def json_response(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def safe_path(self, root, part):
        root = Path(root).resolve()
        p = (root / unquote(part)).resolve()
        if not p.is_relative_to(root):
            raise ValueError('文件路径越界')
        return p

    def serve_file(self, path, head=False):
        resolved=path.resolve()
        if resolved.is_relative_to((ROOT/'work/secrets').resolve()) or resolved.is_relative_to(importlib.import_module('workbench.production').personal_root(ROOT).resolve()) or any(p.startswith('.env') for p in resolved.parts):
            return self.send_error(403)
        if not path.is_file():
            return self.send_error(404)
        size = path.stat().st_size
        start, end = 0, max(0, size-1)
        rng = self.headers.get('Range')
        code = 200
        if rng:
            m = re.fullmatch(r'bytes=(\d*)-(\d*)', rng)
            if not m or (not m[1] and not m[2]):
                return self.send_error(416)
            if not m[1]:
                start = max(0, size-int(m[2]))
            else:
                start, end = int(m[1]), min(int(m[2]) if m[2] else size-1, size-1)
            if start >= size or end < start:
                return self.send_error(416)
            code = 206
        self.send_response(code)
        self.send_header('Content-Type', mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(max(0, end-start+1)))
        self.send_header('Cache-Control', 'no-cache')
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if head:
            return
        with path.open('rb') as f:
            f.seek(start)
            remaining = end-start+1
            while remaining > 0:
                b = f.read(min(1024*256, remaining))
                if not b:
                    break
                self.wfile.write(b)
                remaining -= len(b)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        try:
            path = urlsplit(self.path).path
            if path == '/api/production/templates':
                return self.json_response(importlib.import_module('workbench.production').templates(ROOT))
            if path == '/api/production/visual-styles/status':
                return self.json_response({'library':True,'version':2})
            match = re.fullmatch(r'/api/production/visual-styles/(style-[0-9a-f]{12})/export', path)
            if match:
                content=importlib.import_module('workbench.style_transfer').export_style(ROOT,match[1])
                self.send_response(200)
                self.send_header('Content-Type','application/zip')
                self.send_header('Content-Disposition','attachment; filename="suojian-style.zip"')
                self.send_header('Content-Length',str(len(content)))
                self.end_headers()
                if not head: self.wfile.write(content)
                return
            if path == '/api/production/visual-styles/archived':
                mod=importlib.import_module('workbench.visual_styles');lib=mod.StyleLibrary(ROOT)
                return self.json_response(([mod.default_style(ROOT)] if lib.builtin_archived() else [])+lib.list(archived=True))
            if path == '/api/production/visual-styles':
                mod=importlib.import_module('workbench.visual_styles')
                lib=mod.StyleLibrary(ROOT)
                active=parse_qs(urlsplit(self.path).query).get('active',['0'])[0]=='1'
                return self.json_response(([mod.default_style(ROOT)] if not active or not lib.builtin_archived() else [])+lib.list())
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/video-runs', path)
            if match:
                return self.json_response(importlib.import_module('workbench.storyboard_video').Videos(ROOT).list(match[1]))
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/video-runs/([a-f0-9]{32})', path)
            if match:
                return self.json_response(importlib.import_module('workbench.storyboard_video').Videos(ROOT).get(match[1],match[2]))
            if path == '/api/production/tasks':
                return self.json_response(importlib.import_module('workbench.production').Store(ROOT).list())
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/storyboard', path)
            if match:
                return self.json_response(importlib.import_module('workbench.vibe_storyboard').Storyboards(ROOT).get(match[1]))
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/storyboard/export/(overview|zip)', path)
            if match:
                data,ctype,name=importlib.import_module('workbench.storyboard_export').export_storyboard(ROOT,match[1],match[2])
                self.send_response(200); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data)))
                self.send_header('Content-Disposition',f'attachment; filename="{name}"'); self.send_header('Cache-Control','no-store'); self.end_headers()
                if not head:self.wfile.write(data)
                return
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/storyboard/prompt/([A-Za-z0-9_-]{1,48})', path)
            if match:
                mod=importlib.import_module('workbench.vibe_storyboard'); state=mod.Storyboards(ROOT).get(match[1])
                if not state: raise ValueError('请先保存分镜文字稿')
                row=next((s for s in state['shots'] if s['id']==match[2]),None)
                if not row: raise ValueError('镜头不存在')
                task=mod.Storyboards(ROOT).store.get(match[1]);style=task['input'].get('visualStyle',{})
                art=state['artDirection']
                if style.get('description'): art=style['description']+'\n'+art
                return self.json_response({'shotId':row['id'],'prompt':mod.image_prompt(art,row),'source':'已保存的风格与分镜文字，非自动图片分析'})
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/storyboard/image/([A-Za-z0-9_-]{1,48})/(\d+)', path)
            if match:
                mod=importlib.import_module('workbench.vibe_storyboard'); store=mod.Storyboards(ROOT); task=store.store.get(match[1]); state=store.load(task)
                row=next((s for s in (state or {}).get('shots',[]) if s['id']==match[2]),None)
                board=next((b for b in (row or {}).get('boards',[]) if b['version']==int(match[3])),None)
                if not board:raise ValueError('分镜图片不存在')
                target=Path(board['file']).resolve(); allowed=store.path(task).resolve()/'images'
                if not target.is_relative_to(allowed) or mod.digest_file(target)!=board['sha256']:raise ValueError('分镜图片已变化')
                return self.serve_file(target,head)
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/storyboard/clip/([A-Za-z0-9_-]{1,48})/(\d+)', path)
            if match:
                mod=importlib.import_module('workbench.vibe_storyboard'); store=mod.Storyboards(ROOT); task=store.store.get(match[1]); state=store.load(task)
                row=next((s for s in (state or {}).get('shots',[]) if s['id']==match[2]),None)
                clip=next((c for c in (row or {}).get('clips',[]) if c['version']==int(match[3])),None)
                if not clip:raise ValueError('镜头片段不存在')
                target=Path(clip['file']).resolve(); allowed=store.path(task).resolve()/'clips'
                if not target.is_relative_to(allowed) or mod.digest_file(target)!=clip['sha256']:raise ValueError('镜头片段已变化')
                return self.serve_file(target,head)
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})', path)
            if match:
                return self.json_response(importlib.import_module('workbench.production').Store(ROOT).get(match[1]))
            match = re.fullmatch(r'/production/posters/([A-Za-z0-9-]+)', path)
            if match:
                return self.serve_file(importlib.import_module('workbench.production').poster_path(ROOT, match[1]), head)
            if path == '/api/director/settings':
                return self.json_response(director().settings())
            if path == '/api/director/plans':
                return self.json_response(director().plans())
            if path == '/api/director/jobs':
                return self.json_response(director().job_list())
            if path == '/api/config':
                return self.json_response(load_config())
            if path == '/api/films/editor':
                return self.json_response(importlib.import_module('workbench.films').load_editor(ROOT,parse_qs(urlsplit(self.path).query).get('id',[''])[0]))
            if path == '/api/library':
                return self.json_response(library_data())
            if path == '/api/capabilities':
                mod = importlib.import_module('workbench.capabilities')
                if hasattr(mod, 'build_catalog'):
                    return self.json_response(mod.build_catalog(VENDOR))
                for name in ('catalog', 'get_capabilities', 'capabilities', 'build_catalog'):
                    fn = getattr(mod, name, None)
                    if callable(fn):
                        try:
                            data = fn(context(OUTPUTS))
                        except TypeError:
                            data = fn()
                        return self.json_response(data)
                return self.json_response(getattr(mod, 'CAPABILITIES', []))
            if path == '/api/jobs':
                registered=importlib.import_module('workbench.films').as_jobs(ROOT)
                known_paths={str(Path(p['path']).resolve()) for j in registered for p in j['result']['artifacts'] if p['path'].endswith('.mp4')}
                existing=[j for j in JOBS.values() if not (j.get('action')=='render' and any(str(Path(a.get('path','')).resolve()) in known_paths for a in artifact_links(j).get('result',{}).get('artifacts',[]) if isinstance(a,dict)))]
                return self.json_response(artifact_links(sorted(registered+existing,key=lambda j:j.get('created',0),reverse=True)))
            if path == '/api/films':
                return self.json_response(importlib.import_module('workbench.films').catalog(ROOT))
            if path == '/api/films/latest-completion':
                return self.json_response(importlib.import_module('workbench.films').latest_completion(ROOT))
            if path == '/api/health':
                return self.json_response({'ok': True, 'project': PROJECT.exists(), 'engine': (ROOT/'engine/player.js').exists(), 'baseline': BASELINE.exists(), 'deployment': 'suojian-park-vibe-v2', 'productionWorkflow': True, 'workflowVersion': 3, 'sharedServiceTouched': False})
            if path == '/api/native-tools':
                return self.json_response(importlib.import_module('workbench.library').native_tools(VENDOR, ROOT))
            if path == '/baseline.mp4':
                return self.serve_file(BASELINE, head)
            for prefix, root in [('/engine/', ROOT/'engine'), ('/media/', PROJECT/'public'), ('/vendor/', VENDOR), ('/native/canvas/', ROOT/'work/native/canvas'), ('/native/shotcraft/', ROOT/'work/native/shotcraft'), ('/files/', ROOT), ('/outputs/', OUTPUTS), ('/static/', ROOT/'workbench/static')]:
                if path.startswith(prefix):
                    if prefix == '/files/' and not path[len(prefix):].startswith(('outputs/', 'work/')):
                        return self.send_error(403)
                    return self.serve_file(self.safe_path(root, path[len(prefix):]), head)
            if path in ('/advanced', '/advanced/'):
                return self.serve_file(ROOT/'workbench/static/advanced.html', head)
            if path in ('/storyboard', '/storyboard/'):
                return self.serve_file(ROOT/'workbench/static/storyboard.html', head)
            return self.serve_file(ROOT/'workbench/static/studio.html', head) if path == '/' else self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.json_response({'error': str(error)}, 400)

    def read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 5*1024*1024:
            raise ValueError('请求过大')
        return json.loads(self.rfile.read(length) or b'{}')

    def allowed_mutation(self):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        return host.split(':')[0] in ('localhost', '127.0.0.1') and (not origin or urlsplit(origin).netloc == host) and self.headers.get('Sec-Fetch-Site') != 'cross-site'

    def do_POST(self):
        try:
            if not self.allowed_mutation():
                return self.json_response({'error': '请从本机工具台操作'}, 403)
            path = urlsplit(self.path).path
            if path == '/api/upload':
                query = parse_qs(urlsplit(self.path).query)
                filename = Path(query.get('name', ['asset'])[0]).name
                if Path(filename).suffix.lower() not in ('.mp4','.mov','.webm','.mp3','.wav','.m4a','.ogg','.flac','.png','.jpg','.jpeg','.webp','.json','.txt','.md','.csv','.pdf'):
                    raise ValueError('此文件类型暂不支持')
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 250*1024*1024:
                    raise ValueError('文件大小须在 250 MB 内')
                dest = PROJECT/'public/uploads'/f'{uuid.uuid4().hex[:12]}{Path(filename).suffix.lower()}'
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open('wb') as f:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024*1024, remaining))
                        if not chunk:
                            raise ValueError('上传中断')
                        f.write(chunk)
                        remaining -= len(chunk)
                return self.json_response({'path': str(dest), 'url': '/media/uploads/'+dest.name, 'asset': 'uploads/'+dest.name, 'name': filename})
            if path == '/api/production/visual-styles/import':
                length=int(self.headers.get('Content-Length',0))
                if not 0 < length <= 200*1024*1024: raise ValueError('请选择 200 MB 以内的模板压缩包')
                return self.json_response(importlib.import_module('workbench.style_transfer').import_style(ROOT,self.rfile.read(length)),201)
            data = self.read_json()
            if path == '/api/production/import-script':
                mod = importlib.import_module('workbench.script_import')
                if data.get('url'):
                    return self.json_response(mod.fetch_script(data['url']))
                return self.json_response({'script': mod.extract_html(str(data.get('html') or ''))})
            if path == '/api/production/visual-styles':
                return self.json_response(importlib.import_module('workbench.visual_styles').StyleLibrary(ROOT).create(data),201)
            match = re.fullmatch(r'/api/production/visual-styles/(style-[0-9a-f]{12}|handdrawn-realistic-v1)/(archive|restore)', path)
            if match:
                return self.json_response(importlib.import_module('workbench.visual_styles').StyleLibrary(ROOT).set_archived(match[1],match[2]=='archive',data.get('expectedVersion')))
            match = re.fullmatch(r'/api/production/visual-styles/(style-[0-9a-f]{12})/revise', path)
            if match:
                return self.json_response(importlib.import_module('workbench.visual_styles').StyleLibrary(ROOT).revise(match[1],data))
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/storyboard/save-style', path)
            if match:
                return self.json_response(importlib.import_module('workbench.visual_styles').StyleLibrary(ROOT).from_storyboard(match[1],data),201)
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/video-runs', path)
            if match:
                return self.json_response(importlib.import_module('workbench.storyboard_video').Videos(ROOT).start(match[1],data),202)
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/storyboard/(create|board|request_changes|select_board|confirm_board|confirm_all|plan|confirm_plan|register_clip|accept_clip|request_clip_changes|edit_description)', path)
            if match:
                store=importlib.import_module('workbench.vibe_storyboard').Storyboards(ROOT)
                return self.json_response(store.create(match[1],data) if match[2]=='create' else store.mutate(match[1],match[2],data))
            if path == '/api/production/tasks':
                return self.json_response(importlib.import_module('workbench.production').Store(ROOT).create(data), 201)
            match = re.fullmatch(r'/api/production/tasks/(V\d{8}-[a-f0-9]{8})/(revise|progress|deliver)', path)
            if match:
                store = importlib.import_module('workbench.production').Store(ROOT)
                return self.json_response(getattr(store, match[2])(match[1], data))
            if path == '/api/director/settings':
                return self.json_response(director().save_settings(data))
            if path in ('/api/director/test','/api/director/models'):
                return self.json_response(director().test(data.get('profile_id'),models=path.endswith('/models')))
            if path == '/api/director/run':
                return self.json_response(director().enqueue(data),202)
            if path == '/api/director/cancel':
                return self.json_response(director().cancel(data.get('id')))
            if path == '/api/director/propose':
                with LOCK:
                    d=director(); base=load_config()
                    if data.get('baseHash') and data['baseHash']!=importlib.import_module('workbench.director').digest(base):
                        raise ValueError('工程已变化，请重新读取当前工程')
                    return self.json_response(d.propose(data.get('instruction',''),data,base,data.get('source','chat-agent'),data.get('role','edit')))
            if path in ('/api/director/apply','/api/director/undo'):
                return self.json_response(director().apply(data.get('id'),undo=path.endswith('/undo')))
            if path == '/api/select-audio':
                relative = str(data.get('source','')).removeprefix('/vendor/')
                source = self.safe_path(VENDOR, relative)
                if not source.is_file() or source.suffix.lower() not in ('.mp3','.wav','.ogg','.m4a','.flac'):
                    raise ValueError('请选择镜头库内的音频')
                dest = PROJECT/'public/library-audio'/(hashlib.sha256(source.read_bytes()).hexdigest()[:14]+source.suffix)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.copy2(source,dest)
                return self.json_response({'path':str(dest),'url':'/media/library-audio/'+dest.name,'name':source.name})
            if path == '/api/native/launch':
                tools = importlib.import_module('workbench.library').native_tools(VENDOR, ROOT)['tools']
                tool = next((t for t in tools if t['id']==data.get('id')), None)
                if not tool or not tool.get('available'):
                    raise ValueError('独立工具依赖尚未齐备：'+', '.join(tool.get('missingDependencies',[]) if tool else []))
                if tool['id']=='canvas':
                    return self.json_response({'url':tool['url'],'message':'Canvas 独立预览已就绪'})
                existing = NATIVE_PROCESSES.get(tool['id'])
                if existing and existing.poll() is None:
                    return self.json_response({'url':tool['url'],'message':'返回已启动的独立工具'})
                service_record = ROOT/'work/native'/('service-'+tool['id']+'.json')
                if service_record.exists():
                    record=json.loads(service_record.read_text(encoding='utf-8'))
                    if record.get('url')==tool['url'] and record.get('cwd')==tool['cwd']:
                        try:
                            with urllib.request.urlopen(tool['url'],timeout=2) as check:
                                if check.status==200:
                                    return self.json_response({'url':tool['url'],'message':'返回此前已启动的独立工具'})
                        except OSError:
                            pass
                if tool.get('prepareArgv'):
                    subprocess.run(tool['prepareArgv'], cwd=tool['cwd'],check=True,capture_output=True,timeout=60)
                log = (ROOT/'work/native'/f'{tool["id"]}-server.log').open('a',encoding='utf-8')
                NATIVE_PROCESSES[tool['id']] = subprocess.Popen(tool['argv'],cwd=tool['cwd'],stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                process=NATIVE_PROCESSES[tool['id']]
                atomic_json(service_record,{'url':tool['url'],'cwd':tool['cwd'],'pid':process.pid,'argv':tool['argv']})
                for attempt in range(40):
                    if process.poll() is not None:
                        raise ValueError('独立工具启动失败，请查看 '+tool['id']+'-server.log')
                    try:
                        with urllib.request.urlopen(tool['url'],timeout=1) as check:
                            if check.status==200:
                                return self.json_response({'url':tool['url'],'message':'独立工具已启动'})
                    except OSError:
                        time.sleep(.2)
                return self.json_response({'url':tool['url'],'message':'正在启动本工具台的独立端口，稍后刷新'})
            if path == '/api/config':
                config = validate_config(data)
                with LOCK:
                    save_config(config)
                return self.json_response({'saved': True, 'config': config})
            if path == '/api/films/open':
                with LOCK:
                    films=importlib.import_module('workbench.films')
                    config=validate_config(films.load_editor(ROOT,data.get('id','')))
                    current=load_config()
                    if data.get('automatic') and not films.can_auto_open(ROOT,data.get('id',''),current):
                        return self.json_response({'opened':False,'reason':'current_project_changed','config':current})
                    if not current.get('editFilmId') and current.get('selectedBaseline')=='realistic-halfbody' and not current.get('scenePack'):
                        old=next((f for f in films.catalog(ROOT) if '最终选定' in f['video']['name']),None)
                        if old:films.save_editor(ROOT,{**current,'editFilmId':old['id']})
                    else:films.save_editor(ROOT,current)
                    save_config(config)
                return self.json_response({'saved':True,'opened':True,'config':config})
            if path == '/api/films/register':
                config=validate_config(data.get('config',load_config()))
                if not config.get('shots'):raise ValueError('请提供生成本条影片时使用的镜头工程')
                video=Path(str(data.get('video','')))
                if not video.is_absolute():video=ROOT/video
                identifier=importlib.import_module('workbench.films').register_film(ROOT,video,config)
                return self.json_response({'registered':True,'filmId':identifier,'autoOpenEditor':True})
            if path == '/api/reset':
                config = default_config()
                atomic_json(CONFIG, config)
                return self.json_response(config)
            if path == '/api/tools':
                action = data.get('action')
                if action not in ('render','still','pilot','catalog','research','storyboard','audio_analyze','tts','qc','export_draft','capture','publish_prepare','package','vendor_workbench','demo','native','publish'):
                    raise ValueError('未知工具')
                config = validate_config(data.get('config', load_config()))
                job_id = time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
                JOBS[job_id] = {'id': job_id, 'action': action, 'status': 'queued', 'phase': '等待处理', 'created': time.time()}
                threading.Thread(target=tool_worker, args=(job_id, action, data.get('payload') or {}, config), daemon=True).start()
                return self.json_response(JOBS[job_id], 202)
            return self.send_error(404)
        except importlib.import_module('workbench.production').Conflict as error:
            self.json_response({'error': str(error)}, 409)
        except Exception as error:
            self.json_response({'error': str(error)}, 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8796)
    args = parser.parse_args()
    OUTPUTS.mkdir(exist_ok=True)
    for f in (OUTPUTS/'runs').glob('*/job.json') if (OUTPUTS/'runs').exists() else []:
        try:
            job = json.loads(f.read_text(encoding='utf-8'))
            if job.get('status') in ('queued', 'running'):
                job.update(status='interrupted', phase='上次会话中断，请创建新任务')
            JOBS[job['id']] = job
        except Exception:
            pass
    print(f'Park Vibe workbench: http://127.0.0.1:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
