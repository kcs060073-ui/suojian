from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-codex-mcp.ps1"
UNINSTALLER = ROOT / "scripts" / "uninstall-codex-mcp.ps1"
WORKBUDDY_INSTALLER = ROOT / "scripts" / "install-workbuddy-mcp.ps1"
WORKBUDDY_UNINSTALLER = ROOT / "scripts" / "uninstall-workbuddy-mcp.ps1"


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 only")
class McpInstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.powershell = shutil.which("powershell.exe")
        if self.powershell is None:
            self.skipTest("Windows PowerShell 5.1 is unavailable")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def run_script(self, script: Path, config: Path, *extra: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ConfigPath",
                str(config),
                *extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_scripts_exist_and_parse_in_windows_powershell_51(self):
        for script in (
            INSTALLER,
            UNINSTALLER,
            WORKBUDDY_INSTALLER,
            WORKBUDDY_UNINSTALLER,
        ):
            self.assertTrue(script.is_file())
            literal = str(script).replace("'", "''")
            command = (
                "$tokens=$null; $errors=$null; "
                "[System.Management.Automation.Language.Parser]::ParseFile("
                f"'{literal}', [ref]$tokens, [ref]$errors) | Out-Null; "
                "if ($errors.Count -ne 0) { exit 1 }"
            )
            completed = subprocess.run(
                [self.powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))

    def test_toml_dry_run_is_read_only_and_real_install_preserves_other_servers(self):
        config = self.root / "config.toml"
        original = (
            'model = "gpt-test"\n\n'
            '[mcp_servers.other]\n'
            "command = 'other.exe'\n"
            "env = { TOKEN = 'TOP_SECRET_MARKER' }\n"
        )
        config.write_text(original, encoding="utf-8")

        dry_run = self.run_script(INSTALLER, config, "-DryRun")
        self.assertEqual(dry_run.returncode, 0, (dry_run.stdout + dry_run.stderr).decode(errors="replace"))
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        output = dry_run.stdout.decode("utf-8", errors="replace")
        self.assertIn("[mcp_servers.cutroom]", output)
        preview = tomllib.loads(output[output.index('[mcp_servers.cutroom]'):])['mcp_servers']['cutroom']
        self.assertEqual(preview['command'], str(ROOT / ".venv" / "Scripts" / "python.exe"))
        self.assertEqual(preview['args'][0], str(ROOT / "scripts" / "cutroom_mcp.py"))
        self.assertIn("--client", output)
        self.assertIn("codex", output)
        self.assertNotIn("TOP_SECRET_MARKER", output)

        installed = self.run_script(INSTALLER, config)
        self.assertEqual(installed.returncode, 0, (installed.stdout + installed.stderr).decode(errors="replace"))
        text = config.read_text(encoding="utf-8")
        self.assertIn('[mcp_servers.other]', text)
        self.assertEqual(text.count("[mcp_servers.cutroom]"), 1)

        installed_again = self.run_script(INSTALLER, config)
        self.assertEqual(installed_again.returncode, 0)
        self.assertEqual(config.read_text(encoding="utf-8").count("[mcp_servers.cutroom]"), 1)

    def test_json_install_and_uninstall_touch_only_cutroom_entry(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "theme": "dark",
            "mcpServers": {
                "other": {"command": "other.exe", "args": []},
                "cutroom": {"command": "stale.exe", "args": []},
            },
        }), encoding="utf-8")
        sentinel = self.root / "catalog.sqlite"
        sentinel.write_text("do-not-delete", encoding="utf-8")

        installed = self.run_script(INSTALLER, config)
        self.assertEqual(installed.returncode, 0, (installed.stdout + installed.stderr).decode(errors="replace"))
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["theme"], "dark")
        self.assertIn("other", data["mcpServers"])
        self.assertEqual(Path(data["mcpServers"]["cutroom"]["cwd"]), ROOT)

        removed = self.run_script(UNINSTALLER, config)
        self.assertEqual(removed.returncode, 0, (removed.stdout + removed.stderr).decode(errors="replace"))
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(set(data["mcpServers"]), {"other"})
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-delete")

    def test_uninstall_dry_run_does_not_echo_unrelated_config(self):
        config = self.root / "config.toml"
        config.write_text(
            "[mcp_servers.other]\n"
            "env = { TOKEN = 'TOP_SECRET_MARKER' }\n\n"
            "[mcp_servers.cutroom]\n"
            "command = 'python.exe'\n",
            encoding="utf-8",
        )

        removed = self.run_script(UNINSTALLER, config, "-DryRun")

        self.assertEqual(removed.returncode, 0, (removed.stdout + removed.stderr).decode(errors="replace"))
        self.assertNotIn("TOP_SECRET_MARKER", removed.stdout.decode("utf-8", errors="replace"))

    def test_toml_uninstall_without_cutroom_is_byte_for_byte_noop(self):
        config = self.root / "config.toml"
        original = b'model = "gpt-test"\n\n[mcp_servers.other]\ncommand = "other.exe"\n'
        config.write_bytes(original)

        removed = self.run_script(UNINSTALLER, config)

        self.assertEqual(removed.returncode, 0, (removed.stdout + removed.stderr).decode(errors="replace"))
        self.assertEqual(config.read_bytes(), original)

    def test_toml_edit_preserves_array_tables_quoted_names_and_multiline_strings(self):
        original = '''model = "test"
notes = """configuration example:
[mcp_servers.cutroom]
this is literal documentation
"""
[mcp_servers.cutroom]
command = "old.exe"
[mcp_servers.cutroom.env]
MODE = "old"
[[skills.config]]
path = "C:/safe-skill/SKILL.md"
enabled = false
[mcp_servers."cutroom.extra"]
command = "unrelated.exe"
notes = ''' + "'''" + '''
[[mcp_servers.cutroom]]
literal example
''' + "'''" + '''
[mcp_servers.other]
command = "other.exe"
'''
        expected = tomllib.loads(original)
        del expected['mcp_servers']['cutroom']
        for script in (INSTALLER, UNINSTALLER):
            with self.subTest(script=script.name):
                config = self.root / 'complex.toml'
                config.write_text(original, encoding='utf-8')
                result = self.run_script(script, config)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
                actual = tomllib.loads(config.read_text(encoding='utf-8'))
                if script == INSTALLER:
                    self.assertEqual(actual['mcp_servers']['cutroom']['cwd'], str(ROOT))
                    del actual['mcp_servers']['cutroom']
                self.assertEqual(actual, expected)

    def test_toml_uninstall_handles_quoted_root_and_array_target(self):
        for target in ('["mcp_servers"."cutroom".env]\nX="test"\n',
                       '[[mcp_servers.cutroom]]\ncommand="old"\n'):
            with self.subTest(target=target):
                config = self.root / 'quoted.toml'
                config.write_text(target + '[[skills.config]]\npath="safe"\n', encoding='utf-8')
                result = self.run_script(UNINSTALLER, config)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
                actual = tomllib.loads(config.read_text(encoding='utf-8'))
                self.assertNotIn('cutroom', actual.get('mcp_servers', {}))
                self.assertEqual(actual['skills'], {'config': [{'path': 'safe'}]})

    def test_toml_unsafe_or_invalid_edit_does_not_overwrite_config(self):
        for original in (b'[mcp_servers.cutroom]\ncommand="unterminated',
                         b'mcp_servers = {cutroom={command="old"}, other={command="safe"}}\n'):
            for script in (INSTALLER, UNINSTALLER):
                with self.subTest(script=script.name, original=original):
                    config = self.root / 'unsafe.toml'
                    config.write_bytes(original)
                    result = self.run_script(script, config)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(config.read_bytes(), original)

    def test_uninstaller_source_contains_no_cutroom_data_deletion(self):
        for script in (UNINSTALLER, WORKBUDDY_UNINSTALLER):
            text = script.read_text(encoding="utf-8").lower()
            self.assertNotIn("remove-item $projectroot", text)
            self.assertNotIn("catalog.sqlite", text)
            self.assertNotIn("runtime\\workbench", text)

    def test_workbuddy_install_preserves_existing_servers_and_redacts_dry_run(self):
        config = self.root / "mcp.json"
        original = {
            "theme": "dark",
            "mcpServers": {
                "zhaoshang_wechat_bridge": {
                    "command": "bridge.exe",
                    "args": [],
                    "env": {"TOKEN": "TOP_SECRET_MARKER"},
                }
            },
        }
        config.write_text(json.dumps(original), encoding="utf-8")

        dry_run = self.run_script(WORKBUDDY_INSTALLER, config, "-DryRun")
        self.assertEqual(
            dry_run.returncode,
            0,
            (dry_run.stdout + dry_run.stderr).decode(errors="replace"),
        )
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")), original)
        preview = dry_run.stdout.decode("utf-8", errors="replace")
        self.assertIn('"cutroom"', preview)
        self.assertIn('"workbuddy"', preview)
        self.assertNotIn("TOP_SECRET_MARKER", preview)

        installed = self.run_script(WORKBUDDY_INSTALLER, config)
        self.assertEqual(
            installed.returncode,
            0,
            (installed.stdout + installed.stderr).decode(errors="replace"),
        )
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(
            data["mcpServers"]["zhaoshang_wechat_bridge"],
            original["mcpServers"]["zhaoshang_wechat_bridge"],
        )
        self.assertEqual(data["mcpServers"]["cutroom"]["type"], "stdio")
        self.assertEqual(
            data["mcpServers"]["cutroom"]["args"][-2:],
            ["--client", "workbuddy"],
        )
        self.assertEqual(Path(data["mcpServers"]["cutroom"]["cwd"]), ROOT)

    def test_workbuddy_install_rejects_invalid_json_without_overwrite(self):
        config = self.root / "mcp.json"
        original = b'{"mcpServers": '
        config.write_bytes(original)

        completed = self.run_script(WORKBUDDY_INSTALLER, config)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(config.read_bytes(), original)

    def test_workbuddy_reinstall_is_idempotent_and_uninstall_removes_only_cutroom(self):
        config = self.root / "mcp.json"
        original_other = {"command": "other.exe", "env": {"TOKEN": "SECRET"}}
        config.write_text(
            json.dumps({"mcpServers": {"other": original_other}}),
            encoding="utf-8",
        )

        self.assertEqual(self.run_script(WORKBUDDY_INSTALLER, config).returncode, 0)
        first = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(self.run_script(WORKBUDDY_INSTALLER, config).returncode, 0)
        second = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(first, second)

        dry_run = self.run_script(WORKBUDDY_UNINSTALLER, config, "-DryRun")
        self.assertEqual(dry_run.returncode, 0)
        self.assertNotIn("SECRET", dry_run.stdout.decode("utf-8", errors="replace"))
        self.assertEqual(json.loads(config.read_text(encoding="utf-8-sig")), second)

        self.assertEqual(self.run_script(WORKBUDDY_UNINSTALLER, config).returncode, 0)
        removed = json.loads(config.read_text(encoding="utf-8-sig"))
        self.assertEqual(set(removed["mcpServers"]), {"other"})
        self.assertEqual(removed["mcpServers"]["other"], original_other)

    def test_workbuddy_uninstall_without_cutroom_is_byte_for_byte_noop(self):
        config = self.root / "mcp.json"
        original = b'{"mcpServers":{"other":{"command":"other.exe"}}}\n'
        config.write_bytes(original)

        removed = self.run_script(WORKBUDDY_UNINSTALLER, config)

        self.assertEqual(
            removed.returncode,
            0,
            (removed.stdout + removed.stderr).decode(errors="replace"),
        )
        self.assertEqual(config.read_bytes(), original)

    def test_user_docs_explain_mode_boundary_task_url_and_safe_rollback(self):
        documents = "\n".join((ROOT / relative).read_text(encoding="utf-8") for relative in (
            "README.md",
            "web-ui/README.md",
            "web-ui/public/help.html",
            "docs/user-guide/cutroom-user-guide.md",
            "docs/operations/media-catalog-agent-contract.md",
        ))
        for phrase in (
            "独立模式：CUTROOM 自己调用已配置的文案/分镜模型",
            "Agent 模式：当前 Codex 模型完成文案与分镜",
            "豆包 TTS、Seedance 等生产服务仍使用 CUTROOM 各自凭据",
            "只移除 Codex 中的 cutroom 服务项",
            "http://127.0.0.1:8765/#/tasks/<task-id>",
            "四个人工门禁",
        ):
            self.assertIn(phrase, documents)
        self.assertNotIn("?startup-key=", documents)


if __name__ == "__main__":
    unittest.main()
