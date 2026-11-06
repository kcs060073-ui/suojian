"""Move a visual style between local installations without exposing its source task."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from workbench.visual_styles import StyleLibrary

MAX_IMAGE = 30 * 1024 * 1024
MAX_ARCHIVE = 200 * 1024 * 1024
FORMAT = 'suojian-style/1'


def export_style(root, style_id):
    style = StyleLibrary(root).get(style_id)
    references = style.get('references', [])
    if not references or len(references) > 30:
        raise ValueError('模板没有可导出的参考图')
    manifest = {'format': FORMAT, 'name': style['name'], 'description': style['description'], 'images': []}
    data = BytesIO()
    total = 0
    with ZipFile(data, 'w', ZIP_DEFLATED) as archive:
        for index, ref in enumerate(references, 1):
            path = Path(ref['file'])
            if not path.is_file() or path.stat().st_size > MAX_IMAGE:
                raise ValueError('参考图不存在或超过 30 MB：' + path.name)
            content = path.read_bytes()
            total += len(content)
            if total > MAX_ARCHIVE:
                raise ValueError('参考图总大小超过 200 MB')
            digest = sha256(content).hexdigest()
            if digest != ref['sha256']:
                raise ValueError('参考图已变化，请重新保存模板')
            entry = f'images/{index:02d}{path.suffix.lower()}'
            archive.writestr(entry, content)
            manifest['images'].append({'entry': entry, 'sha256': digest})
        archive.writestr('style.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
    if data.tell() > MAX_ARCHIVE:
        raise ValueError('模板压缩包超过 200 MB')
    return data.getvalue()


def import_style(root, data):
    if not 0 < len(data) <= MAX_ARCHIVE:
        raise ValueError('请选择 200 MB 以内的模板压缩包')
    try:
        archive = ZipFile(BytesIO(data))
    except BadZipFile as error:
        raise ValueError('模板压缩包无法读取') from error
    with archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or names.count('style.json') != 1:
            raise ValueError('模板压缩包内容不正确')
        manifest_info = archive.getinfo('style.json')
        if manifest_info.file_size > 1024 * 1024:
            raise ValueError('模板说明过大')
        try:
            manifest = json.loads(archive.read(manifest_info))
        except (ValueError, UnicodeError) as error:
            raise ValueError('模板说明无法读取') from error
        images = manifest.get('images') if isinstance(manifest, dict) else None
        if not isinstance(manifest, dict) or manifest.get('format') != FORMAT or not isinstance(images, list) or not 1 <= len(images) <= 30:
            raise ValueError('这不是有效的溯剪风格模板包')
        expected = {'style.json'}
        for i, item in enumerate(images, 1):
            entry = item.get('entry', '') if isinstance(item, dict) else ''
            suffix = Path(entry).suffix.lower()
            if suffix not in ('.png', '.jpg', '.jpeg', '.webp') or entry != f'images/{i:02d}{suffix}' or entry not in names:
                raise ValueError('参考图片清单不正确')
            expected.add(entry)
            if archive.getinfo(entry).file_size > MAX_IMAGE:
                raise ValueError('单张参考图超过 30 MB')
        if set(names) != expected or sum(x.file_size for x in archive.infolist()) > MAX_ARCHIVE:
            raise ValueError('模板压缩包包含多余文件或内容过大')
        work = Path(root).resolve() / 'work' / 'production'
        work.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='style-import-', dir=work) as temporary:
            folder = Path(temporary).resolve()
            if folder.parent != work.resolve():
                raise ValueError('临时目录无效')
            paths = []
            for item in images:
                content = archive.read(item['entry'])
                if sha256(content).hexdigest() != item.get('sha256'):
                    raise ValueError('参考图片校验失败')
                target = folder / Path(item['entry']).name
                with target.open('xb') as stream:
                    stream.write(content)
                paths.append(str(target))
            return StyleLibrary(root).create({'name': manifest.get('name', ''),
                                              'description': manifest.get('description', ''),
                                              'images': paths})
