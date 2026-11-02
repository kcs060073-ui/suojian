from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from scripts.build_release import _write_text_atomic, build_release, validate_contract


ROOT = Path(__file__).resolve().parents[1]


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output = Path(self.temp_dir.name) / "dist"

    def test_release_contract_includes_runtime_sources_and_user_entrypoints(self):
        contract = json.loads(
            (ROOT / "release" / "release-files.json").read_text(encoding="utf-8")
        )

        self.assertIn("media_catalog", contract["directories"])
        self.assertIn("web-ui", contract["directories"])
        self.assertIn("docs/operations", contract["directories"])
        self.assertIn("cutroom_mcp", contract["directories"])
        self.assertIn("requirements-mcp.txt", contract["files"])
        self.assertIn("安装CUTROOM.bat", contract["files"])
        self.assertIn("启动CUTROOM.bat", contract["files"])

    def test_builder_rejects_forbidden_paths_even_if_allowlisted(self):
        with self.assertRaisesRegex(ValueError, "forbidden"):
            validate_contract({"files": [".env"], "directories": []})

    def test_checksum_text_replaces_an_existing_file_atomically(self):
        target = Path(self.temp_dir.name) / "checksum.txt"
        target.write_bytes(b"stale-binary-data" * 512)
        previous_file_id = target.stat().st_ino

        _write_text_atomic(target, "abc123  release.zip\n")

        self.assertEqual(b"abc123  release.zip\n", target.read_bytes())
        self.assertNotEqual(previous_file_id, target.stat().st_ino)

    def test_build_emits_zip_manifest_and_checksum_without_private_data(self):
        self.output.mkdir(parents=True, exist_ok=True)
        legacy_checksum = self.output / "CUTROOM-v0.9.0-beta.8-SHA256SUMS.txt"
        legacy_checksum.write_text("stale checksum", encoding="utf-8")
        result = build_release(
            ROOT,
            self.output,
            "0.9.0-beta.8",
            commit="test-commit",
        )

        self.assertFalse(legacy_checksum.exists())
        self.assertTrue(result.zip_path.is_file())
        self.assertTrue(result.checksum_path.is_file())
        self.assertEqual(result.zip_path.name + ".sha256", result.checksum_path.name)
        expected_checksum = hashlib.sha256(result.zip_path.read_bytes()).hexdigest()
        self.assertEqual(
            f"{expected_checksum}  {result.zip_path.name}\n",
            result.checksum_path.read_text(encoding="utf-8"),
        )
        with zipfile.ZipFile(result.zip_path) as archive:
            names = set(archive.namelist())
            archive.extractall(Path(self.temp_dir.name) / 'extracted')
        package = Path(self.temp_dir.name) / 'extracted' / 'CUTROOM-v0.9.0-beta.8'
        # Use the actual allowlist and extracted sources. Only installed third-party
        # Node dependencies are shared; no module resolver points at checkout src.
        from media_catalog.subtitles import _preset, _runtime_signatures
        _, manifest, font = _preset(package, 'sub-modern-clean-v1')
        self.assertEqual(len(_runtime_signatures(package, manifest, font)), 4)
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for release artifact verification')
        fixture = package / 'public' / 'scripts'
        (fixture / 'smoke.script.txt').write_text('测试字幕。', encoding='utf-8')
        (fixture / 'smoke.words.json').write_text(json.dumps([
            {'w': '测试字幕', 'start': 0, 'end': 1}]), encoding='utf-8')
        (fixture / 'smoke.semantics.json').write_text('{"keywords":[],"punchLines":[]}', encoding='utf-8')
        environment = dict(os.environ, NODE_PATH=str(ROOT / 'node_modules'),
            TS_NODE_PROJECT=str(package / 'tsconfig.json'), TS_NODE_TRANSPILE_ONLY='true')
        completed = subprocess.run([node, str(ROOT / 'node_modules/ts-node/dist/bin.js'),
            str(package / 'scripts/make-ass.ts'), '--script', str(fixture / 'smoke.script.txt'),
            '--words', str(fixture / 'smoke.words.json'), '--semantics', str(fixture / 'smoke.semantics.json'),
            '--output', str(fixture / 'smoke.ass'), '--metadata', str(fixture / 'smoke.subtitle.json')],
            cwd=package, env=environment, capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 0, (completed.stdout + completed.stderr).decode(errors='replace'))
        self.assertIn('测试字幕', (fixture / 'smoke.ass').read_text(encoding='utf-8'))
        # Resolve every relative import in shipped TS sources using the installed
        # compiler against this package, catching transitive omissions as well.
        check = r'''const fs=require('fs'),path=require('path'),ts=require('typescript');
const root=process.cwd();
for(const dir of ['scripts','src']) for(const file of fs.readdirSync(dir,{recursive:true})) {
 if(!file.endsWith('.ts')||file.endsWith('.d.ts')) continue;
 const source=path.join(root,dir,file);
 for(const item of ts.preProcessFile(fs.readFileSync(source,'utf8')).importedFiles) {
  if(item.fileName.startsWith('.')&&!ts.resolveModuleName(item.fileName,source,{},ts.sys).resolvedModule)
   throw Error(source+': unresolved '+item.fileName);
 }
}'''
        completed = subprocess.run([node, '-e', check], cwd=package, env=environment,
            capture_output=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors='replace'))
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        self.assertIsNotNone(ffmpeg, 'FFmpeg is required for release artifact verification')
        self.assertIsNotNone(ffprobe, 'ffprobe is required for release artifact verification')
        video = fixture / 'smoke.mp4'
        rendered = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'color=c=navy:s=180x320:r=10:d=1.2', '-vf',
            'ass=public/scripts/smoke.ass:fontsdir=assets/fonts', '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p', str(video)], cwd=package, capture_output=True, timeout=60)
        self.assertEqual(rendered.returncode, 0, rendered.stderr.decode(errors='replace'))
        probed = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'json', str(video)], capture_output=True, timeout=15, check=True)
        self.assertGreaterEqual(float(json.loads(probed.stdout)['format']['duration']), 1)
        prefix = "CUTROOM-v0.9.0-beta.8/"
        self.assertIn(prefix + "release-manifest.json", names)
        self.assertIn(prefix + "release/install.ps1", names)
        self.assertIn(prefix + "requirements-mcp.txt", names)
        self.assertIn(prefix + "cutroom_mcp/server.py", names)
        for relative in (
            "scripts/cutroom_mcp.py",
            "scripts/probe-cutroom-mcp.py",
            "scripts/install-workbuddy-mcp.ps1",
            "scripts/uninstall-workbuddy-mcp.ps1",
            "scripts/show-doubao-work-mcp-config.ps1",
            "scripts/start-doubao-work-mcp.ps1",
            "docs/operations/cutroom-agent-mcp-clients.md",
        ):
            self.assertIn(prefix + relative, names)
        self.assertIn(prefix + "启动CUTROOM.bat", names)
        self.assertIn(prefix + "media_catalog/migrations/030_tts_voice_catalog.sql", names)
        self.assertFalse(
            any(name.startswith(prefix + "media_catalog/media_catalog/") for name in names)
        )
        self.assertIn(prefix + "docs/operations/model-profiles-new-api.md", names)
        for web_asset in (
            "web-ui/public/index.html",
            "web-ui/public/app.mjs",
            "web-ui/public/settings-view.mjs",
            "web-ui/public/task-create.mjs",
            "web-ui/public/styles.css",
        ):
            self.assertIn(prefix + web_asset, names)
        for category in (
            "subtitle-presets",
            "voice-profiles",
            "bgm",
            "sfx",
            "visual-cards",
            "stickers",
            "fancy-text-presets",
            "brand",
        ):
            self.assertIn(prefix + f"assets-local/{category}/inbox/.gitkeep", names)
        self.assertFalse(any(Path(name).name == ".env" for name in names))
        self.assertFalse(any("/runtime/" in name and not name.endswith("/.gitkeep") for name in names))
        self.assertFalse(any("/assets-local/" in name and not name.endswith("/.gitkeep") for name in names))
        self.assertFalse(any(name.endswith((".sqlite", ".sqlite3", ".db")) for name in names))
        self.assertFalse(any(Path(name).name == "session-signing.key" for name in names))
        self.assertFalse(any(".workbuddy" in name.lower() for name in names))
        self.assertFalse(any(".doubaowork" in name.lower() for name in names))
        self.assertFalse(any(name.lower().endswith("mcp.json") for name in names))

    def test_installer_provisions_all_required_runtime_dependencies(self):
        install_script = (ROOT / "release" / "install.ps1").read_text(encoding="utf-8")

        self.assertIn("Python.Python.3.11", install_script)
        self.assertIn("OpenJS.NodeJS.LTS", install_script)
        self.assertIn("Gyan.FFmpeg", install_script)
        self.assertIn("-m venv", install_script)
        self.assertIn("-m pip install -r", install_script)
        self.assertIn("npm ci", install_script)

    def test_installer_uses_package_local_temp_for_dependency_installation(self):
        install_script = (ROOT / "release" / "install.ps1").read_text(encoding="utf-8")

        self.assertIn('Join-Path $projectRoot "runtime\\install-temp"', install_script)
        self.assertIn("$env:TEMP = $installTemp", install_script)
        self.assertIn("$env:TMP = $installTemp", install_script)
        self.assertLess(
            install_script.index("$env:TEMP = $installTemp"),
            install_script.index('Write-Stage "安装 Python 依赖"'),
        )

    def test_normal_installer_keeps_mcp_registration_optional(self):
        install_script = (ROOT / "release" / "install.ps1").read_text(encoding="utf-8")

        self.assertIn("requirements.txt", install_script)
        self.assertNotIn("& $mcpInstaller", install_script)
        self.assertNotIn("requirements-mcp.txt", install_script)

    def test_installer_does_not_truncate_native_version_checks_through_a_pipeline(self):
        install_script = (ROOT / "release" / "install.ps1").read_text(encoding="utf-8")

        self.assertNotIn(
            "& ffmpeg.exe -version | Select-Object -First 1",
            install_script,
        )
        self.assertNotIn(
            "& ffprobe.exe -version | Select-Object -First 1",
            install_script,
        )

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 only")
    def test_installer_parses_in_windows_powershell_51(self):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell 5.1 is unavailable")
        install_script = ROOT / "release" / "install.ps1"
        literal_path = str(install_script).replace("'", "''")
        command = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{literal_path}', [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -ne 0) { "
            "$errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )

        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        details = (completed.stdout + completed.stderr).decode(
            "utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, details)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 only")
    def test_python_probe_tolerates_an_unavailable_py_selector(self):
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("Windows PowerShell 5.1 is unavailable")
        install_script = ROOT / "release" / "install.ps1"
        literal_path = str(install_script).replace("'", "''")
        command = (
            f"$raw=Get-Content -Raw -LiteralPath '{literal_path}'; "
            "$start=$raw.IndexOf('function Resolve-Python311'); "
            "$prefix='$ErrorActionPreference = \"Stop\"; ' + "
            "$raw.Substring($start,$raw.IndexOf('try {')-$start); "
            "Invoke-Expression $prefix; "
            "$result=Resolve-Python311; "
            "if ($result) { Write-Output $result } else { Write-Output '<none>' }"
        )

        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        details = (completed.stdout + completed.stderr).decode(
            "utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 0, details)

    def test_python_runtime_dependencies_are_pinned(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("faster-whisper==1.2.1", requirements)
        self.assertIn("numpy==2.4.6", requirements)
        self.assertIn("Pillow==12.2.0", requirements)
        self.assertIn("volcengine-python-sdk==5.0.48", requirements)

    def test_double_click_entrypoints_call_the_supported_scripts(self):
        installer = (ROOT / "安装CUTROOM.bat").read_text(encoding="utf-8")
        launcher = (ROOT / "启动CUTROOM.bat").read_text(encoding="utf-8")

        self.assertIn("release\\install.ps1", installer)
        self.assertIn("ExecutionPolicy Bypass", installer)
        self.assertIn("workbench\\start-local.ps1", launcher)
        service_runner = (ROOT / "workbench" / "start-local.ps1").read_text(encoding="utf-8")
        self.assertIn("workbench.server", service_runner)
        self.assertIn("http://127.0.0.1:8765/", launcher)

    def test_beta_version_is_consistent(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))

        self.assertEqual("0.9.0-beta.8", package["version"])
        self.assertEqual("0.9.0-beta.8", lock["version"])
        self.assertEqual("0.9.0-beta.8", lock["packages"][""]["version"])

    def test_release_notes_and_maintenance_guidance_are_complete(self):
        notes = (ROOT / "release" / "RELEASE_NOTES_v0.9.0-beta.8.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "user-guide" / "cutroom-user-guide.md").read_text(encoding="utf-8")
        operations = (ROOT / "docs" / "operations" / "model-profiles-new-api.md").read_text(encoding="utf-8")

        self.assertIn("Beta", notes)
        self.assertIn("普通同事电脑", notes)
        download_url = (
            "https://github.com/qnwhageqlavs-jpg/cutroom/"
            "releases/tag/v0.9.0-beta.8"
        )
        self.assertIn(download_url, readme)
        self.assertIn(download_url, guide)
        self.assertIn("D:\\CUTROOM-v0.9.0-beta.8", notes)
        self.assertIn("runtime\\install-temp", notes)
        self.assertIn("不要关闭窗口", notes)
        self.assertNotIn("AK/SK、AppID 和 Access Token", guide)
        for phrase in ("Access Key ID", "Secret Access Key", "本条视频音色", "Windows 凭据管理器"):
            self.assertIn(phrase, guide + operations)
        for phrase in ("安装CUTROOM.bat", "启动CUTROOM.bat", "升级", "回退", "卸载", "署名"):
            self.assertIn(phrase, guide)



    def test_colleague_acceptance_guidance_is_linked_and_complete(self):
        acceptance_path = (
            ROOT
            / "docs"
            / "validation"
            / "cutroom-colleague-first-real-task-acceptance.md"
        )
        self.assertTrue(acceptance_path.is_file())

        acceptance = acceptance_path.read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (
            ROOT / "docs" / "user-guide" / "cutroom-user-guide.md"
        ).read_text(encoding="utf-8")
        link = "./docs/validation/cutroom-colleague-first-real-task-acceptance.md"
        guide_link = "../validation/cutroom-colleague-first-real-task-acceptance.md"

        self.assertIn(link, readme)
        self.assertIn(guide_link, guide)
        for phrase in (
            "最终文案确认",
            "画面和顺序确认",
            "本片包装方案确认",
            "完整观看后的成片确认",
            "人工操作时间",
            "总完成时间",
            "七文件发布包",
            "普通同事本人",
        ):
            self.assertIn(phrase, acceptance)

if __name__ == "__main__":
    unittest.main()
