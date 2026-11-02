"""Installed Park Vibe module and local worker lifecycle for the SuoJian host."""
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from flask import jsonify, render_template, request

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'extensions' / 'park-vibe'
ADDRESS = 'http://127.0.0.1:8796'
CREATIVE_MODULE = Path.home() / 'Documents' / 'ChatGPT' / '溯剪终端'
CREATIVE_ADDRESS = 'http://127.0.0.1:18979'
LOCK = threading.Lock()


def health():
    try:
        with urllib.request.urlopen(ADDRESS + '/api/health', timeout=1) as response:
            value = json.load(response)
        return value if value.get('deployment') == 'suojian-park-vibe-v2' else None
    except (OSError, ValueError):
        return None


def ensure_worker():
    with LOCK:
        if health():
            return True
        # An occupied port is never terminated by a web request.
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', 8796)) == 0:
                return False
        logs = MODULE / 'work'
        with (logs / 'server.log').open('ab') as out, (logs / 'server-error.log').open('ab') as err:
            subprocess.Popen([sys.executable, '-B', '-m', 'workbench.server', '--port', '8796'],
                cwd=MODULE, stdout=out, stderr=err,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(40):
            if health():
                return True
            time.sleep(.1)
        return False


def creative_health():
    try:
        with urllib.request.urlopen(CREATIVE_ADDRESS + '/api/production/visual-styles/status', timeout=1) as response:
            value = json.load(response)
        return value.get('library') is True and value.get('version') == 2
    except (OSError, ValueError):
        return False


def ensure_creative_worker():
    with LOCK:
        if creative_health():
            return True
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', 18979)) == 0:
                return False
        if not (CREATIVE_MODULE / 'workbench' / 'server.py').is_file():
            return False
        logs = CREATIVE_MODULE / 'work'
        logs.mkdir(exist_ok=True)
        with (logs / 'main-entry-server.log').open('ab') as out, (logs / 'main-entry-server-error.log').open('ab') as err:
            subprocess.Popen([sys.executable, '-B', '-m', 'workbench.server', '--port', '18979'],
                cwd=CREATIVE_MODULE, stdout=out, stderr=err,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(40):
            if creative_health():
                return True
            time.sleep(.1)
        return False


def film_projects(workspace=None):
    """Read local film records without starting rendering or calling providers."""
    installed = Path(workspace) / 'extensions/park-vibe' if workspace is not None else MODULE
    source = installed / 'workbench' / 'films.py'
    if not source.is_file():
        return []
    spec = importlib.util.spec_from_file_location('suojian_installed_vibe_films', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [dict(id=f['id'], job=f['id'], mode='vibe', title=f['title'],
        status='completed', status_label='影片可继续编辑' if f['editorAvailable'] else '视频已导出',
        href='/motion?film=' + f['id'], updated_at=f['created']) for f in module.catalog(installed)]


def register_park_vibe(app):
    @app.before_request
    def park_vibe_entry():
        if request.path != '/motion' or request.args.get('job'):
            return None
        if request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
            return jsonify(error='仅限本机工作台'), 403
        film = request.args.get('film', '')
        query = '?film=' + film if re.fullmatch(r'film-[0-9a-f]{16}', film) else ''
        legacy = bool(query) or request.args.get('view') == 'films'
        ready = ensure_worker() if legacy else ensure_creative_worker()
        view = 'films' if request.args.get('view') == 'films' else 'edit'
        return render_template('park_vibe.html', ready=ready,
                               worker=ADDRESS + '/' + query + '#' + view if legacy else CREATIVE_ADDRESS + '/',
                               legacy=legacy)

    @app.get('/motion/legacy')
    def park_vibe_legacy():
        return render_template('motion_explainer.html')

    @app.get('/api/park-vibe/status')
    def park_vibe_status():
        return jsonify(installed=MODULE.is_dir(), ready=bool(health()), version='2', entry='/motion')
