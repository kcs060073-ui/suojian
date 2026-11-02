import json
import tempfile
import unittest
from pathlib import Path
from flask import Flask
from suo_jian.ui.studio_home import project_index, register_studio_routes


class StudioHomeTests(unittest.TestCase):
    def test_three_workflows_and_registered_draft_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def write(name, value):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding='utf-8')
            write('1234abcd/edit_blueprint.json', {'source': {'file_name': '1234abcd_reference.mp4'}})
            write('1234abcd/execution.json', {'status': 'exported'})
            write('1234abcd/presenter/last-render.json', {'id': 'render-0123456789'})
            write('1234abcd/presenter/render-0123456789/draft.json', {'display_name': 'Reviewed version', 'registration': {'registered': True}, 'folder': 'PRIVATE_LOCAL_PATH'})
            write('1234abcd/motion_explainer/project.json', {'title': 'test'})
            rows = project_index(root)
            self.assertEqual({r['mode'] for r in rows}, {'feed', 'presenter', 'vibe'})
            p = next(r for r in rows if r['mode'] == 'presenter')
            self.assertEqual(p['title'], 'Reviewed version')
            self.assertEqual(p['status_label'], '草稿已保存')
            self.assertEqual(p['href'], '/presenter?job=1234abcd')
            self.assertNotIn('PRIVATE_LOCAL_PATH', json.dumps(rows))
            self.assertEqual(next(r for r in rows if r['mode'] == 'feed')['title'], 'reference.mp4')

    def test_common_analysis_does_not_create_fake_feed_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / '1234abcd'; (folder / 'presenter').mkdir(parents=True)
            (folder / 'edit_blueprint.json').write_text('{"source":null}')
            (folder / 'presenter/plan.json').write_text('{}')
            self.assertEqual([r['mode'] for r in project_index(Path(tmp))], ['presenter'])

    def test_corrupt_history_and_non_task_directories_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'not-a-task').mkdir()
            (root / '1234abcd').mkdir()
            (root / '1234abcd/edit_blueprint.json').write_text('{invalid')
            self.assertEqual(project_index(root), [])
            app = Flask(__name__); register_studio_routes(app, root)
            with app.test_client() as client:
                self.assertEqual(client.get('/api/studio/projects').json, {'projects': []})
                self.assertEqual(client.get('/api/studio/projects', base_url='http://untrusted.example').status_code, 403)
