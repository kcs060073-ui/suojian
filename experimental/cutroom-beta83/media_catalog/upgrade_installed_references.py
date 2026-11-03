"""Separate shipped program files from user data in immutable recovery plans."""
import json
from pathlib import Path
from .path_utils import filesystem_path
from .upgrade_recovery import _plain_path, _fail


def _release(root, *, required=False):
    from scripts.product_update import manifest_files, safe_path, UpdateError, DATA_ROOTS
    from .upgrade_frozen_references import _observe
    root = _plain_path(root)
    path = _plain_path(root/'release-manifest.json')
    if not path.is_file():
        if required: _fail('INSTALLATION_MANIFEST_MISSING', '目标安装缺少发行清单。')
        return None
    signature = _observe(path)
    try:
        records = manifest_files(json.loads(filesystem_path(path).read_text(encoding='utf-8-sig')))
        records = {name: record for name,record in records.items() if name.split('/')[0] not in DATA_ROOTS}
        for name in records: safe_path(root, name)
    except (UpdateError, ValueError, TypeError, KeyError, OSError):
        _fail('INSTALLATION_MANIFEST_INVALID', '发行清单或运行文件路径无效。')
    if _observe(path) != signature:
        _fail('SOURCE_CHANGED', '发行清单在盘点期间变化。')
    return path, signature, records


def classify_installed_files(root, files):
    """Freeze classification and raw manifest identity in the recovery plan."""
    from .upgrade_frozen_references import _observe
    root = _plain_path(root)
    release = _release(root)
    if release is None: return None
    manifest, signature, records = release
    classified = []
    for raw, row in list(files.items()):
        path = _plain_path(raw)
        if not path.is_relative_to(root): continue
        relative = path.relative_to(root).as_posix()
        record = records.get(relative)
        if record is None: continue
        expected = {'size_bytes': record['bytes'], 'sha256': record['sha256']}
        if _observe(path) != expected or (row['bytes'],row['sha256']) != (record['bytes'],record['sha256']):
            _fail('INSTALLATION_FILE_CHANGED', '源安装文件与发行清单不一致，不能当作可保留的程序文件。')
        classified.append({'path':str(path),'relativePath':relative,'signature':expected})
    if not classified: return None
    files[str(manifest)] = {'path':str(manifest),'bytes':signature['size_bytes'],'sha256':signature['sha256'],'scope':'old_installation'}
    classified.append({'path':str(manifest),'relativePath':'release-manifest.json','signature':signature})
    if _observe(manifest) != signature: _fail('SOURCE_CHANGED', '发行清单在盘点期间变化。')
    return {'manifestPath':str(manifest),'manifestSignature':signature,'files':sorted(classified,key=lambda x:x['path'])}


def verify_installed_release(root):
    """Verify destination against its own exact release bytes, not compatibility IDs."""
    from .upgrade_frozen_references import _observe
    root = _plain_path(root)
    manifest, signature, records = _release(root, required=True)
    for name, record in records.items():
        if _observe(_plain_path(root/name)) != {'size_bytes':record['bytes'],'sha256':record['sha256']}:
            _fail('INSTALLATION_FILE_CHANGED', '目标安装文件与其发行清单不一致，未完成恢复。')
    if _observe(manifest) != signature: _fail('SOURCE_CHANGED', '目标发行清单在验证期间变化。')
    return signature
