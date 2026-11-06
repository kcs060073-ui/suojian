import io
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from workbench.style_transfer import export_style, import_style
from workbench.visual_styles import StyleLibrary
from workbench.production import Store


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

    def test_shared_style_is_available_in_new_checkout_and_keeps_old_task_snapshot(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'update_shared_style.py'
        spec = importlib.util.spec_from_file_location('update_shared_style', script)
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as project_dir:
            source, project = Path(source_dir), Path(project_dir)
            (source / 'outputs').mkdir()
            image = source / 'outputs' / 'art.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nfirst')
            library = StyleLibrary(source)
            original = library.create({'name': '公共手绘', 'description': '灰蓝线稿', 'images': [str(image)]})
            package = source / 'package.zip'
            package.write_bytes(export_style(source, original['style_id']))
            updater.update_shared_style(project, 'handdrawn', package)
            shared = StyleLibrary(project).get('shared-handdrawn')
            self.assertEqual(shared['version'], 1)
            self.assertEqual(StyleLibrary(project).list()[0]['style_id'], 'shared-handdrawn')
            self.assertTrue(Path(shared['references'][0]['file']).is_file())
            store = Store(project)
            task = store.create({'title': '测试', 'script': '脚本', 'creativeRoute': 'storyboard',
                                 'materials': [], 'visualStyleKey': 'shared-handdrawn'})
            image.write_bytes(b'\x89PNG\r\n\x1a\nsecond')
            updated = library.revise(original['style_id'], {'expectedVersion': 1, 'images': [str(image)]})
            package.write_bytes(export_style(source, updated['style_id']))
            updater.update_shared_style(project, 'handdrawn', package)
            self.assertEqual(StyleLibrary(project).get('shared-handdrawn')['version'], 2)
            snapshot = store.get(task['id'])['input']['visualStyle']
            self.assertEqual(snapshot['version'], 1)
            self.assertTrue(Path(snapshot['references'][0]['file']).is_file())


if __name__ == '__main__':
    unittest.main()
