import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from workbench.production import Store
from workbench.vibe_storyboard import Storyboards, image_prompt
from workbench.storyboard_export import export_storyboard


class StoryboardExportTests(unittest.TestCase):
    def test_reference_limit_and_confirmed_unmodified_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'outputs').mkdir()
            images=[]
            for i in range(3):
                path=root/'outputs'/f'{i}.png'
                Image.new('RGB',(200,320),(40*i,120,180)).save(path)
                images.append(str(path))
            duplicate=root/'outputs/duplicate.png';duplicate.write_bytes(Path(images[0]).read_bytes())
            refs=images+[str(duplicate)]
            store=Store(root);task=store.create({'title':'测试','script':'完整脚本','materials':refs,
                'creativeRoute':'storyboard','referenceGroups':{'scene':refs,'other':[]}})
            api=Storyboards(root);key={'expectedRevision':1,'inputHash':task['inputHash']}
            shots=[{'id':f's{i}','start':i,'end':i+1,'onscreenText':'重点中文 123'} for i in range(8)]
            state=api.create(task['id'],{**key,'artDirection':'手绘','shots':shots})
            assigned=[s['sceneReference'] for s in state['shots']]
            self.assertEqual([assigned.count(p) for p in images],[2,2,2])
            self.assertEqual(assigned.count(None),2)
            self.assertIn(images[0],image_prompt(state['artDirection'],state['shots'][0]))
            with self.assertRaisesRegex(ValueError,'确认'):
                export_storyboard(root,task['id'],'overview')
            for shot in state['shots']:
                state=api.mutate(task['id'],'board',{**key,'expectedSerial':state['serial'],
                    'shotId':shot['id'],'path':images[0]})
            state=api.mutate(task['id'],'confirm_all',{**key,'expectedSerial':state['serial']})
            overview,typ,_=export_storyboard(root,task['id'],'overview')
            self.assertEqual(typ,'image/png')
            with Image.open(io.BytesIO(overview)) as image:
                self.assertGreater(image.width,200)
                self.assertEqual(image.getpixel((100,612)),(0,120,180))
            archive,typ,_=export_storyboard(root,task['id'],'zip')
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                self.assertEqual(z.namelist(),[f's{i}.png' for i in range(8)])
                for name in z.namelist():
                    self.assertEqual(z.read(name),Path(images[0]).read_bytes())
                    with Image.open(io.BytesIO(z.read(name))) as image:
                        self.assertEqual(image.size,(200,320))
            Path(state['shots'][0]['boards'][0]['file']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'变化'):
                export_storyboard(root,task['id'],'zip')
