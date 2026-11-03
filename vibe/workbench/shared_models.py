"""Read a selected shared model without copying or exposing its credentials."""
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3


def planning_profile(root):
    binding = Path(root) / 'work' / 'director' / 'shared-profile.json'
    if not binding.is_file():
        return None
    profile_id = json.loads(binding.read_text(encoding='utf-8')).get('profileId')
    if not profile_id:
        return None
    cutroom = Path(os.environ.get('SUOJIAN_CUTROOM_ROOT') or Path.home() / 'CUTROOM').resolve()
    database = cutroom / 'runtime' / 'workbench' / 'catalog.sqlite'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT p.profile_id,p.display_name,p.model,p.enabled,p.capability,p.settings_json,a.provider,a.base_url,a.credential_ref FROM model_profiles p JOIN model_provider_accounts a ON a.account_id=p.account_id WHERE p.profile_id=?', (profile_id,)).fetchone()
    if not row or not row[3] or row[4] != 'planning':
        raise ValueError('共享文字模型已停用或不存在，请在模型与 API 中重新选择。')
    options = json.loads(row[5] or '{}')
    if options.get('protocol') != 'openai':
        raise ValueError('所选共享文字模型暂不支持 VibeCraft 导演协议。')
    spec = importlib.util.spec_from_file_location('suojian_cutroom_credentials', cutroom / 'media_catalog' / 'credentials.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret = module.read_provider_secret(row[8])
    if not secret:
        raise ValueError('共享文字模型的本机凭据不可用。')
    return {'id': 'shared-' + row[0], 'name': row[1], 'provider': row[6],
            'base_url': row[7], 'protocol': 'chat', 'model': row[2], 'key': secret, 'enabled': True}
