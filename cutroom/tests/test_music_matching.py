import unittest
import numpy as np
from media_catalog.music_matching import RATE,features,choose_window,distance

class MusicMatchingTests(unittest.TestCase):
    def tone(self,frequency,seconds=10):
        t=np.arange(int(seconds*RATE))/RATE
        return (.1*np.sin(2*np.pi*frequency*t)).astype(np.float32)

    def test_window_avoids_wrong_timbre_intro(self):
        reference=features(self.tone(1000,6))
        result=choose_window(np.concatenate([self.tone(150),self.tone(1000)]),6,reference)
        self.assertGreaterEqual(result['start'],10)
        self.assertFalse(result['loop_required'])
        self.assertLessEqual(result['start']+result['duration'],20)

    def test_short_music_reports_loop_and_bounded_start(self):
        audio=self.tone(500,5)
        result=choose_window(audio,12,features(audio))
        self.assertTrue(result['loop_required'])
        self.assertEqual(result['start'],0)
        self.assertEqual(result['duration'],5)

    def test_rhythm_is_approximate_and_optional(self):
        t=np.arange(12*RATE)/RATE
        x=(.1*np.sin(2*np.pi*500*t)*(t%0.5<.08)).astype(np.float32)
        profile=features(x)
        self.assertIsNotNone(profile['bpm'])
        self.assertAlmostEqual(profile['bpm'],120,delta=10)
        self.assertGreaterEqual(profile['tempo_confidence'],.2)

    def test_equal_profiles_have_zero_distance(self):
        profile=features(self.tone(500))
        self.assertEqual(distance(profile,profile),0)

if __name__=='__main__': unittest.main()
