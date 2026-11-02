import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from media_catalog.reference_editing import apply_reference_policy, split_intro_cues


class ReferenceEditingTests(unittest.TestCase):
    def test_six_shots_are_independent_of_single_line_caption_count_and_report_substitutes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/'runtime/workbench/tasks/task-a/reference-policies/plan-a.json'
            path.parent.mkdir(parents=True)
            policy={"task_id":"task-a","plan_id":"plan-a","ratio":"16:9","width":1920,"number_preset":"ft-pop",
                    "shots":[{"duration":1,"motion":{"type":"pan_left"}} for _ in range(6)],
                    "subtitle":{"max_lines":1,"max_chars_per_line":15,"size":96,"fontname_guess":"Arial","primary_hex":"#F8EF29","outline_hex":"#111111","margin_v":70,"outline":6,"shadow":1}}
            path.write_text(json.dumps(policy),encoding='utf-8')
            original=[{"text":"层高11到12米，","start":0,"end":3,"dur":3,"src":"a.mp4"},
                      {"text":"一层承重5吨。","start":3,"end":6,"dur":3,"src":"b.mp4"}]
            original_json=json.dumps(original)
            with patch('media_catalog.reference_editing.detect_camera_motion',return_value={"type":"static","confidence":.7,"duration":10,"width":1920,"height":1080}):
                result=apply_reference_policy(root,'task-a','plan-a',original,{'id':'sub-ad-test'}, {'a':Path('a.mp4'),'b':Path('b.mp4'),'c':Path('c.mp4')})
            self.assertEqual(json.dumps(original),original_json)
            self.assertEqual(len(result['segments']),6)
            self.assertEqual(len(result['subtitleEvents']),2)
            self.assertTrue(all('\n' not in row['displayText'] for row in result['subtitleEvents']))
            self.assertEqual(result['subtitlePreset']['primary_hex'],'#F8EF29')
            self.assertEqual(result['transitionMode'],'cut')
            self.assertEqual(result['referenceMatch']['selectedUniqueAssets'],2)
            self.assertTrue(any(row['repeated_asset'] for row in result['referenceMatch']['shots']))
            self.assertTrue(all(row['substitution'] != 'exact' for row in result['referenceMatch']['shots']))
            self.assertEqual({row['src'] for row in result['segments']},{'a.mp4','b.mp4'})

    def test_intro_list_is_three_cues_without_rewriting_audio_copy(self):
        rows=[{"i":3,"text":"嘉兴平湖新仓智谷天地：国资、可租可售、","start":8.79,"end":13.784,"dur":4.994}]
        result=split_intro_cues(rows)
        self.assertEqual([x['text'] for x in result],["嘉兴平湖新仓智谷天地：","国资、","可租可售、"])
        self.assertEqual(''.join(x['text'] for x in result),rows[0]['text'])
        self.assertEqual(result[0]['start'],rows[0]['start'])
        self.assertEqual(result[-1]['end'],rows[0]['end'])
        self.assertTrue(all(a['end']==b['start'] for a,b in zip(result,result[1:])))
        self.assertTrue(all(x['dur']>0 for x in result))
        from media_catalog.ad_subtitles import layout_ad_subtitles
        events=layout_ad_subtitles(result,{'max_lines':1,'max_chars_per_line':15},'16:9')
        self.assertEqual([x['displayText'] for x in events],["嘉兴平湖新仓智谷天地","国资","可租可售"])
        self.assertEqual(len(rows),1)

    def test_clock_colon_is_not_an_intro(self):
        row={"text":"上午10:30开始","start":0,"end":3,"dur":3}
        self.assertEqual(split_intro_cues([row]),[row])

    def test_unbound_task_preserves_standard_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(apply_reference_policy(Path(directory),'task-a','plan-a',[],{},{}))


if __name__ == '__main__':
    unittest.main()
