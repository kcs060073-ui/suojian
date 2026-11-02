"""Maintain a key-free, immutable MotionGit library. Never execute downloaded code."""
from __future__ import annotations
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SKILL = Path(__file__).resolve().parents[1]
ROOT = SKILL / 'assets' / 'motiongit'
CATALOG = 'https://raw.githubusercontent.com/motiontemplates/all/main/catalog.json'
ALLOWED = {'raw.githubusercontent.com', 'motiontemplates.github.io', 'motiongit.com',
           'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
AGENT = 'SuoJian-KeyFreeLibrary/1.0'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def read(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def allowed(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.hostname not in ALLOWED or p.username or p.password:
        raise ValueError('Source is not an approved public HTTPS host')
    if p.query:
        raise ValueError('Credential-bearing or signed URLs are not accepted by public sync')


class PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(PublicRedirects)


def fetch(url, destination, previous=None, limit=10_000_000):
    allowed(url)
    headers = {'User-Agent': AGENT}
    if previous and destination.is_file():
        if previous.get('etag'):
            headers['If-None-Match'] = previous['etag']
        if previous.get('modified'):
            headers['If-Modified-Since'] = previous['modified']
    req = urllib.request.Request(url, headers=headers)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + '.partial')
    try:
        with OPENER.open(req, timeout=35) as response:
            allowed(response.url)
            if int(response.headers.get('Content-Length', 0)) > limit:
                raise ValueError('Public file exceeds download limit')
            total = 0
            with tmp.open('wb') as output:
                while chunk := response.read(256 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError('Public file exceeds download limit')
                    output.write(chunk)
            tmp.replace(destination)
            return {'url': url, 'etag': response.headers.get('ETag'),
                    'modified': response.headers.get('Last-Modified'), 'checked': now(),
                    'sha256': digest(destination), 'bytes': total, 'changed': True}
    except urllib.error.HTTPError as error:
        if error.code == 304 and previous and destination.is_file():
            if digest(destination) != previous.get('sha256'):
                return fetch(url, destination, None, limit)
            return {**previous, 'checked': now(), 'changed': False}
        raise
    finally:
        if tmp.exists():
            tmp.unlink()


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.downloads = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get('href', '')
        if tag == 'a' and '/download/' in href:
            self.downloads.append(href)


def archive_check(path):
    with zipfile.ZipFile(path) as package:
        names = package.namelist()
        if len(names) > 100000 or sum(i.file_size for i in package.infolist()) > 4_000_000_000:
            raise ValueError('Archive expansion exceeds library budget')
        for name in names:
            normalized = name.replace('\\', '/').lower().strip('/')
            parts = normalized.split('/')
            if 'personal' in parts or normalized.endswith('core.enc') or normalized.endswith('content/release.bin'):
                raise ValueError('Personal/protected package rejected: user allows key-free public packages only')
            if '..' in parts or name.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', name):
                raise ValueError('Unsafe archive entry')
        if package.testzip():
            raise ValueError('Archive CRC check failed')
        metadata = {}
        for name in ('motion.json', 'guide.json'):
            if name in names and package.getinfo(name).file_size < 2_000_000:
                obj = json.loads(package.read(name))
                if isinstance(obj, dict):
                    for key in ('version', 'name', 'id'):
                        if isinstance(obj.get(key), str):
                            metadata[key] = obj[key][:160]
        return {'archiveChecked': True, 'personalMarkersFound': False, 'entries': len(names),
                'declared': metadata, 'renderVerified': False}


def rows_from(catalog):
    rows = []
    for item in catalog.get('templates', []):
        rows.append(item)
        rows.extend(item.get('additionalDemos', []))
    rows.extend(catalog.get('previews', []))
    result, seen = [], set()
    for item in rows:
        key = str(item.get('repositoryId') or item.get('previewId') or item.get('templateId'))
        key += '-' + str(item.get('orientation', 'landscape'))
        if key in seen:
            continue
        if not re.fullmatch('[A-Za-z0-9-]+', key):
            raise ValueError('Unexpected catalog identifier')
        seen.add(key)
        title = item.get('title', {})
        summary = item.get('summary', {})
        result.append({'key': key, 'title': title.get('zh', '') if isinstance(title, dict) else str(title),
                       'summary': summary.get('zh', '') if isinstance(summary, dict) else str(summary),
                       'website': item.get('website'), 'repository': item.get('repository'),
                       'version': item.get('version'), 'width': int(item.get('width') or 0), 'height': int(item.get('height') or 0),
                       'fps': float(item.get('fps') or 0), 'orientation': item.get('orientation'),
                       'poster': item.get('poster') or item.get('preview'), 'demo': item.get('demo'),
                       'download': item.get('download', {}), 'packages': []})
    return result


def write_gallery(index):
    index = json.loads(json.dumps(index))
    private = read(ROOT/'personal-availability.json', {})
    for r in index['items']:
        candidates = [p for p in private.get('items', []) if p['key'] == r['key']] if private.get('authorized') else []
        if candidates:
            latest = max(candidates, key=lambda p: tuple(int(n) for n in re.findall(r'\d+', p['version'])))
            if not r['packages'] or tuple(int(n) for n in re.findall(r'\d+', latest['version'])) > tuple(int(n) for n in re.findall(r'\d+', r.get('version') or '0')):
                r['packages'] = [latest]
                r['personal'] = True
                r['version'] = latest['version']
    e = html.escape
    cards = []
    for r in index['items']:
        state = ('已下载个人包 · 仅本机使用' if r.get('personal') else '已下载公开包 · 待制作验证') if r['packages'] else ('尚未开放下载' if r['download'].get('type') == 'coming-soon' else '尚未下载或官网暂不可下载')
        image = f'<img loading="lazy" src="{e(r["posterLocal"])}" alt="{e(r["title"])}">' if r.get('posterLocal') else '<div class="noimage">暂无本地预览</div>'
        is_https = lambda u: isinstance(u, str) and urllib.parse.urlsplit(u).scheme == 'https'
        controls = f'<a href="{e(r["website"])}" target="_blank" rel="noreferrer">官网详情</a>' if is_https(r.get('website')) else ''
        if is_https(r.get('demo')):
            controls += f' <a href="{e(r["demo"])}" target="_blank" rel="noreferrer">查看 Demo</a>'
        pack = ''.join(f'<div class="package">{e(p["language"])} · {p["bytes"]/1048576:.1f} MB · {e(p["sha256"][:10])}</div>' for p in r['packages'])
        prompt = f'使用园区 Vibe 本地模板 {r["title"]}（{r["key"]}）制作。读取本地库实际包和指南，核对许可，按我的脚本选择镜头、音乐与音效，并联网补充合适插图或动画。'
        button = f'<button data-prompt="{e(prompt, quote=True)}">选用并显示制作指令</button>' if r['packages'] else ''
        cards.append(f'<article data-ready="{bool(r["packages"]).__str__().lower()}" data-name="{e(r["title"])}">{image}<div class="body"><small>{e(state)}</small><h2>{e(r["title"])}</h2><p>{e(r["summary"])}</p><p>{r["width"]} × {r["height"]} · {r["fps"]} fps</p>{pack}<div class="actions">{controls}</div>{button}</div></article>')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>园区 Vibe · 本地公开模板库</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#203c36;font:15px system-ui,'Microsoft YaHei',sans-serif}main{max-width:1250px;margin:auto;padding:45px 30px}h1{font-size:34px;margin:12px 0}header p{color:#66776d;line-height:1.8;max-width:850px}.bar{display:flex;gap:12px;margin:26px 0}input,select,button,textarea{font:inherit;border:1px solid #cdd5cb;border-radius:9px;padding:11px;background:white;color:inherit}input{flex:1}button{background:#25493d;color:white;cursor:pointer;margin-top:12px}#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:22px}article{background:white;border-radius:16px;overflow:hidden;border:1px solid #dce0d7}article img{width:100%;height:190px;object-fit:contain;background:#e6e8df}.noimage{height:190px;display:grid;place-items:center;background:#e6e8df}.body{padding:20px}h2{font-size:21px;margin:12px 0}.body p{font-size:14px;line-height:1.7;color:#67766a}small,.package{font-size:12px;color:#8b6942}a{color:#426c55;font-size:13px}.actions{margin-top:15px}textarea{display:block;width:100%;height:110px;margin-bottom:20px}#chosen[hidden]{display:none}</style>
<main><header><small>SUOJIAN · KEY-FREE LOCAL LIBRARY</small><h1>选好风格，交给对话制作。</h1><p>全部目录保留；只下载不带账户 Key 的公开包。已下载表示原包和结构检查完成，实际制作仍需核实运行环境与审看预览。选择模板后，把制作指令连同脚本、素材交给对话明确启动。</p><p>__SUMMARY__</p></header><div class="bar"><input id="query" placeholder="搜索模板名称"><select id="scope"><option value="all">完整目录</option><option value="ready">已下载公开包</option></select></div><section id="chosen" hidden><p>已选择模板。填写脚本、园区素材和要求后交给对话启动。</p><textarea id="prompt"></textarea></section><section id="grid">__CARDS__</section></main>
<script>const q=document.querySelector('#query'),scope=document.querySelector('#scope');function filter(){for(const c of document.querySelectorAll('article'))c.hidden=!c.dataset.name.toLowerCase().includes(q.value.toLowerCase())||(scope.value==='ready'&&c.dataset.ready!=='true')}q.oninput=scope.onchange=filter;for(const b of document.querySelectorAll('[data-prompt]'))b.onclick=()=>{document.querySelector('#chosen').hidden=false;document.querySelector('#prompt').value=b.dataset.prompt;document.querySelector('#chosen').scrollIntoView({behavior:'smooth',block:'center'})};</script></html>'''
    document = document.replace('园区 Vibe · 本地公开模板库', '园区 Vibe · 本地模板库').replace('SUOJIAN · KEY-FREE LOCAL LIBRARY', 'SUOJIAN · LOCAL TEMPLATE LIBRARY').replace('全部目录保留；只下载不带账户 Key 的公开包。', '全部目录保留；公开包与已授权个人包分开保存，个人包仅本机使用。').replace('已下载公开包</option>', '已下载本地包</option>')
    summary = f'目录 {len(index["items"])} 项 · {sum(bool(r["packages"]) for r in index["items"])} 个模板画幅已下载 · 其中 {sum(bool(r.get("personal")) for r in index["items"])} 个画幅为个人包 · 检查时间 {index["checkedAt"]}'
    (ROOT / 'catalogue.html').write_text(document.replace('__SUMMARY__', e(summary)).replace('__CARDS__', '\n'.join(cards)), encoding='utf-8')


def sync(offline=False, check_only=False, no_images=False):
    ROOT.mkdir(parents=True, exist_ok=True)
    previous = read(ROOT / 'library-index.json', {'items': [], 'http': {}})
    http = previous.get('http', {})
    errors, changes = [], []
    source = ROOT / 'upstream-catalog.json'
    if not offline:
        http[CATALOG] = fetch(CATALOG, source, http.get(CATALOG), 10_000_000)
    catalog = read(source, None)
    if not catalog or catalog.get('format') != 'motiongit.catalog/1':
        raise ValueError('Official catalog is missing or has an unknown format')
    items = rows_from(catalog)
    old = {r['key']: r for r in previous.get('items', [])}
    previous_packages = {p['url']: p for r in previous.get('items', []) for p in r.get('packages', [])}
    for r in items:
        older = old.get(r['key'], {})
        r['packages'] = [p for p in older.get('packages', []) if (ROOT / p['path']).is_file()]
        if r['key'] not in old:
            changes.append({'kind': 'catalog-added', 'template': r['key']})
        if r['download'].get('type') == 'direct-package':
            for language, url in r['download'].get('urls', {}).items():
                if not re.fullmatch('[a-z]{2}(?:-[A-Z]{2})?', language):
                    raise ValueError('Unexpected language identifier')
                if not urllib.parse.urlsplit(url).path.endswith('.motion'):
                    continue
                if offline or check_only:
                    continue
                try:
                    staging = ROOT / 'cache' / f'{r["key"]}-{language}.motion'
                    info = fetch(url, staging, http.get(url), 256_000_000)
                    checked = archive_check(staging)
                    target = ROOT / 'packages' / r['key'] / language / (info['sha256'] + '.motion')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        try:
                            os.link(staging, target)
                        except OSError:
                            import shutil
                            shutil.copyfile(staging, target)
                    row = {'language': language, 'url': url, 'path': target.relative_to(ROOT).as_posix(),
                           'sha256': info['sha256'], 'bytes': info['bytes'], 'downloadedAt': now(), **checked}
                    if url not in previous_packages or previous_packages[url]['sha256'] != row['sha256']:
                        changes.append({'kind': 'package-downloaded', 'template': r['key'], 'language': language})
                    r['packages'] = [p for p in r['packages'] if p['url'] != url] + [row]
                    http[url] = info
                    print('Public package checked: ' + r['key'] + ' / ' + language, flush=True)
                except Exception as error:
                    errors.append({'template': r['key'], 'stage': 'public-package', 'message': str(error)[:300]})
    if not offline and not no_images:
        def poster(r):
            url = r.get('poster')
            if not url:
                return None
            suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
            if suffix not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
                return None
            path = ROOT / 'posters' / (r['key'] + suffix)
            result = fetch(url, path, http.get(url), 12_000_000)
            return url, result, path.relative_to(ROOT).as_posix()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(poster, r): r for r in items}
            for future in concurrent.futures.as_completed(futures):
                r = futures[future]
                try:
                    result = future.result()
                    if result:
                        url, meta, local = result
                        http[url] = meta
                        r['posterLocal'] = local
                except Exception as error:
                    errors.append({'template': r['key'], 'stage': 'poster', 'message': str(error)[:300]})
    for r in items:
        if not r.get('posterLocal'):
            old_poster = old.get(r['key'], {}).get('posterLocal')
            if old_poster and (ROOT / old_poster).is_file():
                r['posterLocal'] = old_poster
        r['status'] = 'public-downloaded-unrendered' if r['packages'] else ('coming-soon' if r['download'].get('type') == 'coming-soon' else 'personal-download-excluded')
    index = {'schema': 'suojian.key-free-motion-library/1', 'checkedAt': now(), 'source': CATALOG,
             'policy': {'personalPackages': False, 'executeDownloads': False, 'retainOldVersions': True},
             'items': items, 'http': http, 'changes': changes, 'errors': errors,
             'summary': {'catalogItems': len(items), 'localTemplateCount': sum(bool(r['packages']) for r in items),
                         'packageCount': sum(len(r['packages']) for r in items),
                         'comingSoon': sum(r['status'] == 'coming-soon' for r in items)}}
    save(ROOT / 'library-index.json', index)
    save(ROOT / 'sync-latest.json', {k: index[k] for k in ('checkedAt', 'summary', 'changes', 'errors')})
    write_gallery(index)
    return {k: index[k] for k in ('checkedAt', 'summary', 'changes', 'errors')}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--offline', action='store_true')
    p.add_argument('--check-only', action='store_true')
    p.add_argument('--no-images', action='store_true')
    args = p.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = ROOT / 'sync.lock'
    try:
        with lock.open('x', encoding='ascii') as marker:
            marker.write(str(os.getpid()))
    except FileExistsError:
        raise SystemExit('A library sync is already active; preserve its lock and resume after it finishes.')
    try:
        result = sync(args.offline, args.check_only, args.no_images)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
