import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/media_catalog.py", *args],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


class PackagingCliTests(unittest.TestCase):
    def test_assets_confirm_requires_preview_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "catalog.sqlite"
            result = run_cli([
                "--db", str(db_path), "assets", "confirm", "--plan", "plan-1"
            ])
            self.assertFalse(db_path.exists())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--preview-id", result.stderr)


if __name__ == "__main__":
    unittest.main()
