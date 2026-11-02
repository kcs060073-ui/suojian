import tempfile, unittest
from pathlib import Path
from workbench.production import Store, Conflict
from workbench.vibe_storyboard import Storyboards, image_prompt

class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root)
        self.task=self.store.create({'title':'测试','script':'一段话','materials':['test'],'format':'portrait'})
        self.api=Storyboards(self.root);self.key={'expectedRevision':1,'inputHash':self.task['inputHash']}
        self.state=self.api.create(self.task['id'],{**self.key,'artDirection':'白底黑线','shots':[{'id':'02','start':0,'end':7},{'id':'03','start':7,'end':14}]})
        self.img=self.root/'outputs/image.png';self.img.parent.mkdir();self.img.write_bytes(b'\x89PNG\r\n\x1a\ncontent')
        self.elem={'element_id':'parcel','action_type':'move','start_time':1,'duration':2,'motion_description':'沿路线',
                   'start_state':{},'end_state':{},'execution_method':'programmatic','prompt':'',
                   'required_assets':[],'overlay_requirements':[],'validation_rules':[]}
    def call(self,op,**kw):
        self.state=self.api.mutate(self.task['id'],op,{**self.key,'expectedSerial':self.state['serial'],**kw});return self.state
    def test_confirm_plan_and_invalidate_on_replacement(self):
        self.call('board',shotId='02',path=str(self.img));self.call('board',shotId='03',path=str(self.img))
        with self.assertRaises(ValueError):self.call('plan',shotId='02',elements=[self.elem])
        self.call('confirm_all')
        for name in ('02','03'):
            self.call('plan',shotId=name,elements=[self.elem]);p=next(x for x in self.state['shots'] if x['id']==name)['plans'][-1]
            self.call('confirm_plan',shotId=name,planSha256=p['sha256'])
        self.api.require_plans(self.task['id'])
        self.call('board',shotId='02',path=str(self.img))
        with self.assertRaises(ValueError):self.api.require_plans(self.task['id'])
        self.assertEqual(self.state['shots'][1]['plans'][0]['status'],'confirmed')
        self.assertEqual(len(self.state['shots'][0]['boards']),2)
    def test_conflicts_and_invalid_paths(self):
        with self.assertRaises(Conflict):self.api.mutate(self.task['id'],'confirm_all',{**self.key,'expectedSerial':0})
        outside=Path(self.tmp.name).parent/'outside.png';outside.write_bytes(self.img.read_bytes());self.addCleanup(outside.unlink,missing_ok=True)
        with self.assertRaises(ValueError):self.call('board',shotId='02',path=str(outside))
        with self.assertRaises(ValueError):self.api.require_plans(self.task['id'])
    def test_mode_gates_render_before_storyboard_exists(self):
        task=self.store.create({'title':'简笔','script':'一句话','materials':[],'format':'portrait','workflowMode':'stick-narrative'})
        with self.assertRaises(ValueError):self.store.progress(task['id'],{'executor':'dialogue','expectedRevision':1,'inputHash':task['inputHash'],'status':'rendering','note':'制作'})
        legacy=self.store.create({'title':'旧流程','script':'一句话','materials':['test'],'format':'portrait'})
        self.store.progress(legacy['id'],{'executor':'dialogue','expectedRevision':1,'inputHash':legacy['inputHash'],'status':'rendering','note':'旧流程'})

    def test_per_shot_confirmation_and_stale_hash(self):
        self.call('board',shotId='02',path=str(self.img))
        b=self.state['shots'][0]['boards'][0]
        with self.assertRaises(Conflict):self.call('confirm_board',shotId='02',boardSha256='wrong')
        self.call('confirm_board',shotId='02',boardSha256=b['sha256'])
        self.assertEqual(self.state['shots'][0]['boards'][0]['status'],'confirmed')
        with self.assertRaises(ValueError):self.api.require_plans(self.task['id'])
        self.assertEqual(self.state['shots'][1]['selectedBoard'],None)

    def test_prompt_carries_context_and_missing_data(self):
        prompt=image_prompt('白底黑线',{'id':'02','start':0,'end':7,'description':{'subject':'两栋楼','pending':'入驻未核实'}})
        for text in ('白底黑线','两栋楼','入驻未核实','待补充','不要编造'):
            self.assertIn(text,prompt)

    def test_new_storyboard_route_preserves_script_and_style(self):
        task=self.store.create({'title':'新路线','script':'未经修改的原文','materials':[],
                                'creativeRoute':'storyboard','businessPreset':'general'})
        self.assertEqual(task['input']['script'],'未经修改的原文')
        self.assertEqual(task['input']['visualStyle']['style_id'],'handdrawn-realistic-v1')
        self.assertEqual(task['input']['businessPreset'],'general')
        with self.assertRaises(ValueError):self.api.require_plans(task['id'])

    def test_description_edit_requires_new_review_and_preserves_old(self):
        self.call('board',shotId='02',path=str(self.img));self.call('board',shotId='03',path=str(self.img))
        self.call('confirm_all')
        self.call('plan',shotId='02',elements=[self.elem])
        p=self.state['shots'][0]['plans'][-1]
        self.call('confirm_plan',shotId='02',planSha256=p['sha256'])
        self.call('edit_description',shotId='02',description={'subject':'调整后的主体','why':'解释关系'})
        shot=self.state['shots'][0]
        self.assertEqual(shot['description']['subject'],'调整后的主体')
        self.assertEqual(shot['descriptionHistory'][0]['id'],'02')
        self.assertEqual(shot['boards'][0]['status'],'pending')
        self.assertEqual(shot['plans'][0]['status'],'stale')
        self.assertEqual(self.state['shots'][1]['boards'][0]['status'],'confirmed')

if __name__=='__main__':unittest.main()
