import tempfile
import unittest
import subprocess
import json
import numpy as np
from pathlib import Path
from flask import Flask
from unittest.mock import patch
from suo_jian.presenter.library import save
from suo_jian.presenter.sentences import split_sentences
from suo_jian.ui.presenter_api import register_presenter_routes


def transcript(text):
    words = [c for c in text if c not in '，。']
    return {'text':text,'words':[{'text':c,'start':i*.15,'end':i*.15+.1} for i,c in enumerate(words)]}


class SentenceWorkflowTests(unittest.TestCase):
    def test_punctuation_maps_to_actual_word_ranges(self):
        rows=split_sentences(transcript('研发在这里，客户在外地。'))
        self.assertEqual([(r['first'],r['last'],r['text']) for r in rows],[(0,4,'研发在这里'),(5,9,'客户在外地')])

    def test_select_and_order_single_file_sentences_without_reference_video(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);app=Flask(__name__)
            register_presenter_routes(app,root,lambda job: (_ for _ in ()).throw(AssertionError('reference should not be needed')))
            c=app.test_client();job=c.post('/api/presenter/new',json={}).json['job']
            work=root/'work'/'presenter-workspaces'/job
            save(work/'library.json',{'folder':'素材','file_count':1,'assets':[{'id':'a','name':'a.mp4','person_group':'one','duration':10}], 'groups':[{'id':'one','asset_ids':['a'],'anchor':[1,0]}], 'errors':[]})
            save(work/'asr/a-timed-v1.json',transcript('研发在这里，客户在外地。'))
            sentences=c.get(f'/api/presenter/{job}/sentences').json['assets'][0]['sentences']
            payload={'person_group':'one','selections':[{'asset_id':'a','first':s['first'],'last':s['last']} for s in reversed(sentences)]}
            response=c.post(f'/api/presenter/{job}/sentence-plan',json=payload)
            self.assertEqual(response.status_code,200,response.json)
            plan=response.json['plan']
            self.assertEqual([r['text'] for r in plan['rows']],['客户在外地','研发在这里'])
            self.assertEqual([(c['word_first'],c['word_last']) for c in plan['selected']],[(5,9),(0,4)])
            first_cut=plan['selected'][0]
            self.assertAlmostEqual(sentences[1]['preview_start'],.725)
            self.assertAlmostEqual(first_cut['source_start'],sentences[1]['preview_start'])
            self.assertAlmostEqual(first_cut['captions'][0]['local_start'],.025)
            self.assertEqual(c.get(f'/api/presenter/{job}/state').json['plan']['selection_mode'],'sentences')
            payload['person_group']='wrong'
            self.assertEqual(c.post(f'/api/presenter/{job}/sentence-plan',json=payload).status_code,400)

    def test_contiguous_selected_captions_remain_separate(self):
        from suo_jian.presenter.continuity import merge_uninterrupted
        words=transcript('研发在这里，客户在外地。')
        cuts=[{'asset_id':'a','word_first':0,'word_last':4,'source_start':0.,'source_end':.7,'text':'研发在这里','spoken_text':'研发在这里','captions':[{'text':'研发在这里','local_start':0.,'local_end':.7}]},
              {'asset_id':'a','word_first':5,'word_last':9,'source_start':.75,'source_end':1.5,'text':'客户在外地','spoken_text':'客户在外地','captions':[{'text':'客户在外地','local_start':0.,'local_end':.75}]}]
        merged=merge_uninterrupted(cuts,{'a':words})
        self.assertEqual(len(merged),1)
        self.assertEqual([c['text'] for c in merged[0]['captions']],['研发在这里','客户在外地'])

    def test_adjacent_sentence_plan_keeps_long_source_pause_and_duration(self):
        from suo_jian.presenter.rendering import caption_timeline
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);app=Flask(__name__)
            register_presenter_routes(app,root,lambda job: None)
            client=app.test_client();job=client.post('/api/presenter/new',json={}).json['job']
            work=root/'work'/'presenter-workspaces'/job
            library={'folder':'素材','file_count':1,'assets':[
                {'id':'a','name':'a.mp4','person_group':'one','duration':20}],
                'groups':[{'id':'one','asset_ids':['a']}],'errors':[]}
            save(work/'library.json',library)
            value={'text':'第一句。第二句。第三句。','words':[
                {'text':'第一句','start':1.,'end':2.},
                {'text':'第二句','start':6.,'end':7.},
                {'text':'第三句','start':11.,'end':12.}]}
            save(work/'asr/a-timed-v1.json',value)
            rows=client.get(f'/api/presenter/{job}/sentences').json['assets'][0]['sentences']
            response=client.post(f'/api/presenter/{job}/sentence-plan',json={
                'person_group':'one','selections':[
                    {'asset_id':'a','first':row['first'],'last':row['last']} for row in rows]})
            self.assertEqual(response.status_code,200,response.json)
            plan=response.json['plan'];self.assertEqual(len(plan['selected']),1)
            cut=plan['selected'][0]
            self.assertEqual((cut['source_start'],cut['source_end']),(.7,12.25))
            self.assertAlmostEqual(plan['duration'],11.55)
            self.assertEqual((cut['word_first'],cut['word_last']),(0,2))
            self.assertEqual([caption['text'] for caption in cut['captions']],
                             ['第一句','第二句','第三句'])
            captions=caption_timeline(plan['selected'])
            self.assertAlmostEqual(captions[0]['target_start'],.3)
            self.assertAlmostEqual(captions[1]['target_start'],5.3)
            self.assertAlmostEqual(captions[2]['target_start'],10.3)
            self.assertLess(captions[0]['target_start']+captions[0]['duration'],
                            captions[1]['target_start'])
            self.assertEqual([row['selected']['word_first'] for row in plan['rows']],[0,1,2])
            self.assertEqual([len(row['selected']['captions']) for row in plan['rows']],[1,1,1])
            saved=client.get(f'/api/presenter/{job}/state').json['plan']
            self.assertEqual(saved['duration'],plan['duration'])
            self.assertEqual(saved['selected'],plan['selected'])

    def test_pause_policy_does_not_restore_skipped_speech_or_change_selection_order(self):
        from suo_jian.presenter.continuity import merge_uninterrupted
        value={'words':[{'text':'第一句'},{'text':'，'},{'text':'第二句'},
                        {'text':'第三句'}]}
        def cut(first,start,end,asset='a'):
            return {'asset_id':asset,'word_first':first,'word_last':first,
                    'source_start':start,'source_end':end,'text':'口播','spoken_text':'口播',
                    'captions':[{'text':'口播','local_start':0.,'local_end':end-start}]}
        first=cut(0,0.,1.);second=cut(2,6.,7.);third=cut(3,12.,13.)
        self.assertEqual(len(merge_uninterrupted([first,second],{'a':value})),2)
        joined=merge_uninterrupted([first,second],{'a':value},max_gap=None)
        self.assertEqual(len(joined),1)
        self.assertEqual(joined[0]['duration'],7.)
        self.assertEqual(len(first['captions']),1)
        for cuts in ([first,third],[second,first],[first,cut(2,6.,7.,'b')]):
            result=merge_uninterrupted(cuts,{'a':value,'b':value},max_gap=None)
            self.assertEqual(len(result),2)
            self.assertEqual([cut['asset_id'] for cut in result],[cut['asset_id'] for cut in cuts])

    def test_generated_preview_and_editable_draft_keep_the_original_silent_interval(self):
        from suo_jian.presenter.assembly import reviewed_plan
        from suo_jian.presenter.continuity import merge_uninterrupted
        from suo_jian.presenter.rendering import render,export_draft
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',
                'color=c=blue:s=160x284:r=30:d=4.2','-f','lavfi','-i',
                r'aevalsrc=if(between(t\,0.4\,0.8)+between(t\,3.1\,3.6)\,0.2*sin(2*PI*440*t)\,0):s=48000:d=4.2',
                '-c:v','libx264','-threads','2','-c:a','aac',str(source)],
                capture_output=True,check=True,timeout=30)
            save(root/'asr/a-timed-v1.json',{'words':[
                {'text':'第一句','start':.4,'end':.8},
                {'text':'第二句','start':3.1,'end':3.6}]})
            library={'assets':[{'id':'a','name':'source.mp4','path':str(source),
                'person_group':'one','duration':4.2,'width':160,'height':284}]}
            plan=reviewed_plan(library,root,'one',[
                {'asset_id':'a','first':i,'last':i,'captions':[{'first':i,'last':i}]} for i in range(2)],'',sentence_selection=True)
            words={'a':{'words':[{'text':'第一句'},{'text':'第二句'}]}}
            plan['selected']=merge_uninterrupted(plan['selected'],words,max_gap=None)
            # Identity detection is outside this timing test; media operations are real.
            with patch('suo_jian.presenter.rendering.checked_cuts',return_value=[]):
                manifest=render(plan,library,{},root)
            self.assertEqual(len(manifest['cuts']),1)
            self.assertGreaterEqual(manifest['duration'],3.75)
            self.assertEqual(len(manifest['captions']),2)
            self.assertAlmostEqual(manifest['captions'][0]['target_start'],.3)
            self.assertAlmostEqual(manifest['captions'][1]['target_start'],3.)
            media=Path(manifest['cuts'][0]['media'])
            def rms(start,duration=.3):
                raw=subprocess.check_output(['ffmpeg','-v','error','-ss',str(start),'-i',str(media),
                    '-t',str(duration),'-vn','-ac','1','-ar','48000','-f','f32le','pipe:1'],timeout=30)
                samples=np.frombuffer(raw,dtype='<f4')
                self.assertGreater(len(samples),1000)
                return float(np.sqrt(np.mean(samples**2)))
            self.assertLess(rms(.03,.2),.001)
            self.assertLess(rms(1.5),.001)
            self.assertGreater(rms(.35),.01)
            draft=export_draft(manifest,root)
            content=json.loads((Path(draft['folder'])/'draft_content.json').read_text(encoding='utf-8'))
            video=next(track for track in content['tracks'] if track['type']=='video')
            text=next(track for track in content['tracks'] if track['type']=='text')
            self.assertEqual(len(video['segments']),1)
            self.assertGreaterEqual(video['segments'][0]['target_timerange']['duration'],3_750_000)
            self.assertEqual(len(text['segments']),2)
            self.assertEqual(text['segments'][0]['target_timerange']['start'],300_000)
            self.assertEqual(text['segments'][1]['target_timerange']['start'],3_000_000)


if __name__=='__main__':unittest.main()
