import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ad_tts_runner import main


class AdTtsRunnerTests(unittest.TestCase):
    def test_tts_runner_environment_contains_tts_but_no_text_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "catalog.sqlite"
            database.touch()
            plan = root / "adplan.json"
            plan.write_text('{"ratio":"9:16","segments":[]}', encoding="utf-8")
            argv = [
                "--project-root",
                str(root),
                "--node",
                str(root / "node.exe"),
                "--ts-node",
                str(root / "ts-node.js"),
                "--catalog-db",
                str(database),
                "--task-id",
                "agent-task",
                "--plan-input",
                str(plan),
                "--timing-output",
                str(root / "adtiming.json"),
                "--voice-output",
                str(root / "voice.mp3"),
                "--segments-dir",
                str(root / "segments"),
            ]
            environment = {
                "TTS_ACCESS_TOKEN": "tts-secret",
                "TTS_VOICE_TYPE": "voice-a",
                "ARK_API_KEY": "text-secret",
                "LLM_API_KEY": "other-text-secret",
                "OPENAI_API_KEY": "openai-text-secret",
            }
            with (
                patch(
                    "media_catalog.model_runtime.task_model_environment",
                    return_value=(environment, {"capability": "tts"}),
                ),
                patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run,
            ):
                self.assertEqual(main(argv), 0)

        command = run.call_args.args[0]
        process_environment = run.call_args.kwargs["env"]
        self.assertIn("tts.ts", " ".join(command))
        self.assertNotIn("plan-ad.ts", " ".join(command))
        self.assertEqual(process_environment["TTS_ACCESS_TOKEN"], "tts-secret")
        self.assertNotIn("ARK_API_KEY", process_environment)
        self.assertNotIn("LLM_API_KEY", process_environment)
        self.assertNotIn("OPENAI_API_KEY", process_environment)


if __name__ == "__main__":
    unittest.main()
