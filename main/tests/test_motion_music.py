import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from suo_jian.ui.motion_music import align_events, align_shots, SOURCE_CUTS, analyze_beats
from suo_jian.ui import motion_explainer as motion
from suo_jian.ui.app import app


class MotionMusicTests(unittest.TestCase):
    def test_speech_events_only_snap_to_nearby_measured_beats(self):
        values = align_events([8.9, 14.5, 21.6, 26.8])
        self.assertTrue(values[1]["synced"])
        self.assertFalse(values[2]["synced"])
        self.assertTrue(all(abs(e["offsetSeconds"]) <= .18 for e in values))
        self.assertEqual(values[0]["frame"], 0)

    def test_shot_plan_preserves_all_six_sources_and_complete_duration(self):
        plan = align_shots(list(range(0, 1067, 18)))
        self.assertEqual(len(plan), 6)
        self.assertEqual(plan[0]["from"], 0)
        self.assertEqual(plan[-1]["from"] + plan[-1]["duration"], 1067)
        for i, item in enumerate(plan):
            self.assertLessEqual(abs(item["shiftFrames"]), 6)
            self.assertEqual(item["sourceStart"], SOURCE_CUTS[i])
            self.assertEqual(item["sourceEnd"], SOURCE_CUTS[i + 1])
            self.assertAlmostEqual(item["playbackRate"] * item["duration"], item["sourceEnd"] - item["sourceStart"])
            if i:
                self.assertEqual(plan[i - 1]["from"] + plan[i - 1]["duration"], item["from"])

    def test_actual_click_audio_yields_measured_120_bpm(self):
        import librosa
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "click.wav"
            sf.write(path, librosa.clicks(times=np.arange(.5, 12, .5), sr=22050, length=12 * 22050), 22050)
            result = analyze_beats(path)
            self.assertLess(abs(result["bpm"] - 120), 6)
            self.assertGreater(len(result["beatFrames"]), 18)

    def test_bgm_upload_rejects_non_audio_without_saving(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(motion, "folder", return_value=Path(temp)):
            result = app.test_client().post("/api/motion/a89d53f9/music", data={"bgm": (io.BytesIO(b'bad'), 'page.html')})
            self.assertEqual(result.status_code, 400)
            self.assertFalse((Path(temp) / "music.json").exists())

    def test_real_uploaded_bgm_is_measured_and_returns_playable_ducked_stem(self):
        import librosa
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            public = root / "public/a89d53f9"
            public.mkdir(parents=True)
            samples = librosa.clicks(times=np.arange(.5, 12, .5), sr=22050, length=12 * 22050)
            wav = root / "click.wav"
            sf.write(wav, samples, 22050)
            # FFmpeg detects this test voice fixture by content.
            (public / "narration.mp3").write_bytes(wav.read_bytes())
            with patch.object(motion, "folder", return_value=root / "result"), patch.object(motion, "engine", return_value=root):
                result = app.test_client().post("/api/motion/a89d53f9/music", data={"bgm": (io.BytesIO(wav.read_bytes()), "my-bgm.wav")})
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json["music"]["source"], "uploaded")
            self.assertTrue(result.json["music"]["looped"])
            self.assertGreater(len(result.json["music"]["beatFrames"]), 50)
            self.assertGreater((public / result.json["music"]["asset"]).stat().st_size, 1000)

    def test_style_mutation_rejects_external_origin_and_unknown_style(self):
        client = app.test_client()
        self.assertEqual(client.post("/api/motion/a89d53f9/look", json={"visualStyle": "gold"}, headers={"Origin": "https://other.example"}).status_code, 403)
        self.assertEqual(client.post("/api/motion/a89d53f9/look", json={"visualStyle": "url(https://other.example)"}).status_code, 400)

    def test_story_style_preserves_selected_music_and_beat_timing(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(motion, "folder", return_value=Path(temp)):
            music = {"asset": "music-123456789abc.m4a", "beatFrames": [2, 21, 41], "visualStyle": "gold"}
            (Path(temp) / "music.json").write_text(json.dumps(music), encoding="utf-8")
            result = app.test_client().post("/api/motion/a89d53f9/look", json={"visualStyle": "story"})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["music"], {**music, "visualStyle": "story"})

    def test_music_gain_is_saved_without_replacing_music_or_beat_timing(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(motion, "folder", return_value=Path(temp)):
            music = {"asset": "music-123456789abc.m4a", "beatFrames": [2, 21, 41], "visualStyle": "story"}
            (Path(temp) / "music.json").write_text(json.dumps(music), encoding="utf-8")
            client = app.test_client()
            response = client.post("/api/motion/a89d53f9/mix", json={"gain": 1.8})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["music"], {**music, "gain": 1.8})
            self.assertEqual(client.get("/api/motion/a89d53f9/project").json["music"]["gain"], 1.8)
            for invalid in (-1, 2.1, True, "loud", None):
                self.assertEqual(client.post("/api/motion/a89d53f9/mix", json={"gain": invalid}).status_code, 400)
            self.assertEqual(client.post("/api/motion/a89d53f9/mix", json={"gain": 1}, headers={"Origin":"https://other.example"}).status_code, 403)
            self.assertEqual(json.loads((Path(temp) / "music.json").read_text())["gain"], 1.8)
