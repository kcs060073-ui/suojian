import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from workbench.card_layers import migrate
from workbench.director import apply_operations
from workbench.server import validate_config
from workbench.paths import NODE as _NODE

ROOT=Path(__file__).resolve().parents[1]
NODE=Path(_NODE)

class CardLayersTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        path=self.root/'work/park-fusion/src/script07-data.json';path.parent.mkdir(parents=True);path.write_bytes((ROOT/'work/park-fusion/src/script07-data.json').read_bytes())
        self.config={'scenePack':'jiading-script07','format':'portrait','musicGain':1,'voiceGain':1,'subtitles':[{'from':745,'to':845,'text':'实际字幕'}],
            'shots':[{'id':'jiading-5','recipe':'footage','duration':206,'from':743,'sourceOffset':0,'source':'/media/generated-cards/known-job.mp4','title':'当前用户主标题','label':'当前镜头'}]}
        receipt=self.root/'outputs/runs/known-job/job.json';receipt.parent.mkdir(parents=True);receipt.write_text(json.dumps({'result':{'receipt':{'id':'aurora-bloom-bg-flip'}}}),encoding='utf-8')

    def test_native_demo_becomes_effect_not_source_replacement(self):
        fixed=migrate(self.config,self.root);s=fixed['shots'][0]
        self.assertEqual(s['source'],'/media/script07/jiading-5.mp4');self.assertEqual(s['recipe'],'jiading-4')
        self.assertEqual(s['cardEffect'],'aurora-bloom-bg-flip');self.assertEqual(s['sourceOffset'],743)
        self.assertEqual(s['referencePreview'],'/media/generated-cards/known-job.mp4')
        self.assertEqual(s['title'],'当前用户主标题');self.assertEqual(fixed['subtitles'],self.config['subtitles'])
        self.assertEqual(migrate(fixed,self.root),fixed)

    def test_unknown_demo_is_not_silently_mapped_to_different_effect(self):
        self.config['shots'][0]['source']='/media/generated-cards/unknown.mp4'
        fixed=migrate(self.config,self.root)
        self.assertEqual(fixed['shots'][0]['source'],self.config['shots'][0]['source'])
        self.assertNotIn('cardEffect',fixed['shots'][0])

    def test_layout_media_and_voice_survive_effect_parameter_change(self):
        c=migrate(self.config,self.root);before=copy.deepcopy(c)
        ops=[{'op':'set','path':'shots.0.cardEffect','value_json':'"deck-deal-flyin"'},
             {'op':'set','path':'shots.0.cardEnterFrames','value_json':'48'},
             {'op':'set','path':'shots.0.cardIntensity','value_json':'1.3'}]
        result=apply_operations(c,ops);after=result[0] if isinstance(result,tuple) else result
        for field in ('recipe','source','sourceOffset','title'):
            self.assertEqual(before['shots'][0][field],after['shots'][0][field])
        self.assertEqual(before['subtitles'],after['subtitles'])
        self.assertEqual(after['shots'][0]['cardEffect'],'deck-deal-flyin')
        self.assertEqual(after['shots'][0]['cardEnterFrames'],48)
        for invalid in ({'backgroundBlur':80},{'backgroundBrightness':2}):
            with self.assertRaises(ValueError):validate_config({**after,**invalid})

    def test_effects_have_distinct_entry_poses_and_parameterized_hold(self):
        module=str(ROOT/'work/park-fusion/src/card-motion.cjs')
        code='const m=require('+json.dumps(module)+');console.log(JSON.stringify(m.EFFECTS.map(cardEffect=>({cardEffect,entry:m.pose({cardEffect,duration:150},12),hold:m.pose({cardEffect,duration:150},100),off:m.pose({cardEffect,duration:150,cardIntensity:0},12)}))))'
        result=subprocess.run([str(NODE),'-e',code],capture_output=True,text=True,check=True)
        rows=json.loads(result.stdout)
        transforms=[r['entry']['transform'] for r in rows[1:]]
        self.assertEqual(len(set(transforms)),5)
        row=next(r for r in rows if r['cardEffect']=='row-embed')['entry']
        self.assertLess(row['mediaX'],0);self.assertGreater(row['diagramX'],0)
        for r in rows:
            self.assertEqual(r['off']['opacity'],1)
            self.assertEqual(r['hold']['opacity'],1)
            self.assertAlmostEqual(r['hold']['blur'],0)
