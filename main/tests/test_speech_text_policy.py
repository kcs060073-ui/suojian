import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from suo_jian.analyzer.speech_text_policy import usable_speech, matching_captions, caption_probe_times
from suo_jian.analyzer.subtitles import _normalize_asr_segments
from suo_jian.pipeline import analyze, AnalysisCheckpoints


class SpeechTextPolicyTests(unittest.TestCase):
    speech = [{'start':1,'end':6,'text':'这里是我们的园区','confidence':.9}]
    caption = {'start':2,'end':3.5,'text':'我们的园区','sample_count':2,
               'bbox':{'y':.8,'height':.05},'confidence':.9}

    def test_music_and_untimed_hallucination_do_not_trigger_ocr(self):
        self.assertEqual(usable_speech([{'start':0,'end':0,'text':'random imagined transcript'}]),[])
        music = _normalize_asr_segments([{'start':0,'end':4,'text':'<|BGM|>music words'}])
        self.assertEqual(usable_speech(music),[])
        self.assertTrue(usable_speech([{'start':0,'end':0,'text':'可靠口播','speech_event':'speech'}]))

    def test_captions_must_match_the_voice_and_timing(self):
        self.assertEqual(matching_captions([self.caption],self.speech),[self.caption])
        for update in ({'text':'CHINA EASTERN'},{'start':10,'end':12},{'sample_count':1},{'bbox':{'y':.1,'height':.1}}):
            self.assertEqual(matching_captions([{**self.caption,**update}],self.speech),[])
        self.assertLessEqual(len(caption_probe_times(self.speech*20,10)),18)

    def run_pipeline(self, speech, observations):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root=Path(directory);video=root/'video.mp4';video.write_bytes(b'fixture')
            returns={'read_metadata':{'duration':10},'detect_scenes':([],[]),
                     'analyze_beats':({'timestamps':[]},None),'transcribe_if_available':(speech,None),
                     'analyze_tts':{},'build_shots':([],[]),'analyze_sound_effects':({},None),
                     'separate_audio_stems':({},None),'analyze_stickers':({},None),
                     'analyze_creative':({},[]),'build_blueprint':{'warnings':[]},
                     'save_blueprint':root/'blueprint.json','export_draft_skeleton':(root/'draft',''),
                     'write_preview':None}
            for name,value in returns.items():stack.enter_context(patch('suo_jian.pipeline.'+name,return_value=value))
            ocr=stack.enter_context(patch('suo_jian.pipeline.detect_hard_subtitles',return_value=(observations,None)))
            # An older all-frame OCR cache must not override the new speech gate.
            AnalysisCheckpoints(video,root/'out',{'subtitles':True,'stems':False}).stage('ocr',lambda:([self.caption],None))
            blueprint,_,_=analyze(video,root/'out',with_subtitles=True)
            return blueprint,ocr.call_args_list

    def test_pipeline_skips_all_ocr_without_reliable_speech(self):
        blueprint,calls=self.run_pipeline([], [self.caption])
        self.assertEqual(calls,[])
        self.assertEqual(blueprint['text_analysis']['status'],'skipped')

    def test_pipeline_only_probes_when_no_matching_subtitles(self):
        blueprint,calls=self.run_pipeline(self.speech, [{**self.caption,'text':'LOGO'}])
        self.assertEqual(len(calls),1)
        self.assertIn('sample_times',calls[0].kwargs)
        self.assertEqual(blueprint['text_analysis']['status'],'skipped')

    def test_pipeline_expands_ocr_only_after_matching_captions(self):
        blueprint,calls=self.run_pipeline(self.speech,[self.caption])
        self.assertEqual(len(calls),2)
        self.assertIn('progress',calls[1].kwargs)
        self.assertEqual(blueprint['text_analysis']['status'],'completed')


if __name__=='__main__':unittest.main()
