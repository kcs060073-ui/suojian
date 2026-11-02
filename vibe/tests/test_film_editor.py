import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from workbench.films import catalog, load_editor, save_editor, register_film, latest_completion, can_auto_open
from workbench.director import apply_operations
from workbench.server import validate_config

from workbench.paths import NODE as _NODE
ROOT=Path(__file__).resolve().parents[1]
NODE=Path(_NODE)

class FilmEditorTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'outputs').mkdir()
        (self.root/'outputs/嘉定园区-这栋楼只有一个业主-竖屏混合版.mp4').write_bytes(b'video')
        dest=self.root/'work/park-fusion/src/script07-data.json';dest.parent.mkdir(parents=True)
        dest.write_bytes((ROOT/'work/park-fusion/src/script07-data.json').read_bytes())
        self.id=catalog(self.root)[0]['id']
        self.config=load_editor(self.root,self.id)

    def test_real_six_shot_project_uses_jiading_assets_and_xiaoxiao(self):
        c=validate_config(self.config)
        self.assertEqual(len(c['shots']),6);self.assertEqual(sum(s['duration'] for s in c['shots']),1121)
        self.assertEqual(c['shots'][2]['recipe'],'jiading-2');self.assertIn('自持物业',c['shots'][2]['title'])
        self.assertTrue(all(s['source'].startswith('/media/script07/jiading-') for s in c['shots']))
        self.assertEqual(c['narrationFile'],'/media/script07/voice.mp3');self.assertEqual(len(c['subtitles']),12)
        self.assertTrue(catalog(self.root)[0]['editorAvailable'])

    def test_project_edits_are_saved_separately_from_another_film(self):
        first=copy.deepcopy(self.config);first['shots'][0]['title']='我修改的标题';save_editor(self.root,first)
        other=self.root/'outputs/runs/next/preview.mp4';other.parent.mkdir(parents=True);other.write_bytes(b'other')
        register_film(self.root,other,{**first,'name':'另一个项目'})
        other_id=next(f['id'] for f in catalog(self.root) if f['video']['name']=='preview.mp4')
        second=load_editor(self.root,other_id);second['shots'][0]['title']='第二个项目';save_editor(self.root,second)
        self.assertEqual(load_editor(self.root,self.id)['shots'][0]['title'],'我修改的标题')
        self.assertEqual(load_editor(self.root,other_id)['shots'][0]['title'],'第二个项目')

    def test_unknown_film_cannot_load_arbitrary_local_file(self):
        for identifier in ('../../private','film-0000000000000000'):
            with self.assertRaises(ValueError):load_editor(self.root,identifier)

    def test_completion_opens_matching_project_and_protects_later_edits(self):
        video=self.root/'outputs/new-film.mp4';video.write_bytes(b'completed video')
        identifier=register_film(self.root,video,self.config)
        event=latest_completion(self.root)
        self.assertEqual(event['filmId'],identifier)
        self.assertTrue(can_auto_open(self.root,identifier,self.config))
        same=copy.deepcopy(self.config);same['editFilmId']=identifier
        self.assertTrue(can_auto_open(self.root,identifier,same))
        same['shots'][0]['title']='渲染期间新增的修改'
        self.assertFalse(can_auto_open(self.root,identifier,same))
        register_film(self.root,video,self.config)
        self.assertNotEqual(latest_completion(self.root)['id'],event['id'])

    def manifest(self,c):
        source=self.root/'input.json';source.write_text(json.dumps(c,ensure_ascii=False),encoding='utf-8')
        result=subprocess.run([str(NODE),str(ROOT/'engine/bridge.cjs'),'manifest','--config',str(source)],capture_output=True,text=True,encoding='utf-8',check=True)
        return json.loads(result.stdout)

    def test_render_manifest_uses_correct_voice_and_retimes_cuts_and_subtitles(self):
        c=self.config;m=self.manifest(c)
        self.assertEqual(m['scenePack'],'jiading-script07');self.assertEqual(m['durationInFrames'],1121)
        self.assertEqual(len(m['subtitles']),12)
        voices=[e for e in m['audioEvents'] if e['track']=='voice']
        self.assertEqual(len(voices),6);self.assertTrue(all(e['source'].replace('\\','/').endswith('/script07/voice.mp3') for e in voices))
        self.assertFalse(any(e['track']=='reference' for e in m['audioEvents']))
        c['shots']=[c['shots'][2],c['shots'][0]]
        m=self.manifest(c);self.assertEqual(m['durationInFrames'],314)
        voices=[e for e in m['audioEvents'] if e['track']=='voice']
        self.assertEqual((voices[0]['from'],voices[0]['sourceFrom']),(0,288))
        self.assertEqual((voices[1]['from'],voices[1]['sourceFrom']),(237,0))
        self.assertEqual(m['subtitles'][0]['text'],'这里是自持物业，只租不售。')
        self.assertEqual(m['subtitles'][0]['from'],2)
        c['plate']=True;m=self.manifest(c);self.assertEqual(m['audioEvents'],[]);self.assertEqual(m['subtitles'],[])

    def test_chat_director_can_edit_jiading_title_without_changing_footage_or_voice(self):
        ops=[{'op':'set','path':'shots.2.title','value_json':json.dumps('产权方直接签约',ensure_ascii=False)}]
        result=apply_operations(self.config,ops)
        c=result[0] if isinstance(result,tuple) else result
        self.assertEqual(c['shots'][2]['title'],'产权方直接签约')
        self.assertEqual(c['shots'][2]['recipe'],'jiading-2');self.assertEqual(c['narrationFile'],self.config['narrationFile'])

    def test_card_effect_switch_preserves_all_audio_segments_and_subtitles(self):
        original=self.manifest(self.config)
        modified=copy.deepcopy(self.config)
        for s in modified['shots']:s['recipe']='deck-deal-flyin';s['cardMotion']=True
        changed=self.manifest(modified)
        self.assertEqual(original['audioEvents'],changed['audioEvents'])
        self.assertEqual(original['subtitles'],changed['subtitles'])

    def test_chat_card_switch_enables_motion_only_for_selected_shot(self):
        before=copy.deepcopy(self.config)
        changed=apply_operations(before,[{'op':'set','path':'shots.2.recipe','value_json':'"deck-deal-flyin"'}])
        changed=changed[0] if isinstance(changed,tuple) else changed
        self.assertFalse(changed['shots'][2]['cardMotion'])
        self.assertEqual(changed['shots'][2]['recipe'],'jiading-2')
        self.assertEqual(changed['shots'][2]['cardEffect'],'deck-deal-flyin')
        self.assertEqual(changed['shots'][2]['sourceOffset'],288)
        self.assertEqual(changed['shots'][1],self.config['shots'][1])
