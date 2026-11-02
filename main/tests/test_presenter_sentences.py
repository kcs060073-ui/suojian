import tempfile
import unittest
from pathlib import Path
from flask import Flask
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


if __name__=='__main__':unittest.main()
