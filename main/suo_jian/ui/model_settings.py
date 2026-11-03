"""One local settings entry for existing model accounts and VibeCraft routing."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from flask import jsonify, render_template, request
from suo_jian.integrations.cutroom.client import call_local_adapter


def register_model_settings(app):
    def local(json_required=False):
        origin = request.headers.get('Origin')
        if (request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}
                or request.headers.get('Sec-Fetch-Site') == 'cross-site'
                or (origin and urlsplit(origin).netloc != request.host)
                or (json_required and not request.is_json)):
            raise PermissionError('请从溯剪本机页面操作。')

    def backend(method, path, body=None):
        root = Path(os.environ.get('SUOJIAN_CUTROOM_ROOT', Path.home() / 'CUTROOM'))
        data = call_local_adapter(root, 'model_settings', {'method': method, 'path': path, 'body': body or {}})
        if data.get('settingsError'):
            raise ValueError(data['settingsError'])
        return data

    def binding_path():
        root = Path(os.environ.get('SUOJIAN_VIBE_ROOT', Path.home() / 'Documents' / 'ChatGPT' / '溯剪终端'))
        return root / 'work' / 'director' / 'shared-profile.json'

    def guarded(function):
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except PermissionError as exc:
                return jsonify(error=str(exc)), 403
            except (ValueError, FileNotFoundError) as exc:
                return jsonify(error=str(exc)), 400
            except Exception:
                return jsonify(error='模型设置暂不可用，请检查本机后端和数据库。'), 503
        wrapped.__name__ = function.__name__
        return wrapped

    @app.get('/settings/models')
    @guarded
    def model_settings_page():
        local()
        return render_template('model_settings.html')

    @app.get('/api/model-settings')
    @guarded
    def model_settings_read():
        local()
        data = backend('GET', '/api/settings/model-profiles')
        file = binding_path()
        binding = json.loads(file.read_text(encoding='utf-8')) if file.is_file() else {}
        accounts = [{**{k: a.get(k) for k in ('accountId', 'name', 'provider')}, 'ttsResourceId': (a.get('settings') or {}).get('tts_resource_id')} for a in data.get('accounts', [])]
        profiles = [{k: p.get(k) for k in ('profileId', 'accountId', 'displayName', 'capability', 'model', 'enabled', 'isDefault', 'availabilityStatus')} for p in data.get('profiles', [])]
        return jsonify(accounts=accounts, profiles=profiles, vibeProfileId=binding.get('profileId'))

    @app.post('/api/model-settings/accounts')
    @guarded
    def model_settings_account_create():
        local(True)
        body = request.get_json()
        address = (body.get('baseUrl') or '').strip()
        if address:
            parsed = urlsplit(address)
            if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}))):
                raise ValueError('远程接口请使用 HTTPS，本机接口可使用 HTTP。')
        return jsonify(backend('POST', '/api/settings/provider-accounts', body))

    @app.post('/api/model-settings/profiles')
    @guarded
    def model_settings_profile_create():
        local(True)
        return jsonify(backend('POST', '/api/settings/model-profiles', request.get_json()))

    @app.post('/api/model-settings/profiles/<profile_id>/default')
    @guarded
    def model_settings_default(profile_id):
        local(True)
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', profile_id):
            raise ValueError('模型配置编号无效。')
        return jsonify(backend('POST', '/api/settings/model-profiles/' + profile_id + '/default'))

    @app.route('/api/model-settings/accounts/<account_id>/voices', methods=['GET', 'POST'])
    @guarded
    def model_settings_voices(account_id):
        local(request.method == 'POST')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', account_id):
            raise ValueError('服务编号无效。')
        path = '/api/settings/provider-accounts/' + account_id + '/voices'
        if request.method == 'POST':
            path += '/refresh'
        data = backend(request.method, path)
        voices = [{k: voice.get(k) for k in ('voiceId', 'displayName', 'selectable')} for voice in data.get('voices', [])]
        return jsonify(voices=voices)

    @app.post('/api/model-settings/vibecraft')
    @guarded
    def model_settings_bind_vibecraft():
        local(True)
        profile_id = request.get_json().get('profileId')
        data = backend('GET', '/api/settings/model-profiles')
        profile = next((p for p in data.get('profiles', []) if p.get('profileId') == profile_id), None)
        if profile_id and (not profile or profile.get('capability') != 'planning' or not profile.get('enabled')):
            raise ValueError('请选择已启用的文字策划模型。')
        file = binding_path()
        file.parent.mkdir(parents=True, exist_ok=True)
        temporary = file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'profileId': profile_id}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(file)
        return jsonify(profileId=profile_id)
