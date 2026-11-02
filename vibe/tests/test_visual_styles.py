import tempfile
import unittest
from pathlib import Path
from workbench.production import Store
from workbench.vibe_storyboard import Storyboards
from workbench.visual_styles import StyleLibrary


class VisualStyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.library=StyleLibrary(self.root)
        self.image=self.root/'outputs'/'art.png';self.image.parent.mkdir();self.image.write_bytes(b'\x89PNG\r\n\x1a\nexample')

    def test_template_snapshot_keeps_its_version(self):
        style=self.library.create({'name':'线稿','description':'黑色细线','images':[str(self.image)]})
        self.image.unlink()
        store=Store(self.root)
        task=store.create({'title':'新片','script':'新脚本','creativeRoute':'storyboard','materials':[],
                           'visualStyleKey':style['style_id']})
        self.assertEqual(task['input']['visualStyle']['version'],1)
        changed=self.library.revise(style['style_id'],{'expectedVersion':1,'description':'更细的线条'})
        self.assertEqual(changed['version'],2)
        self.assertEqual(store.get(task['id'])['input']['visualStyle']['description'],'黑色细线')
        self.assertTrue(Path(style['references'][0]['file']).exists())
        with self.assertRaises(ValueError):self.library.revise(style['style_id'],{'expectedVersion':1})

    def test_save_storyboard_copies_images_and_script(self):
        store=Store(self.root);task=store.create({'title':'示例','script':'原脚本','creativeRoute':'storyboard','materials':[]})
        boards=Storyboards(self.root);key={'expectedRevision':1,'inputHash':task['inputHash']}
        state=boards.create(task['id'],{**key,'artDirection':'黑白线稿','shots':[{'id':'01','start':0,'end':5,'subject':'厂房'}]})
        state=boards.mutate(task['id'],'board',{**key,'expectedSerial':state['serial'],'shotId':'01','path':str(self.image)})
        saved=self.library.from_storyboard(task['id'],{'name':'厂房示例'})
        self.assertEqual(saved['source']['script'],'原脚本')
        self.assertEqual(saved['sourceShots'][0]['description']['subject'],'厂房')
        self.assertEqual(saved['sourceShots'][0]['reviewStatus'],'pending')
        self.assertEqual(saved['references'][0]['sha256'],state['shots'][0]['boards'][0]['sha256'])

    def test_adding_images_creates_a_new_version_and_keeps_old_reference(self):
        original=self.library.create({'name':'线稿','description':'黑线','images':[str(self.image)]})
        second=self.root/'outputs'/'another.png';second.write_bytes(b'\x89PNG\r\n\x1a\nanother')
        revised=self.library.revise(original['style_id'],{'expectedVersion':1,
            'images':[original['references'][0]['file'],str(second)]})
        self.assertEqual(revised['version'],2)
        self.assertEqual(len(revised['references']),2)
        self.assertEqual(revised['references'][0]['sha256'],original['references'][0]['sha256'])
        self.assertEqual(len(self.library.get(original['style_id'],1)['references']),1)

    def test_relative_builtin_reference_resolves_from_workspace(self):
        saved=self.library.create({'name':'内置风格 · 补充参考','description':'细线',
                                   'images':['outputs/art.png',str(self.image)]})
        self.assertEqual(len(saved['references']),2)
        self.assertEqual(saved['references'][0]['sha256'],saved['references'][1]['sha256'])

    def test_rename_archive_restore_keep_existing_task_snapshot(self):
        style=self.library.create({'name':'旧名称','description':'线稿','images':[str(self.image)]})
        store=Store(self.root)
        task=store.create({'title':'任务','script':'原文','creativeRoute':'storyboard','materials':[],
                           'visualStyleKey':style['style_id']})
        renamed=self.library.revise(style['style_id'],{'expectedVersion':1,'name':'新名称'})
        self.assertEqual(renamed['name'],'新名称')
        self.assertEqual(store.get(task['id'])['input']['visualStyle']['name'],'旧名称')
        self.library.set_archived(style['style_id'],True,2)
        self.assertEqual(self.library.list(),[])
        self.assertEqual(self.library.list(archived=True)[0]['name'],'新名称')
        self.assertTrue(Path(task['input']['visualStyle']['references'][0]['file']).exists())
        with self.assertRaises(ValueError): self.library.set_archived(style['style_id'],False,1)
        self.library.set_archived(style['style_id'],False,2)
        self.assertEqual(self.library.list()[0]['name'],'新名称')
        self.library.set_archived('handdrawn-realistic-v1',True,1)
        self.assertTrue(self.library.builtin_archived())
        self.library.set_archived('handdrawn-realistic-v1',False,1)
        self.assertFalse(self.library.builtin_archived())

    def test_save_name_and_description_together_preserves_reference(self):
        original=self.library.create({'name':'原名称','description':'旧说明','images':[str(self.image)]})
        updated=self.library.revise(original['style_id'],{'expectedVersion':1,'name':'新名称','description':'新的视觉说明'})
        self.assertEqual((updated['name'],updated['description']),('新名称','新的视觉说明'))
        self.assertEqual(updated['references'][0]['sha256'],original['references'][0]['sha256'])
        self.assertEqual(self.library.get(original['style_id'],1)['name'],'原名称')

if __name__=='__main__': unittest.main()
