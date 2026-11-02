import copy
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from workbench.production import Store, Conflict, normalize, read, save


class ProductionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parents[1]/'work/qa/production-tests'
        self.root = self.parent/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.store = Store(self.root)
        self.input = {'title': '嘉定测试任务', 'script': '保留已确认事实，用实拍说明直接签约。',
                      'materials': ['C:/park/footage'], 'format': 'landscape', 'styleIntent': 'vary'}

    def tearDown(self):
        resolved = self.root.resolve()
        assert resolved.parent == self.parent.resolve()
        shutil.rmtree(resolved)

    def test_save_does_not_start_and_input_is_immutable(self):
        task = self.store.create(self.input)
        original = Path(task['inputFile']).read_bytes()
        newer = {**self.input, 'requirements': '少用卡片'}
        second = self.store.revise(task['id'], {'expectedRevision': 1, 'input': newer})
        self.assertEqual(second['status'], 'awaiting_dialogue')
        self.assertEqual(second['revision'], 2)
        self.assertEqual(original, Path(task['inputFile']).read_bytes())
        self.assertEqual(second['input']['autoSelection']['seed'], task['input']['autoSelection']['seed'])

    def test_stale_revision_cannot_replace_current_input(self):
        task = self.store.create(self.input)
        self.store.revise(task['id'], {'expectedRevision': 1, 'input': self.input})
        with self.assertRaises(Conflict):
            self.store.revise(task['id'], {'expectedRevision': 1, 'input': {**self.input, 'script': '过期修改'}})
        self.assertEqual(self.store.get(task['id'])['input']['script'], self.input['script'])

    def test_browser_cannot_invent_execution_and_no_output_cannot_complete(self):
        task = self.store.create(self.input)
        with self.assertRaises(ValueError):
            self.store.progress(task['id'], {'expectedRevision': 1, 'status': 'rendering', 'note': '未由对话启动'})
        with self.assertRaises(ValueError):
            self.store.progress(task['id'], {'expectedRevision': 1, 'executor': 'dialogue', 'status': 'completed', 'note': '没有视频'})
        self.assertEqual(self.store.get(task['id'])['status'], 'awaiting_dialogue')

    def test_input_cannot_change_during_active_execution(self):
        task = self.store.create(self.input)
        self.store.progress(task['id'], {'expectedRevision': 1, 'executor': 'dialogue', 'status': 'planning', 'note': '执行对话已启动'})
        with self.assertRaises(Conflict):
            self.store.revise(task['id'], {'expectedRevision': 1, 'input': self.input})

    def test_redraw_changes_selection_but_keeps_source_content(self):
        with patch('workbench.production.secrets.randbits', return_value=42):
            task = self.store.create(self.input)
        with patch('workbench.production.secrets.randbits', return_value=99):
            next_task = self.store.revise(task['id'], {'expectedRevision': 1, 'input': {**self.input, 'redraw': True}})
        self.assertEqual(next_task['input']['script'], task['input']['script'])
        self.assertEqual(next_task['input']['materials'], task['input']['materials'])
        self.assertEqual(next_task['input']['autoSelection']['seed'], 99)

    def test_template_hash_stays_pinned_across_library_update(self):
        library = self.root/'library'
        package = library/'packages/old.motion'
        package.parent.mkdir(parents=True)
        package.write_bytes(b'public-template-fixture')
        sha = hashlib.sha256(package.read_bytes()).hexdigest()
        catalog = {'items': [{'key': 'test-landscape', 'title': '测试模板', 'version': '1', 'orientation': 'landscape',
                              'packages': [{'path': 'packages/old.motion', 'sha256': sha, 'language': 'zh', 'personalMarkersFound': False}]}]}
        save(library/'library-index.json', catalog)
        with patch('workbench.production.library_root', return_value=library):
            task = self.store.create({**self.input, 'templateKey': 'test-landscape'})
            save(library/'library-index.json', {'items': []})
            next_task = self.store.revise(task['id'], {'expectedRevision': 1, 'input': {**self.input, 'templateKey': 'test-landscape'}})
        self.assertEqual(next_task['input']['template']['sha256'], sha)
        self.assertEqual(next_task['input']['template']['version'], '1')

    def test_personal_or_wrong_aspect_template_is_not_accepted(self):
        library = self.root/'library'
        save(library/'library-index.json', {'items': [{'key': 'private', 'orientation': 'landscape', 'packages': [
            {'language': 'zh', 'personalMarkersFound': True}]}]})
        with patch('workbench.production.library_root', return_value=library):
            with self.assertRaises(ValueError):
                self.store.create({**self.input, 'templateKey': 'private'})
            with self.assertRaises(ValueError):
                self.store.create({**self.input, 'format': 'portrait', 'templateKey': 'private'})

    def test_delivery_rejects_missing_or_external_files_without_state_change(self):
        task = self.store.create(self.input)
        outside = self.root/'not-a-delivery.mp4'
        outside.write_bytes(b'not-video')
        with self.assertRaises(ValueError):
            self.store.deliver(task['id'], {'expectedRevision': 1, 'executor': 'dialogue', 'video': str(outside)})
        self.assertEqual(self.store.get(task['id'])['versions'], [])
        self.assertEqual(self.store.get(task['id'])['status'], 'awaiting_dialogue')


if __name__ == '__main__':
    unittest.main()
