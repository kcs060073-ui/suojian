import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from workbench.style_transfer import export_style, import_style
from workbench.visual_styles import StyleLibrary


class StyleTransferTests(unittest.TestCase):
    def test_round_trip_restores_reference_images_on_another_computer(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source, target = Path(source_dir), Path(target_dir)
            (source / 'outputs').mkdir()
            paths = []
            for i in range(2):
                path = source / 'outputs' / f'{i}.png'
                path.write_bytes(b'\x89PNG\r\n\x1a\nimage' + bytes([i]))
                paths.append(str(path))
            style = StyleLibrary(source).create({'name': '手绘风格', 'description': '灰蓝细线',
                                                  'images': paths, 'source': {'script': '私人脚本'}})
            package = export_style(source, style['style_id'])
            with ZipFile(io.BytesIO(package)) as archive:
                self.assertNotIn('私人脚本', archive.read('style.json').decode())
            imported = import_style(target, package)
            self.assertEqual((imported['name'], imported['description']), ('手绘风格', '灰蓝细线'))
            self.assertEqual([r['sha256'] for r in imported['references']], [r['sha256'] for r in style['references']])
            for ref in imported['references']:
                self.assertTrue(Path(ref['file']).is_relative_to(target))
                self.assertTrue(Path(ref['file']).is_file())

    def test_copied_style_library_repairs_old_absolute_paths(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source, target = Path(source_dir), Path(target_dir)
            (source / 'outputs').mkdir()
            image = source / 'outputs' / 'art.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
            style = StyleLibrary(source).create({'name': '线稿', 'images': [str(image)]})
            shutil.copytree(source / 'work', target / 'work')
            copied = StyleLibrary(target).get(style['style_id'])
            self.assertTrue(Path(copied['references'][0]['file']).is_relative_to(target))
            self.assertTrue(Path(copied['references'][0]['file']).is_file())
            self.assertTrue(export_style(target, style['style_id']))

    def test_rejects_unexpected_archive_content(self):
        with tempfile.TemporaryDirectory() as directory:
            data = io.BytesIO()
            with ZipFile(data, 'w') as archive:
                archive.writestr('style.json', json.dumps({'format': 'suojian-style/1', 'images': [{'entry': '../evil.png'}]}))
                archive.writestr('../evil.png', b'bad')
            with self.assertRaises(ValueError):
                import_style(Path(directory), data.getvalue())


if __name__ == '__main__':
    unittest.main()
