import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from media_catalog.cli import build_parser, configure_stdio, main


class CliTests(unittest.TestCase):
    def test_assemble_requires_complete_three_source_subtitle_set_and_rejects_ass_conflicts(self):
        parser = build_parser()
        valid = parser.parse_args([
            "assemble", "--task", "task-1", "--render",
            "--script", "script.txt", "--words", "words.json",
            "--semantics", "semantics.json", "--output", "out.mp4",
        ])
        self.assertEqual(valid.semantics, "semantics.json")

        for args in (
            ["assemble", "--task", "task-1", "--render", "--script", "script.txt", "--words", "words.json"],
            ["assemble", "--task", "task-1", "--render", "--semantics", "semantics.json"],
            ["assemble", "--task", "task-1", "--render", "--ass", "captions.ass", "--semantics", "semantics.json"],
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                parser.parse_args(args)

    def test_stdio_is_reconfigured_to_utf8_when_supported(self):
        class Stream:
            def __init__(self):
                self.calls = []

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        stdout = Stream()
        stderr = Stream()
        configure_stdio(stdout, stderr)
        self.assertEqual(stdout.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(stderr.calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_status_json_has_stable_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            db_path = Path(tmp) / "catalog.sqlite"
            self.assertEqual(main(["--db", str(db_path), "init"]), 0)
            with redirect_stdout(output):
                self.assertEqual(
                    main(["--db", str(db_path), "status", "--json"]),
                    0,
                )
            result = json.loads(output.getvalue())
            required = {"libraries", "assets", "jobs", "warnings"}
            self.assertTrue(required.issubset(result))
            self.assertIn("p1", result)

    def test_catalog_only_scan_and_search_use_readable_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            media.mkdir()
            (media / "厂房航拍.mp4").write_bytes(b"video")
            db_path = root / "catalog.sqlite"
            self.assertEqual(main(["--db", str(db_path), "init"]), 0)
            self.assertEqual(
                main(
                    [
                        "--db",
                        str(db_path),
                        "library",
                        "add",
                        "--id",
                        "park-01",
                        "--name",
                        "园区素材",
                        "--kind",
                        "local",
                        "--mount",
                        str(media),
                    ]
                ),
                0,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db_path),
                            "scan",
                            "--library",
                            "park-01",
                            "--catalog-only",
                        ]
                    ),
                    0,
                )
            self.assertIn("已登记素材：1", output.getvalue())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db_path),
                            "search",
                            "厂房",
                            "--json",
                        ]
                    ),
                    0,
                )
            result = json.loads(output.getvalue())
            self.assertEqual(result[0]["relative_path"], "厂房航拍.mp4")

    def test_error_is_concise_and_does_not_print_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = io.StringIO()
            with redirect_stderr(error):
                code = main(
                    [
                        "--db",
                        str(Path(tmp) / "catalog.sqlite"),
                        "library",
                        "mount",
                        "--id",
                        "missing",
                        "--mount",
                        tmp,
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("找不到素材库", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())

    def test_json_error_has_stable_agent_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "offline-media"
            db_path = root / "catalog.sqlite"
            self.assertEqual(main(["--db", str(db_path), "init"]), 0)
            self.assertEqual(
                main(
                    [
                        "--db",
                        str(db_path),
                        "library",
                        "add",
                        "--id",
                        "offline",
                        "--name",
                        "离线素材库",
                        "--kind",
                        "nas",
                        "--mount",
                        str(missing),
                    ]
                ),
                0,
            )
            error = io.StringIO()
            with redirect_stderr(error):
                code = main(
                    [
                        "--db",
                        str(db_path),
                        "scan",
                        "--library",
                        "offline",
                        "--catalog-only",
                        "--json",
                    ]
                )
            self.assertEqual(code, 1)
            result = json.loads(error.getvalue())
            self.assertEqual(set(result), {"ok", "error"})
            self.assertFalse(result["ok"])
            self.assertEqual(
                set(result["error"]),
                {"code", "retryable", "user_message", "next_action"},
            )
            self.assertEqual(result["error"]["code"], "LIBRARY_OFFLINE")
            self.assertTrue(result["error"]["retryable"])
            self.assertNotIn("Traceback", error.getvalue())

    def test_probe_command_is_separate_resumable_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            from PIL import Image

            root = Path(tmp)
            media = root / "media"
            media.mkdir()
            Image.new("RGB", (16, 9)).save(media / "园区.png")
            db_path = root / "catalog.sqlite"
            self.assertEqual(main(["--db", str(db_path), "init"]), 0)
            self.assertEqual(
                main(
                    [
                        "--db",
                        str(db_path),
                        "library",
                        "add",
                        "--id",
                        "park",
                        "--name",
                        "园区",
                        "--kind",
                        "local",
                        "--mount",
                        str(media),
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "--db",
                        str(db_path),
                        "scan",
                        "--library",
                        "park",
                        "--catalog-only",
                    ]
                ),
                0,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(db_path),
                            "probe",
                            "--library",
                            "park",
                            "--workers",
                            "2",
                            "--json",
                        ]
                    ),
                    0,
                )
            result = json.loads(output.getvalue())
            self.assertTrue(result["ok"])
            self.assertEqual(result["technical_probe"]["completed"], 1)
            self.assertEqual(result["progress"]["waiting"], 0)

    def test_probe_rejects_more_than_four_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = io.StringIO()
            with redirect_stderr(error):
                code = main(
                    [
                        "--db",
                        str(Path(tmp) / "catalog.sqlite"),
                        "probe",
                        "--library",
                        "park",
                        "--workers",
                        "5",
                        "--json",
                    ]
                )
            self.assertEqual(code, 1)
            result = json.loads(error.getvalue())
            self.assertEqual(result["error"]["code"], "INVALID_REQUEST")

    def test_help_describes_local_and_removable_drives_for_teammates(self):
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_info:
                main(["library", "--help"])
        self.assertEqual(exit_info.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("本机或移动硬盘", help_text)
        self.assertNotIn("本机或 NAS", help_text)


if __name__ == "__main__":
    unittest.main()
