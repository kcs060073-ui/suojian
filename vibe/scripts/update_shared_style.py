"""Add a reviewed style package to the repository's shared template library."""
import argparse
import json
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.style_transfer import import_style


def update_shared_style(root, slug, package):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', slug):
        raise ValueError('模板编号只支持小写英文字母、数字和短横线')
    root = Path(root).resolve()
    folder = root / 'work' / 'shared-styles' / slug
    index = folder / 'index.json'
    current = json.loads(index.read_text(encoding='utf-8'))['latest'] if index.exists() else 0
    with TemporaryDirectory(prefix='suojian-style-verify-') as temporary:
        style = import_style(Path(temporary), Path(package).read_bytes())
        version = current + 1
        destination = folder / f'v{version:03d}'
        destination.mkdir(parents=True, exist_ok=False)
        refs = []
        for i, ref in enumerate(style['references'], 1):
            source = Path(ref['file'])
            name = f'{i:02d}{source.suffix.lower()}'
            with source.open('rb') as src, (destination / name).open('xb') as dst:
                shutil.copyfileobj(src, dst)
            refs.append({'file': name, 'sha256': ref['sha256'], 'name': name})
        shared = {'schema': 'suojian.visual-style/1', 'style_id': 'shared-'+slug,
                  'name': style['name'], 'description': style['description'], 'version': version,
                  'references': refs, 'createdAt': time.time(), 'source': None, 'sourceShots': [],
                  'acceptance': '项目共享风格参考；实际使用效果需审看'}
        (destination / 'style.json').write_text(json.dumps(shared, ensure_ascii=False, indent=2), encoding='utf-8')
        index.write_text(json.dumps({'latest': version}), encoding='utf-8')
    return shared


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='把溯剪风格模板包加入项目共享模板')
    parser.add_argument('package', type=Path)
    parser.add_argument('--id', required=True, help='稳定编号，仅小写字母、数字和短横线')
    args = parser.parse_args()
    result = update_shared_style(ROOT, args.id, args.package)
    print(f"已准备公共模板 {result['name']} v{result['version']}。提交项目更新后其他电脑可使用。")
