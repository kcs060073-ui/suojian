from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "show-doubao-work-mcp-config.ps1"
LAUNCHER = ROOT / "scripts" / "start-doubao-work-mcp.ps1"


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 only")
class DoubaoWorkMcpConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.powershell = shutil.which("powershell.exe")
        if self.powershell is None:
            self.skipTest("Windows PowerShell 5.1 is unavailable")

    def test_descriptor_is_exact_loopback_http_and_user_data_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_local_app_data = Path(tmp) / "LocalAppData"
            fake_doubao_data = fake_local_app_data / "DoubaoWork" / "User Data"
            fake_doubao_data.mkdir(parents=True)
            marker = fake_doubao_data / "marker.json"
            marker.write_text('{"preserve":true}', encoding="utf-8")
            token_file = Path(tmp) / "doubao-work.token"
            token_file.write_text("test-doubao-token-32-bytes-value", encoding="utf-8")
            before = {
                path.relative_to(fake_local_app_data): path.read_bytes()
                for path in fake_local_app_data.rglob("*")
                if path.is_file()
            }
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(fake_local_app_data)

            completed = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(SCRIPT),
                    "-TokenFile",
                    str(token_file),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode(errors="replace"),
            )
            descriptor = json.loads(completed.stdout.decode("utf-8-sig"))
            self.assertEqual(
                set(descriptor),
                {"name", "transport", "url", "headers"},
            )
            self.assertEqual(descriptor["name"], "cutroom")
            self.assertEqual(descriptor["transport"], "http")
            self.assertEqual(descriptor["url"], "http://127.0.0.1:8766/mcp")
            self.assertEqual(
                descriptor["headers"],
                {"Authorization": "Bearer test-doubao-token-32-bytes-value"},
            )
            after = {
                path.relative_to(fake_local_app_data): path.read_bytes()
                for path in fake_local_app_data.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_foreground_launcher_binds_only_loopback_as_doubao_work(self):
        self.assertTrue(LAUNCHER.is_file())
        source = LAUNCHER.read_text(encoding="utf-8-sig")
        self.assertIn('"--client", "doubao_work"', source)
        self.assertIn('"--transport", "streamable-http"', source)
        self.assertIn('"--port", "8766"', source)
        self.assertIn("RandomNumberGenerator", source)
        self.assertIn("CUTROOM_MCP_BEARER_TOKEN", source)
        self.assertNotIn("0.0.0.0", source)

    def test_scripts_parse_in_windows_powershell_51(self):
        for script in (SCRIPT, LAUNCHER):
            command = (
                "$tokens=$null;$errors=$null;"
                f"[System.Management.Automation.Language.Parser]::ParseFile('{script}',"
                "[ref]$tokens,[ref]$errors)|Out-Null;"
                "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
            )
            completed = subprocess.run(
                [self.powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode(errors="replace"),
            )


if __name__ == "__main__":
    unittest.main()
