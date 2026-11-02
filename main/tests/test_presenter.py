import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from flask import Flask
from suo_jian.presenter.library import save,cluster
from suo_jian.presenter.matching import match,normalized
from suo_jian.presenter.rendering import render,draft_video_bounds
from suo_jian.presenter.assembly import reviewed_plan
from suo_jian.presenter.delivery import install_draft
from suo_jian.presenter.continuity import merge_uninterrupted,primary_profile,continuity_bonus
from suo_jian.ui.presenter_api import register_presenter_routes

def speech(text):
    return {'text':text,'words':[{'text':c,'start':i*.12,'end':(i+1)*.12} for i,c in enumerate(text)]}

class PresenterTests(unittest.TestCase):
    def test_numeric_normalization_preserves_decimals(self):
        self.assertEqual(normalized('三毛钱，五百平米'),normalized('3毛钱500平米'))
        self.assertEqual(normalized('四点五米'),normalized('4.5米'))
        self.assertNotEqual(normalized('点击链接'),normalized('击链接'))

    def test_click_word_is_retained_in_matched_audio(self):
        with tempfile.TemporaryDirectory() as t:
            work=Path(t);save(work/'asr/a-timed-v1.json',speech('点击链接。'))
            library={'assets':[{'id':'a','person_group':'one','duration':10,'name':'a.mp4'}],'groups':[{'id':'one'}]}
            chosen=match('点击链接',library,work,'one')['selected'][0]
            self.assertEqual(chosen['word_first'],0)
            self.assertEqual(chosen['spoken_text'],'点击链接')

    def test_continuity_prefers_the_reviewed_scene_and_wardrobe(self):
        profiles={'a':{'setting':'inside','outfit':'stripe','backdrop':'columns'},
                  'b':{'setting':'outside','outfit':'stripe','backdrop':'building'}}
        a={'asset_id':'a','source_start':10};b={'asset_id':'b','source_start':10}
        primary=primary_profile([[a],[a],[a,b],[b]],profiles)
        self.assertEqual(primary,('inside','stripe'))
        self.assertGreater(continuity_bonus(a,None,primary,profiles),continuity_bonus(b,None,primary,profiles))

    def test_contiguous_audio_merges_only_when_no_spoken_word_is_omitted(self):
        cuts=[{'asset_id':'a','word_first':0,'word_last':1,'source_start':0.,'source_end':.24,'text':'宿舍','spoken_text':'宿舍'},
              {'asset_id':'a','word_first':3,'word_last':4,'source_start':.36,'source_end':.60,'text':'食堂','spoken_text':'食堂'}]
        self.assertEqual(len(merge_uninterrupted(cuts,{'a':speech('宿舍，食堂')})),1)
        self.assertEqual(len(merge_uninterrupted(cuts,{'a':speech('宿舍和食堂')})),2)

    def test_native_draft_uses_shared_endpoints_for_thirtieth_second_frames(self):
        value={'duration':31.5,'cuts':[{'target_start':20.833333,'duration':4.566667},
                                      {'target_start':25.4,'duration':6.1}]}
        self.assertEqual(draft_video_bounds(value,0)[1],draft_video_bounds(value,1)[0])

    def test_same_person_and_price_are_hard_constraints(self):
        with tempfile.TemporaryDirectory() as t:
            work=Path(t)
            assets=[{'id':'a','person_group':'one','duration':10,'name':'same.mp4'},
                    {'id':'b','person_group':'two','duration':10,'name':'other.mp4'}]
            save(work/'asr/a-timed-v1.json',speech('租金八毛钱。'))
            save(work/'asr/b-timed-v1.json',speech('租金三毛钱。'))
            library={'assets':assets,'groups':[{'id':'one'},{'id':'two'}]}
            self.assertFalse(match('租金三毛钱',library,work,'one')['selected'])
            self.assertEqual(match('租金三毛钱',library,work,'two')['selected'][0]['asset_id'],'b')

    def test_a_single_take_cannot_be_reused_to_fake_repeated_speech(self):
        with tempfile.TemporaryDirectory() as t:
            work=Path(t);save(work/'asr/a-timed-v1.json',speech('水电暖齐全。'))
            library={'assets':[{'id':'a','person_group':'one','duration':10,'name':'a.mp4'}],'groups':[{'id':'one'}]}
            plan=match('水电暖齐全，水电暖齐全',library,work,'one')
            self.assertEqual(len(plan['selected']),1)
            self.assertEqual(len(plan['missing']),1)

    def test_uncertain_faces_are_not_forced_into_a_group(self):
        records=[{'id':'a','faces':[{'feature':[1,0]},None,None]},
                 {'id':'b','faces':[{'feature':[1,0]},{'feature':[1,0]}]},
                 {'id':'c','faces':[{'feature':[0,1]},{'feature':[0,1]}]}]
        groups=cluster(records)
        self.assertIsNone(records[0]['person_group'])
        self.assertEqual(len(groups),2)

    def test_missing_sentence_blocks_full_render_before_any_media_action(self):
        with patch('suo_jian.presenter.rendering.checked_cuts') as check:
            with self.assertRaisesRegex(ValueError,'缺句'):
                render({'missing':['补拍'],'selected':[]},{},{},Path('.'))
            check.assert_not_called()

    def test_reviewed_adaptation_keeps_real_words_and_reference_gaps(self):
        with tempfile.TemporaryDirectory() as t:
            work=Path(t);save(work/'asr/a-timed-v1.json',speech('厂房500平起租。'))
            library={'assets':[{'id':'a','person_group':'one','duration':10,'name':'a.mp4'}]}
            take={'asset_id':'a','first':0,'last':8,'captions':[{'first':0,'last':8}],
                  'reference_point':'起租面积'}
            plan=reviewed_plan(library,work,'one',[take],'参考脚本',['整层1000多平未覆盖'])
            self.assertEqual(plan['selected'][0]['spoken_text'],'厂房500平起租。')
            self.assertEqual(plan['reference_gaps'],['整层1000多平未覆盖'])
            self.assertEqual(plan['selected'][0]['captions'][0]['text'],'厂房500平起租')
            with self.assertRaisesRegex(ValueError,'其他人物'):
                reviewed_plan(library,work,'two',[take],'参考脚本')
            with self.assertRaisesRegex(ValueError,'数字'):
                reviewed_plan(library,work,'one',[{**take,'corrections':{'2':'8'}}],'参考脚本')

    def test_reviewed_captions_cannot_drop_spoken_words(self):
        with tempfile.TemporaryDirectory() as t:
            work=Path(t);save(work/'asr/a-timed-v1.json',speech('水电暖齐全。'))
            library={'assets':[{'id':'a','person_group':'one','duration':10,'name':'a.mp4'}]}
            with self.assertRaisesRegex(ValueError,'漏掉'):
                reviewed_plan(library,work,'one',[{'asset_id':'a','first':0,'last':5,
                    'captions':[{'first':0,'last':3}]}],'参考脚本')

    def test_routes_hide_embeddings_and_reject_cross_site_mutations(self):
        with tempfile.TemporaryDirectory() as t:
            result=Path(t);work=result/'presenter';work.mkdir()
            save(work/'library.json',{'groups':[{'id':'one','anchor':[1,0],'asset_ids':['a']}],
                 'assets':[{'id':'a','path':'private.mp4','faces':[{'feature':[1,0]}]}],'errors':[]})
            app=Flask(__name__);register_presenter_routes(app,result,lambda job:({},result/'ref.mp4',result))
            client=app.test_client()
            state=client.get('/api/presenter/12345678/state').get_json()
            self.assertNotIn('feature',json.dumps(state))
            self.assertNotIn('anchor',json.dumps(state))
            response=client.post('/api/presenter/12345678/scan',json={'folder':t},headers={'Origin':'https://example.com'})
            self.assertEqual(response.status_code,403)
            self.assertEqual(client.get('/api/presenter/12345678/thumb/../../bad').status_code,404)

    def test_install_relocates_materials_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as t,patch('suo_jian.jianying.execution_export.register_native_draft',return_value={'registered':True}),patch('suo_jian.presenter.delivery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='')):
            work=Path(t);source=work/'export'/'溯剪-真人复刻-test';(source/'media').mkdir(parents=True)
            (source/'media/a.mp4').write_bytes(b'fixture')
            save(source/'draft_content.json',{'materials':{'videos':[{'path':str(source/'media/a.mp4')}]},'tracks':[]})
            save(source/'draft_meta_info.json',{})
            native=work/'native';native.mkdir()
            result=install_draft({'folder':str(source)},native)
            saved=json.loads((Path(result['folder'])/'draft_content.json').read_text(encoding='utf-8'))
            path=Path(saved['materials']['videos'][0]['path'])
            self.assertTrue(path.is_relative_to(native));self.assertTrue(path.is_file())
            first_id=saved['id']
            # A second export made from the library's fixed template must still
            # become a separately addressable native project.
            source2=work/'export'/'溯剪-真人复刻-test2'
            import shutil
            shutil.copytree(source,source2)
            save(source2/'draft_content.json',{'id':first_id,'materials':{'videos':[{'path':str(source2/'media/a.mp4')}]},'tracks':[]})
            result2=install_draft({'folder':str(source2)},native)
            saved2=json.loads((Path(result2['folder'])/'draft_content.json').read_text(encoding='utf-8'))
            self.assertNotEqual(first_id,saved2['id'])
            path.write_bytes(b'edited-in-editor')
            install_draft(result,native)
            self.assertEqual(path.read_bytes(),b'edited-in-editor')
            with self.assertRaises(FileExistsError):install_draft({'folder':str(source)},native)

    def test_install_waits_for_editor_to_close_before_copying(self):
        with tempfile.TemporaryDirectory() as t,patch('suo_jian.presenter.delivery.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='RUNNING')):
            root=Path(t);source=root/'溯剪-真人复刻-test';source.mkdir();native=root/'native';native.mkdir()
            with self.assertRaisesRegex(ValueError,'退出剪映'):install_draft({'folder':str(source)},native)
            self.assertFalse((native/source.name).exists())

    def test_registration_rejects_id_owned_by_a_different_project(self):
        from suo_jian.jianying.execution_export import register_native_draft
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);output=root/'new';output.mkdir()
            save(root/'root_meta_info.json',{'all_draft_store':[{'draft_id':'same','draft_fold_path':str(root/'existing')}],'draft_ids':1})
            save(output/'draft_content.json',{'id':'same','duration':1})
            save(output/'draft_meta_info.json',{'draft_name':'new','draft_fold_path':str(output)})
            before=(root/'root_meta_info.json').read_bytes()
            with self.assertRaisesRegex(ValueError,'另一个工程'):register_native_draft(output)
            self.assertEqual(before,(root/'root_meta_info.json').read_bytes())

if __name__=='__main__':unittest.main()
