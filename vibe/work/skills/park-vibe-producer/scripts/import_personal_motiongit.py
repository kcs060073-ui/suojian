"""Import explicitly authorized personal originals locally; never read or print Key values."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import zipfile

SKILL = Path(__file__).resolve().parents[1]
LIBRARY = SKILL/'assets/motiongit'
VAULT = SKILL.parents[2]/'work/secrets/motiongit-personal'


def read(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def run(downloads, ledger=None):
    catalog = read(LIBRARY/'library-index.json', {})
    items = catalog.get('items', [])
    previous = read(VAULT/'index.json', {'items': []})
    result = {p['sha256']: p for p in previous['items']}
    sources = {Path(p['path']).resolve(): p for p in (ledger or []) if p.get('path')}
    imported, errors = [], []
    for path in sorted(Path(downloads).iterdir()):
        if not path.is_file() or not path.name.startswith('MotionGit-') or path.suffix not in ('.motion', '.zip'):
            continue
        match = re.search(r'--(.+)-v([0-9][\w.-]*)\.(motion|zip)$', path.name)
        if not match:
            continue
        slug, version = match[1], match[2]
        row = next((r for r in items if (r.get('website') or '').rstrip('/').split('/')[-1] == slug and not (slug == '385712' and r['orientation'] == 'portrait')), None)
        if not row:
            errors.append({'file': path.name, 'reason': 'not-in-catalog'})
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if len(names) > 100000 or sum(i.file_size for i in archive.infolist()) > 4_000_000_000:
                    raise ValueError('archive-limit')
                if any('..' in n.replace('\\', '/').split('/') or n.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', n) for n in names):
                    raise ValueError('unsafe-entry')
                if archive.testzip():
                    raise ValueError('crc-failed')
                if 'motion.json' not in names:
                    raise ValueError('missing-motion-manifest')
                manifest = json.loads(archive.read('motion.json'))
                if manifest.get('packageProfile') != 'personal-download-v1':
                    raise ValueError('unexpected-package-profile')
                # Verify protected content without decrypting it or reading personal credentials.
                content = manifest.get('content', {})
                content_path = content.get('path')
                if content_path not in names or hashlib.sha256(archive.read(content_path)).hexdigest() != content.get('sha256'):
                    raise ValueError('content-hash-mismatch')
            with path.open('rb') as f:
                sha = hashlib.file_digest(f, 'sha256').hexdigest()
            target = VAULT/'archives'/row['key']/version/(sha+'.motion')
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(path, target)
            with target.open('rb') as f:
                if hashlib.file_digest(f, 'sha256').hexdigest() != sha:
                    raise ValueError('copy-hash-mismatch')
            record = {'key': row['key'], 'title': row['title'], 'orientation': row['orientation'],
                      'version': version, 'language': 'zh', 'archive': target.relative_to(VAULT).as_posix(),
                      'sha256': sha, 'bytes': path.stat().st_size, 'importedAt': time.time(),
                      'containsAccountKey': True, 'archiveChecked': True, 'contentHashVerified': True,
                      'renderVerified': False, 'website': row['website'],
                      'licenseNotes': sources.get(path.resolve(), {}).get('license', result.get(sha, {}).get('licenseNotes', []))}
            record['supportedKeys'] = [row['key']]
            # Official 0.2.31 guide declares Chinese landscape and portrait in one original.
            if slug == '385712' and tuple(int(n) for n in re.findall(r'\d+', version)) >= (0, 2, 31):
                record['supportedKeys'].append('385712-portrait')
                record['compatibilityNote'] = 'Official 0.2.31 page: Chinese landscape and portrait; English portrait B1-B4 only'
            result[sha] = record
            imported.append(row['key'])
        except Exception as error:
            errors.append({'file': path.name, 'reason': type(error).__name__+': '+str(error)[:100]})
    records = list(result.values())
    save(VAULT/'index.json', {'schema': 'suojian.personal-motion-vault/1', 'localOnly': True,
                             'authorization': '2026-10-22 user explicitly allows personal packages containing Key',
                             'items': records})
    # This index is safe for local UI: no Key, account id, protected content or original download links.
    visible = []
    for p in records:
        for key in p.get('supportedKeys', [p['key']]):
            row = next(r for r in items if r['key'] == key)
            visible.append({**{k: p[k] for k in ('version', 'language', 'sha256', 'bytes', 'archiveChecked', 'renderVerified', 'licenseNotes')},
                            **{k: row[k] for k in ('key', 'title', 'orientation')}})
    availability = {'schema': 'suojian.personal-motion-availability/1', 'updatedAt': time.time(),
                    'authorized': True, 'items': visible}
    save(LIBRARY/'personal-availability.json', availability)
    import importlib.util
    spec = importlib.util.spec_from_file_location('motiongit_public_sync', SKILL/'scripts/sync_motiongit.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_gallery(catalog)
    return {'personalArchives': len(records), 'templateFormats': len({p['key'] for p in visible}),
            'bytes': sum(p['bytes'] for p in records), 'imported': sorted(set(imported)), 'errors': errors}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--authorized-personal', action='store_true', required=True)
    p.add_argument('--downloads', type=Path, default=Path.home()/'Downloads')
    p.add_argument('--ledger', type=Path)
    args = p.parse_args()
    print(json.dumps(run(args.downloads, read(args.ledger, []) if args.ledger else None), ensure_ascii=False))
