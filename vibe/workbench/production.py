"""Vibe-only preparation, immutable inputs, dialogue handoff and real deliveries."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import quote
import urllib.request
import uuid

# 外部依赖路径统一来自 workbench/paths.py（可用 SUOJIAN_FFPROBE 覆盖）
from workbench.paths import FFPROBE

LOCK = threading.RLock()
ID = re.compile(r'V\d{8}-[a-f0-9]{8}')
STATUSES = {'awaiting_dialogue': '等待对话启动', 'checking': '检查输入', 'planning': '制作方案',
            'previewing': '制作小样', 'rendering': '制作全片', 'needs_user': '需要你处理',
            'review': '待审看', 'completed': '已完成', 'failed': '制作失败', 'interrupted': '等待对话继续'}
ACTIVE = {'checking', 'planning', 'previewing', 'rendering'}


class Conflict(ValueError):
    pass


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return copy.deepcopy(default)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex[:8] + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def library_root(root):
    candidates = [Path(root)/'work/skills/park-vibe-producer/assets/motiongit',
                  Path.home()/'.codex/skills/park-vibe-producer/assets/motiongit']
    return next((p.resolve() for p in candidates if (p/'library-index.json').is_file()), candidates[0].resolve())


def personal_root(root):
    return library_root(root).parents[4]/'work/secrets/motiongit-personal'


def version_key(value):
    return tuple(int(n) for n in re.findall(r'\d+', value or '0'))


def personal_packages(root, key):
    base = personal_root(root).resolve()
    catalog = read(base/'index.json', {})
    if not catalog.get('localOnly') or not catalog.get('authorization'):
        return []
    result = []
    for p in catalog.get('items', []):
        path = (base/p.get('archive', '__missing__')).resolve()
        if key in p.get('supportedKeys', [p.get('key')]) and p.get('archiveChecked') and path.is_relative_to(base/'archives') and path.is_file():
            result.append({**p, '_archive': path})
    return sorted(result, key=lambda p: (version_key(p.get('version')), p.get('importedAt', 0)), reverse=True)


def templates(root):
    base = library_root(root)
    catalog = read(base/'library-index.json', {})
    result = []
    for row in catalog.get('items', []):
        key = row.get('key', '')
        if not re.fullmatch(r'[A-Za-z0-9-]+', key):
            continue
        packages = []
        for pack in row.get('packages', []):
            path = (base/pack.get('path', '')).resolve()
            if path.is_relative_to(base/'packages') and path.is_file() and pack.get('personalMarkersFound') is False:
                packages.append({k: pack.get(k) for k in ('language', 'sha256', 'bytes', 'renderVerified')})
        private = personal_packages(root, key)
        public_count = len(packages)
        use_private = bool(private) and (not public_count or version_key(private[0]['version']) > version_key(row.get('version')))
        if private:
            p = private[0]
            packages.append({**{k: p.get(k) for k in ('language', 'sha256', 'bytes', 'renderVerified')}, 'personal': True})
        poster = (base/row.get('posterLocal', '__missing__')).resolve()
        result.append({'key': key, 'title': row.get('title', key), 'summary': row.get('summary', ''),
                       'width': row.get('width'), 'height': row.get('height'), 'orientation': row.get('orientation'),
                       'version': private[0]['version'] if use_private else row.get('version'),
                       'available': bool(packages), 'packages': packages, 'personal': use_private,
                       'licenseNotes': private[0].get('licenseNotes', []) if private else [],
                       'status': 'downloaded' if packages else row.get('status', 'unavailable'),
                       'poster': '/production/posters/'+key if poster.is_relative_to(base/'posters') and poster.is_file() else None,
                       'website': row.get('website'), 'demo': row.get('demo')})
    return {'items': result, 'checkedAt': catalog.get('checkedAt'), 'keyFreeOnly': False,
            'summary': {'total': len(result), 'available': sum(r['available'] for r in result),
                        'personal': sum(r['personal'] for r in result)}}


def poster_path(root, key):
    base = library_root(root)
    catalog = read(base/'library-index.json', {})
    row = next((r for r in catalog.get('items', []) if r.get('key') == key), None)
    if not row:
        raise ValueError('模板不存在')
    path = (base/row.get('posterLocal', '__missing__')).resolve()
    if not path.is_relative_to(base/'posters') or not path.is_file():
        raise ValueError('本地封面不存在')
    return path


def text(value, name, maximum=1000, required=False):
    if not isinstance(value, str):
        raise ValueError(name+'须为文字')
    value = value.strip()
    if len(value) > maximum or (required and not value):
        raise ValueError(name+('不能为空' if not value else '过长'))
    return value


def template_snapshot(root, key, orientation):
    if not key or key == 'auto':
        return None
    base = library_root(root)
    raw = read(base/'library-index.json', {})
    row = next((r for r in raw.get('items', []) if r.get('key') == key), None)
    if not row or row.get('orientation') != orientation:
        raise ValueError('所选模板与画幅不符，请重新选择')
    package = next((p for p in row.get('packages', []) if p.get('language') == 'zh'), None)
    private = personal_packages(root, key)
    use_private = bool(private) and (not package or version_key(private[0]['version']) > version_key(row.get('version')))
    if use_private or not package or package.get('personalMarkersFound') is not False:
        if not private:
            raise ValueError('此模板尚无已下载并校验的本地包')
        p = private[0]
        with p['_archive'].open('rb') as f:
            actual = hashlib.file_digest(f, 'sha256').hexdigest()
        if actual != p['sha256']:
            raise ValueError('个人模板包哈希不一致，请先检查模板库')
        return {'key': key, 'title': row['title'], 'version': p['version'], 'package': str(p['_archive']),
                'sha256': actual, 'language': p['language'], 'keyFree': False, 'localOnly': True,
                'licenseNotes': p.get('licenseNotes', []), 'renderVerified': bool(p.get('renderVerified'))}
    path = (base/package['path']).resolve()
    if not path.is_relative_to(base/'packages') or not path.is_file():
        raise ValueError('公开模板包缺失')
    with path.open('rb') as f:
        actual = hashlib.file_digest(f, 'sha256').hexdigest()
    if actual != package.get('sha256'):
        raise ValueError('模板包内容与索引不一致，请先检查模板库')
    return {'key': key, 'title': row['title'], 'version': row.get('version'),
            'package': str(path), 'sha256': actual, 'language': 'zh', 'keyFree': True,
            'renderVerified': bool(package.get('renderVerified'))}


def normalize(root, payload, previous=None):
    if not isinstance(payload, dict):
        raise ValueError('任务输入须为对象')
    orientation = payload.get('format', 'portrait')
    if orientation not in ('portrait', 'landscape'):
        raise ValueError('请选择横屏或竖屏')
    intent = payload.get('styleIntent', 'vary')
    if intent not in ('keep', 'vary', 'explore'):
        raise ValueError('创作方向无效')
    workflow_mode = payload.get('workflowMode', (previous or {}).get('workflowMode', 'standard'))
    if workflow_mode not in ('standard', 'stick-narrative', 'live-action', 'mixed'):
        raise ValueError('制作模式无效')
    route = payload.get('creativeRoute', (previous or {}).get('creativeRoute'))
    if route not in (None, 'storyboard', 'native'):
        raise ValueError('创作路线无效')
    materials = payload.get('materials', [])
    if isinstance(materials, str):
        materials = materials.splitlines()
    if not isinstance(materials, list) or len(materials) > 100:
        raise ValueError('素材列表无效')
    materials = list(dict.fromkeys(text(m, '素材位置', 2000) for m in materials if isinstance(m, str) and m.strip()))
    if not materials and workflow_mode != 'stick-narrative' and route != 'storyboard':
        raise ValueError('请填写园区素材位置或导入素材')
    duration = payload.get('durationSeconds')
    if duration not in (None, ''):
        if type(duration) not in (int, float) or not 5 <= duration <= 600:
            raise ValueError('目标时长须为 5–600 秒，或留空由内容决定')
    else:
        duration = None
    previous = previous or {}
    # Revisions of a pinned template keep their exact source even after library updates.
    if previous.get('template') and previous['template']['key'] == payload.get('templateKey'):
        if orientation == previous.get('format'):
            snapshot = copy.deepcopy(previous['template'])
        else:
            snapshot = template_snapshot(root, payload.get('templateKey', 'auto'), orientation)
    else:
        snapshot = template_snapshot(root, payload.get('templateKey', 'auto'), orientation)
    seed = secrets.randbits(31) if not previous or payload.get('redraw') is True else previous['autoSelection']['seed']
    preview = payload.get('previewFirst', 'auto')
    if preview not in ('auto', 'yes', 'no'):
        raise ValueError('预览方式无效')
    result = {'title': text(payload.get('title', ''), '任务名称', 80, True),
            'script': text(payload.get('script', ''), '脚本或创作意图', 30000, True),
            'materials': materials, 'format': orientation, 'styleIntent': intent, 'workflowMode': workflow_mode,
            'reference': text(payload.get('reference', ''), '参考作品', 3000),
            'requirements': text(payload.get('requirements', ''), '特殊要求', 5000),
            'changeRequest': text(payload.get('changeRequest', ''), '修改要求', 3000),
            'baseVersion': text(payload.get('baseVersion', ''), '参考版本', 100),
            'voice': text(payload.get('voice', ''), '声音要求', 1000), 'durationSeconds': duration,
            'previewFirst': preview, 'template': snapshot,
            'autoSelection': {'mode': 'content-matched-variety', 'seed': seed,
                              'cards': 'dialogue-selects', 'music': 'dialogue-selects', 'sfx': 'action-linked'},
            'externalServices': {'publicAssetSearch': payload.get('onlineAssets', True) is True,
                                 'personalMotionPackages': bool(snapshot and snapshot.get('keyFree') is False), 'paidGeneration': False}}
    if route:
        from workbench.visual_styles import default_style
        result['creativeRoute'] = route
        result['businessPreset'] = text(payload.get('businessPreset', (previous or {}).get('businessPreset', 'park')), '业务预设', 100)
        style_key = payload.get('visualStyleKey', (previous or {}).get('visualStyle', {}).get('style_id', 'handdrawn-realistic-v1'))
        if style_key == 'custom':
            result['visualStyle'] = {'style_id': 'custom', 'name': '自定义参考', 'reference': result['reference'], 'description': result['requirements']}
        elif style_key == 'handdrawn-realistic-v1':
            previous_style = (previous or {}).get('visualStyle') or {}
            result['visualStyle'] = copy.deepcopy(previous_style if previous_style.get('style_id') == style_key else default_style(root))
        else:
            from workbench.visual_styles import StyleLibrary
            previous_style = (previous or {}).get('visualStyle') or {}
            result['visualStyle'] = copy.deepcopy(previous_style if previous_style.get('style_id')==style_key else StyleLibrary(root).get(style_key))
        result['referenceRole'] = payload.get('referenceRole', (previous or {}).get('referenceRole', 'style'))
        if result['referenceRole'] not in ('style', 'scene', 'evidence'):
            raise ValueError('参考用途无效')
        groups = payload.get('referenceGroups', (previous or {}).get('referenceGroups'))
        if groups is not None:
            if not isinstance(groups, dict) or any(not isinstance(groups.get(k, []), list) for k in ('scene', 'other')):
                raise ValueError('参考图片分组无效')
            result['referenceGroups'] = {k: [p for p in groups.get(k, []) if p in materials] for k in ('scene', 'other')}
    return result


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root/'work/production/tasks'

    def path(self, identifier):
        if not ID.fullmatch(str(identifier)):
            raise ValueError('任务编号无效')
        return self.base/identifier

    def get(self, identifier):
        folder = self.path(identifier)
        task = read(folder/'task.json')
        if not isinstance(task, dict):
            raise ValueError('任务不存在')
        task['input'] = read(folder/f'input-{task["revision"]:03d}.json')
        effective = task['status']
        if effective in ACTIVE and time.time()-task.get('updatedAt', 0) > 1800:
            effective = 'interrupted'
        task['effectiveStatus'] = effective
        task['statusLabel'] = STATUSES[effective]
        task['handoff'] = f'启动园区 Vibe 任务 {identifier}，读取第 {task["revision"]} 版输入；按任务要求制作并回写结果。'
        task['inputFile'] = str(folder/f'input-{task["revision"]:03d}.json')
        return task

    def list(self):
        rows = []
        with LOCK:
            if not self.base.exists():
                return []
            for folder in self.base.iterdir():
                if folder.is_dir() and ID.fullmatch(folder.name) and (folder/'task.json').is_file():
                    task = self.get(folder.name)
                    rows.append({**{k: task[k] for k in ('id', 'title', 'revision', 'status', 'effectiveStatus', 'statusLabel',
                                                        'createdAt', 'updatedAt', 'handoff', 'versions')},
                                 'materials': task['input']['materials'], 'reference': task['input']['reference']})
        return sorted(rows, key=lambda r: r['updatedAt'], reverse=True)

    def create(self, payload):
        inputs = normalize(self.root, payload)
        identifier = time.strftime('V%Y%m%d-')+uuid.uuid4().hex[:8]
        with LOCK:
            folder = self.path(identifier)
            task = {'schema': 'suojian.vibe-task/1', 'id': identifier, 'title': inputs['title'], 'revision': 1,
                    'inputHash': signature(inputs), 'status': 'awaiting_dialogue', 'versions': [],
                    'createdAt': time.time(), 'updatedAt': time.time(),
                    'events': [{'at': time.time(), 'status': 'awaiting_dialogue', 'note': '需求已保存，等待在对话中明确启动'}]}
            save(folder/'input-001.json', inputs)
            save(folder/'task.json', task)
        return self.get(identifier)

    def expected(self, task, payload):
        if payload.get('expectedRevision') != task['revision']:
            raise Conflict('任务输入已有新修订，请刷新后重试')
        if payload.get('inputHash') and payload['inputHash'] != task['inputHash']:
            raise Conflict('任务输入内容已变化，请重新读取')

    def revise(self, identifier, payload):
        with LOCK:
            task = self.get(identifier)
            self.expected(task, payload)
            if task['effectiveStatus'] in ACTIVE:
                raise Conflict('此任务正在制作；请在对话中先保存断点，再修订输入')
            data = payload.get('input', {})
            inputs = normalize(self.root, data, task['input'])
            revision = task['revision']+1
            if (self.path(identifier)/f'input-{revision:03d}.json').exists():
                raise Conflict('该修订已经存在')
            save(self.path(identifier)/f'input-{revision:03d}.json', inputs)
            task.update(title=inputs['title'], revision=revision, inputHash=signature(inputs), status='awaiting_dialogue', updatedAt=time.time())
            task['events'].append({'at': time.time(), 'status': 'awaiting_dialogue', 'revision': revision,
                                   'note': '新修订已保存；旧输入和已生成版本保留'})
            self.persist(task)
        return self.get(identifier)

    def persist(self, task):
        save(self.path(task['id'])/'task.json', {k: v for k, v in task.items() if k not in ('input', 'inputFile', 'handoff', 'effectiveStatus', 'statusLabel')})

    def progress(self, identifier, payload):
        if payload.get('executor') != 'dialogue':
            raise ValueError('制作进度由执行对话回写；网页保存不会自动启动制作')
        status = payload.get('status')
        if status not in STATUSES or status in ('review', 'awaiting_dialogue'):
            raise ValueError('进度状态无效；审看状态须由实际交付产生')
        with LOCK:
            task = self.get(identifier)
            self.expected(task, payload)
            if status == 'rendering':
                from workbench.vibe_storyboard import Storyboards
                Storyboards(self.root).require_plans(identifier)
            if status == 'completed' and not any(v['revision'] == task['revision'] for v in task['versions']):
                raise ValueError('当前修订尚无实际输出，不能标为完成')
            note = text(payload.get('note', ''), '进度说明', 2000, True)
            task.update(status=status, updatedAt=time.time())
            task['events'].append({'at': time.time(), 'status': status, 'revision': task['revision'], 'note': note})
            self.persist(task)
        return self.get(identifier)

    def deliver(self, identifier, payload):
        if payload.get('executor') != 'dialogue':
            raise ValueError('交付由执行对话核对后登记')
        with LOCK:
            task = self.get(identifier)
            self.expected(task, payload)
            from workbench.vibe_storyboard import Storyboards
            Storyboards(self.root).require_clips(identifier)
            video = Path(str(payload.get('video', '')))
            video = (self.root/video).resolve() if not video.is_absolute() else video.resolve()
            if not video.is_relative_to(self.root/'outputs') or video.suffix.lower() != '.mp4' or not video.is_file():
                raise ValueError('请提供本模块 outputs 中真实存在的 MP4')
            probe = FFPROBE
            proc = subprocess.run([probe, '-v', 'error', '-show_entries', 'format=duration:stream=codec_type,width,height', '-of', 'json', str(video)], capture_output=True, text=True, encoding='utf-8', timeout=20, check=True)
            meta = json.loads(proc.stdout)
            stream = next((s for s in meta.get('streams', []) if s.get('codec_type') == 'video'), None)
            if not stream or float(meta.get('format', {}).get('duration', 0)) <= 0:
                raise ValueError('文件没有有效的视频流或时长')
            if (stream['width'] > stream['height']) != (task['input']['format'] == 'landscape'):
                raise ValueError('实际视频画幅与本次任务输入不一致')
            with video.open('rb') as f:
                sha = hashlib.file_digest(f, 'sha256').hexdigest()
            if any(v['sha256'] == sha and v['revision'] == task['revision'] for v in task['versions']):
                return self.get(identifier)
            mode = payload.get('editingMode', 'finished-cut')
            if mode not in ('finished-cut', 'native-project', 'shot-timeline'):
                raise ValueError('编辑能力无效')
            project = None
            if payload.get('sourceProject'):
                candidate = Path(payload['sourceProject']).resolve()
                if not candidate.exists() or not any(candidate.is_relative_to(p) for p in (self.root, Path.home()/'Documents/Codex', Path.home()/'Documents/ChatGPT')):
                    raise ValueError('原生工程不存在或超出本机制作目录')
                if any(part.lower() in ('secrets', 'personal') or part.startswith('.env') for part in candidate.parts):
                    raise ValueError('不能将凭据目录登记为制作工程')
                project = str(candidate)
            if mode != 'finished-cut' and not project:
                raise ValueError('声明可继续编辑时须提供实际制作工程')
            kind = payload.get('kind', 'full')
            if kind not in ('preview', 'full'):
                raise ValueError('请选择小样或整片')
            title = text(payload.get('title', video.stem), '版本名称', 120)
            note = text(payload.get('note', ''), '交付说明', 3000)
            film_id = None
            if mode == 'shot-timeline':
                config = read(project)
                if not isinstance(config, dict) or not config.get('shots') or config.get('format') != task['input']['format']:
                    raise ValueError('镜头工程须为本次画幅对应的真实镜头配置 JSON')
                from workbench.films import register_film
                film_id = register_film(self.root, video, config)
            relative = video.relative_to(self.root/'outputs').as_posix()
            version = {'id': uuid.uuid4().hex[:12], 'revision': task['revision'], 'inputHash': task['inputHash'],
                       'title': title, 'kind': kind,
                       'createdAt': time.time(), 'video': str(video), 'url': '/outputs/'+quote(relative, safe='/'),
                       'sha256': sha, 'bytes': video.stat().st_size, 'width': stream['width'], 'height': stream['height'],
                       'durationSeconds': float(meta['format']['duration']),
                       'hasAudio': any(s['codec_type'] == 'audio' for s in meta['streams']),
                       'editingMode': mode, 'sourceProject': project, 'filmId': film_id,
                       'note': note}
            task['versions'].append(version)
            # Independent sidecar; old task/version schema remains readable.
            from workbench.production_trace import safe_record, trace_delivery
            version['productionTrace'] = safe_record(self.root, trace_delivery, task, version)
            task.update(status='review', updatedAt=time.time())
            task['events'].append({'at': time.time(), 'status': 'review', 'versionId': version['id'],
                                   'revision': task['revision'], 'note': '实际视频已检查并登记，等待审看'})
            self.persist(task)
        return self.get(identifier)


def main():
    parser = argparse.ArgumentParser(description='园区 Vibe 对话制作交接；不会自行启动模型或渲染')
    parser.add_argument('action', choices=['list', 'show', 'progress', 'deliver'])
    parser.add_argument('id', nargs='?')
    parser.add_argument('--url', default='http://127.0.0.1:8796')
    parser.add_argument('--file', help='progress/deliver JSON，包含 expectedRevision 与 executor=dialogue')
    args = parser.parse_args()
    endpoint = '/api/production/tasks'
    if args.action != 'list':
        if not args.id or not ID.fullmatch(args.id):
            parser.error('需要实际任务编号')
        endpoint += '/'+args.id
    data = None
    if args.action in ('progress', 'deliver'):
        if not args.file:
            parser.error('需要 --file')
        endpoint += '/'+args.action
        data = json.dumps(read(args.file), ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(args.url+endpoint, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
