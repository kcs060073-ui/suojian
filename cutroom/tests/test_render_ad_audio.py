"""A real short render verifies ad audio output settings, not source text."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RenderAdAudioTests(unittest.TestCase):
    def test_96khz_voice_renders_release_safe_48khz_aac(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            image = work / "image.png"
            voice = work / "voice.wav"
            plan = work / "ad.json"
            output = work / "candidate.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=red:s=90x160", "-frames:v", "1", str(image),
            ], check=True)
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "sine=frequency=880:sample_rate=96000:duration=2.2",
                "-c:a", "pcm_s16le", str(voice),
            ], check=True)
            plan.write_text(json.dumps({
                "name": "qa_audio_probe",
                "ratio": "9:16",
                "total": 2.0,
                "voice": str(voice),
                "segments": [{
                    "i": 0, "text": "测试", "start": 0, "end": 2,
                    "dur": 2, "src": str(image), "matched": True,
                }],
                "output": {"width": 180, "height": 320, "crf": 28},
            }, ensure_ascii=False), encoding="utf-8")
            subprocess.run([
                "node", str(ROOT / "node_modules/ts-node/dist/bin.js"),
                str(ROOT / "scripts/render-ad.ts"), "--plan", str(plan),
                "--output", str(output),
            ], cwd=ROOT, check=True, capture_output=True)
            sample_rate = subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate", "-of", "csv=p=0",
                str(output),
            ], text=True).strip()
            self.assertEqual(sample_rate, "48000")
            measured = subprocess.run([
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(output),
                "-filter_complex", "ebur128=peak=true:framelog=verbose",
                "-f", "null", "-",
            ], capture_output=True, text=True, check=True)
            peaks = re.findall(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", measured.stderr)
            self.assertTrue(peaks)
            self.assertLessEqual(float(peaks[-1]), -1.0)


if __name__ == "__main__":
    unittest.main()
