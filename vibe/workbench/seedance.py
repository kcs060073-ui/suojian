"""Seedance adapter based on the existing Cutroom POC; no credentials are stored here."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

MODEL = os.environ.get('SUOJIAN_SEEDANCE_MODEL', 'doubao-seedance-2-0-260128')
API = 'https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks'


def safe(value, secret=''):
    if isinstance(value, dict):
        return {k: safe(v, secret) for k, v in value.items() if k.lower() not in ('authorization', 'api_key', 'credential_ref')}
    if isinstance(value, list):
        return [safe(v, secret) for v in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, '[redacted]')
        value = re.sub(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', '[image payload omitted]', value)
        return re.sub(r'https?://[^\s"<>]+', lambda m: urllib.parse.urlunsplit((*urllib.parse.urlsplit(m[0])[:3], '', '')), value)
    return value


def build_request(image):
    encoded = base64.b64encode(Path(image).read_bytes()).decode('ascii')
    return {'model': MODEL, 'content': [{'type': 'text', 'text': ''},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}, 'role': 'first_frame'}],
        'duration': 7, 'resolution': '720p', 'ratio': 'adaptive', 'generate_audio': False, 'watermark': True}


def credentials():
    token = os.environ.get('ARK_API_KEY')
    if token:
        return token
    profile = os.environ.get('SUOJIAN_SEEDANCE_PROFILE_ID')
    if not profile:
        raise RuntimeError('请配置 ARK_API_KEY，或指定 Cutroom 中的 SUOJIAN_SEEDANCE_PROFILE_ID；未提交任务')
    cutroom = Path(os.environ.get('SUOJIAN_CUTROOM_ROOT', Path.home() / 'CUTROOM')).resolve()
    database = cutroom / 'runtime/workbench/catalog.sqlite'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT p.model,p.enabled,a.base_url,a.credential_ref FROM model_profiles p JOIN model_provider_accounts a ON a.account_id=p.account_id WHERE p.profile_id=?', (profile,)).fetchone()
    if not row or row[0] != MODEL or not row[1] or row[2].rstrip('/') != API.rsplit('/contents/', 1)[0]:
        raise RuntimeError('Cutroom 模型配置不可用或与所选模型不一致；未提交任务')
    sys.path.insert(0, str(cutroom))
    from media_catalog.credentials import read_provider_secret
    token = read_provider_secret(row[3])
    if not token:
        raise RuntimeError('Cutroom 安全凭据不可用；未提交任务')
    return token


class HTTP:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, body=None):
        request = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response), response.headers.get('X-Request-Id')
        except urllib.error.HTTPError as error:
            detail = error.read(12000).decode('utf-8', 'replace')
            raise RuntimeError('HTTP ' + str(error.code) + ' ' + safe(detail, self.token)) from None

    def download(self, url, target):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username:
            raise RuntimeError('视频下载地址无效')
        target = Path(target)
        part = target.with_suffix('.mp4.part')
        with urllib.request.urlopen(url, timeout=90) as response, part.open('xb') as out:
            count = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > 512 * 1024 * 1024:
                    raise RuntimeError('生成的视频超过大小限制')
                out.write(block)
        if target.exists():
            raise FileExistsError(target)
        part.rename(target)
        with target.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        return {'path': str(target), 'bytes': count, 'sha256': digest, 'downloadHost': parsed.hostname}
