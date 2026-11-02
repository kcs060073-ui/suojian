import tempfile
import unittest
from pathlib import Path

from media_catalog.tools import ToolNotFound, find_ffprobe


class ToolTests(unittest.TestCase):
    def test_explicit_ffprobe_path_wins(self):
        result = find_ffprobe(
            {"FFPROBE_PATH": r"C:\tools\ffprobe.exe"},
            lambda _: None,
            [],
            exists=lambda path: True,
        )
        self.assertEqual(result, Path(r"C:\tools\ffprobe.exe"))

    def test_path_lookup_is_used_before_install_root_search(self):
        result = find_ffprobe(
            {},
            lambda _: r"C:\path\ffprobe.exe",
            [Path(r"D:\ffmpeg")],
            exists=lambda path: True,
        )
        self.assertEqual(result, Path(r"C:\path\ffprobe.exe"))

    def test_known_install_root_can_find_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "ffmpeg" / "bin" / "ffprobe.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            result = find_ffprobe({}, lambda _: None, [Path(tmp)])
            self.assertEqual(result, executable)

    def test_missing_tool_has_actionable_message(self):
        with self.assertRaisesRegex(ToolNotFound, "FFPROBE_PATH"):
            find_ffprobe({}, lambda _: None, [], exists=lambda path: False)


if __name__ == "__main__":
    unittest.main()
