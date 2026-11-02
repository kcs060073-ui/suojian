import hashlib
import io
import json
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog import packaging_plan
from media_catalog.assemble import assemble
from media_catalog.render_mix_adapter import (
    RenderMixJobError,
    compile_render_mix_job,
    verify_catalog_snapshot,
)
from media_catalog.subtitles import compile_task_ass
from tests.test_render_mix_adapter import RenderMixCatalogFixture


def valid_test_audio() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 800)
    return buffer.getvalue()


def signature(path: Path) -> dict:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class SubtitleBindingTests(RenderMixCatalogFixture, unittest.TestCase):
    preset_id = "sub-modern-clean-v1"

    def setUp(self):
        super().setUp()
        self.subtitle_plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="subtitle binding fixture"
        )
        self.work_dir = self.root / "work"
        self.output_path = self.root / "out" / "task-1.mp4"
        self.script_path = self.root / "locked.script.txt"
        self.words_path = self.root / "locked.words.json"
        self.semantics_path = self.root / "locked.semantics.json"
        self.script_path.write_text("新疆算力", encoding="utf-8")
        self.words_path.write_text(
            json.dumps([
                {"w": "新疆", "start": 0.0, "end": 0.8},
                {"w": "算力", "start": 0.8, "end": 1.6},
            ], ensure_ascii=False),
            encoding="utf-8",
        )
        self.semantics_path.write_text(
            json.dumps({"keywords": ["新疆"], "punchLines": ["新疆算力"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.manifest_dir = self.root / "assets" / "manifests"
        self.font_path = self.root / "assets" / "fonts" / "modern.otf"
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.font_path.parent.mkdir(parents=True, exist_ok=True)
        self.font_path.write_bytes(b"modern-font")
        for relative in (
            "scripts/make-ass.ts",
            "src/subtitle-ass.ts",
            "src/subtitle-alignment.ts",
            "node_modules/ts-node/dist/bin.js",
        ):
            runtime_source = self.root / relative
            runtime_source.parent.mkdir(parents=True, exist_ok=True)
            runtime_source.write_text(f"// {relative}", encoding="utf-8")
        (self.manifest_dir / "subtitle-presets.json").write_text(
            json.dumps({
                "items": [{
                    "id": self.preset_id,
                    "font": self.font_path.name,
                    "fontname_guess": "Modern",
                    "size": 72,
                    "primary_hex": "#FFFFFF",
                    "outline_hex": "#000000",
                    "shadow_hex": "#000000",
                    "bold": True,
                    "alignment": 2,
                    "margin_v": 430,
                    "outline": 4,
                    "shadow": 1,
                    "border_style": 1,
                    "license": "commercial-approved",
                }]
            }),
            encoding="utf-8",
        )
        self.manifest_patch = patch.object(
            packaging_plan, "MANIFEST_DIR", self.manifest_dir
        )
        self.manifest_patch.start()
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM packaging_plan_items WHERE plan_id=? AND slot='subtitle'",
                (self.subtitle_plan_id,),
            )
            connection.execute(
                "INSERT INTO packaging_plan_items(plan_id, slot, asset_ref) "
                "VALUES(?, 'subtitle', ?)",
                (self.subtitle_plan_id, self.preset_id),
            )
        packaging_plan.confirm_plan(self.db, self.subtitle_plan_id)
        self.subtitle_calls = []

    def tearDown(self):
        self.manifest_patch.stop()
        super().tearDown()

    def subtitle_runner(self, command, **kwargs):
        self.subtitle_calls.append((command, kwargs))
        output = Path(command[command.index("--output") + 1])
        metadata = Path(command[command.index("--metadata") + 1])
        preset = command[command.index("--preset") + 1]
        ass_bytes = (
            "[V4+ Styles]\n"
            "Style: Default,Modern,72,&H00FFFFFF&,x,x,x,1\n"
            "[Events]\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,新疆算力\n"
        ).encode("utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(ass_bytes)
        metadata.write_text(
            json.dumps({
                "schema": 1,
                "preset": preset,
                "coverage": 1.0,
                "speed": 1.25,
                "lead": 0.08,
                "cues": [{"text": "新疆算力", "start": 0.0, "end": 1.0}],
                "assSha256": hashlib.sha256(ass_bytes).hexdigest(),
            }),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def compile_job(self, *, ass_path=None, subtitle_sources=None):
        return compile_render_mix_job(
            self.db,
            "task-1",
            self.work_dir,
            self.output_path,
            ass_path,
            subtitle_sources=subtitle_sources,
            subtitle_runner=self.subtitle_runner,
        )

    def locked_sources(self, **overrides):
        sources = {
            "script_path": self.script_path,
            "words_path": self.words_path,
            "semantics_path": self.semantics_path,
        }
        sources.update(overrides)
        return sources

    def test_confirmed_preset_controls_generated_task_ass(self):
        job = self.compile_job(subtitle_sources=self.locked_sources())

        self.assertEqual(len(self.subtitle_calls), 1)
        command, kwargs = self.subtitle_calls[0]
        self.assertEqual(command[command.index("--preset") + 1], self.preset_id)
        self.assertEqual(
            Path(command[command.index("--semantics") + 1]),
            self.semantics_path.resolve(),
        )
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("HTTP_PROXY", kwargs["env"])
        self.assertNotIn("HTTPS_PROXY", kwargs["env"])
        self.assertFalse(any(key.startswith("TS_NODE") for key in kwargs["env"]))
        ass_path = Path(job["ass_path"])
        metadata_path = Path(job["dependency_snapshot"]["subtitle"]["metadata"]["path"])
        self.assertRegex(ass_path.name, r"^subtitles\.[0-9a-f]{64}\.ass$")
        self.assertEqual(
            metadata_path.name,
            ass_path.name.removesuffix(".ass") + ".metadata.json",
        )
        self.assertEqual(ass_path.parent, (self.work_dir / "runtime/jobs/task-1").resolve())

    def test_windows_long_work_dir_uses_short_project_runtime_path(self):
        long_work_dir = self.root.joinpath(*(["candidate-" + "x" * 48] * 4))

        with patch("media_catalog.subtitles.os.name", "nt"):
            result = compile_task_ass(
                self.root,
                long_work_dir,
                "task-1",
                self.preset_id,
                self.script_path,
                self.words_path,
                self.semantics_path,
                runner=self.subtitle_runner,
            )

        ass_path = Path(result["ass"]["path"])
        self.assertEqual(
            ass_path.parent,
            (self.root / "runtime" / "subtitles").resolve(),
        )
        command, _ = self.subtitle_calls[-1]
        temporary_output = Path(command[command.index("--output") + 1])
        self.assertLessEqual(len(str(temporary_output)), 240)

    def test_compile_task_ass_reuses_verified_immutable_bytes_and_rejects_tampering(self):
        first = compile_task_ass(
            self.root,
            self.work_dir,
            "task-1",
            self.preset_id,
            self.script_path,
            self.words_path,
            self.semantics_path,
            runner=self.subtitle_runner,
        )
        first_bytes = Path(first["ass"]["path"]).read_bytes()
        reused = compile_task_ass(
            self.root,
            self.work_dir,
            "task-1",
            self.preset_id,
            self.script_path,
            self.words_path,
            self.semantics_path,
            runner=lambda *_args, **_kwargs: self.fail("verified bytes must be reused"),
        )
        self.assertEqual(reused, first)
        self.assertEqual(Path(reused["ass"]["path"]).read_bytes(), first_bytes)

        Path(first["ass"]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_CONTENT_MISMATCH:"):
            compile_task_ass(
                self.root,
                self.work_dir,
                "task-1",
                self.preset_id,
                self.script_path,
                self.words_path,
                self.semantics_path,
                runner=lambda *_args, **_kwargs: self.fail("tampering must fail closed"),
            )

    def test_wrong_or_missing_external_sidecar_is_rejected(self):
        external = self.root / "external.ass"
        external.write_bytes(b"external-ass")
        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_PRESET_MISMATCH:"):
            self.compile_job(ass_path=external)

        sidecar = external.with_suffix(".metadata.json")
        sidecar.write_text(json.dumps({
            "schema": 1,
            "preset": "sub-heavy-orange",
            "coverage": 1.0,
            "cues": [],
            "assSha256": hashlib.sha256(external.read_bytes()).hexdigest(),
            "script": signature(self.script_path),
            "words": signature(self.words_path),
            "semantics": signature(self.semantics_path),
        }), encoding="utf-8")
        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_PRESET_MISMATCH:"):
            self.compile_job(ass_path=external)

    def test_snapshot_binds_subtitle_inputs_outputs_font_and_runtime_sources(self):
        job = self.compile_job(subtitle_sources=self.locked_sources())
        subtitle = job["dependency_snapshot"]["subtitle"]
        self.assertEqual(subtitle["preset_id"], self.preset_id)
        self.assertEqual(subtitle["alignment_coverage"], 1.0)
        for key in ("script", "words", "semantics", "ass", "metadata", "font"):
            self.assertEqual(len(subtitle[key]["sha256"]), 64)
            self.assertTrue(Path(subtitle[key]["path"]).is_file())

        kinds = {entry["kind"] for entry in job["dependency_snapshot"]["files"]}
        self.assertTrue({
            "subtitle:script",
            "subtitle:words",
            "subtitle:semantics",
            "subtitle:ass",
            "subtitle:metadata",
            "subtitle:font",
            "subtitle:make_ass",
            "subtitle:ass_source",
            "subtitle:alignment_source",
            "subtitle:preset_manifest",
        }.issubset(kinds))
        self.assertTrue(verify_catalog_snapshot(
            self.db, "task-1", self.output_path, job["dependency_snapshot"]
        ))

    def test_matching_external_sidecar_is_accepted_and_snapshotted(self):
        generated = compile_task_ass(
            self.root,
            self.work_dir,
            "task-1",
            self.preset_id,
            self.script_path,
            self.words_path,
            self.semantics_path,
            runner=self.subtitle_runner,
        )
        self.subtitle_calls.clear()
        job = self.compile_job(ass_path=Path(generated["ass"]["path"]))

        self.assertEqual(self.subtitle_calls, [])
        self.assertEqual(
            job["dependency_snapshot"]["subtitle"]["preset_id"], self.preset_id
        )
        self.assertEqual(job["ass_path"], generated["ass"]["path"])

    def test_assemble_threads_subtitle_sources_and_separate_runner(self):
        render_calls = []

        def render_runner(command, **kwargs):
            render_calls.append((command, kwargs))
            Path(command[command.index("--output") + 1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        (self.root / "scripts" / "render-mix.ts").write_text("// render", encoding="utf-8")
        expected = {"status": "qa-sentinel"}
        with patch("media_catalog.assemble._finish_render_qa", return_value=expected):
            result = assemble(
                self.db,
                "task-1",
                render=True,
                output_path=self.output_path,
                work_dir=self.work_dir,
                runner=render_runner,
                subtitle_sources=self.locked_sources(),
                subtitle_runner=self.subtitle_runner,
            )

        self.assertEqual(result, expected)
        self.assertEqual(len(self.subtitle_calls), 1)
        self.assertEqual(len(render_calls), 1)

    def test_subtitle_dependency_mutations_fail_snapshot_verification_without_regeneration(self):
        source_keys = ("script", "words", "semantics", "ass", "metadata", "font")
        for key in source_keys:
            with self.subTest(key=key):
                job = self.compile_job(subtitle_sources=self.locked_sources())
                snapshot = job["dependency_snapshot"]
                path = Path(snapshot["subtitle"][key]["path"])
                original = path.read_bytes()
                path.write_bytes(original + b"-changed")
                self.subtitle_calls.clear()
                with patch(
                    "media_catalog.subtitles.compile_task_ass",
                    side_effect=AssertionError(
                        "snapshot verification must not regenerate subtitles"
                    ),
                ):
                    self.assertFalse(verify_catalog_snapshot(
                        self.db, "task-1", self.output_path, snapshot
                    ))
                self.assertEqual(self.subtitle_calls, [])
                path.write_bytes(original)

        job = self.compile_job(subtitle_sources=self.locked_sources())
        snapshot = job["dependency_snapshot"]
        runtime_path = Path(next(
            entry["path"] for entry in snapshot["files"]
            if entry["kind"] == "subtitle:alignment_source"
        ))
        original = runtime_path.read_bytes()
        runtime_path.write_bytes(original + b"-changed")
        self.assertFalse(verify_catalog_snapshot(
            self.db, "task-1", self.output_path, snapshot
        ))
        runtime_path.write_bytes(original)

    def test_final_build_rejects_preset_changed_during_subtitle_generation(self):
        manifest_path = self.manifest_dir / "subtitle-presets.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        replacement = dict(manifest["items"][0])
        replacement["id"] = "sub-race-replacement"
        manifest["items"].append(replacement)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        replacement_plan_id = packaging_plan.clone_plan(
            self.db, self.subtitle_plan_id, plan_name="subtitle race replacement"
        )
        packaging_plan.replace_item(
            self.db, replacement_plan_id, "subtitle", replacement["id"]
        )

        def racing_runner(command, **kwargs):
            result = self.subtitle_runner(command, **kwargs)
            packaging_plan.confirm_plan(self.db, replacement_plan_id)
            return result

        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_PRESET_MISMATCH:"):
            compile_render_mix_job(
                self.db,
                "task-1",
                self.work_dir,
                self.output_path,
                None,
                subtitle_sources=self.locked_sources(),
                subtitle_runner=racing_runner,
            )

        plan_dir = self.work_dir / "runtime" / "jobs" / "task-1"
        self.assertEqual(list(plan_dir.glob("render-mix.*.plan.json")), [])

    def test_generated_metadata_below_alignment_floor_is_rejected(self):
        def low_coverage_runner(command, **kwargs):
            result = self.subtitle_runner(command, **kwargs)
            metadata_path = Path(command[command.index("--metadata") + 1])
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["coverage"] = 0.79
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            return result

        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_PRESET_MISMATCH:"):
            compile_task_ass(
                self.root,
                self.work_dir,
                "task-low-coverage",
                self.preset_id,
                self.script_path,
                self.words_path,
                self.semantics_path,
                runner=low_coverage_runner,
            )

    def test_external_sidecar_below_alignment_floor_is_rejected(self):
        external = self.root / "low-coverage.ass"
        external.write_bytes(b"external-low-coverage")
        external.with_suffix(".metadata.json").write_text(json.dumps({
            "schema": 1,
            "preset": self.preset_id,
            "coverage": 0.79,
            "cues": [],
            "assSha256": hashlib.sha256(external.read_bytes()).hexdigest(),
            "script": signature(self.script_path),
            "words": signature(self.words_path),
            "semantics": signature(self.semantics_path),
        }), encoding="utf-8")

        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_PRESET_MISMATCH:"):
            self.compile_job(ass_path=external)

    def test_external_ass_without_confirmed_subtitle_preset_is_rejected(self):
        plan_id = packaging_plan.clone_plan(
            self.db, self.subtitle_plan_id, plan_name="missing subtitle fixture"
        )
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM packaging_plan_items "
                "WHERE plan_id=? AND slot='subtitle'",
                (plan_id,),
            )
        with self.assertRaisesRegex(RuntimeError, r"^PACKAGING_REQUIRED_SLOT_MISSING:"):
            packaging_plan.confirm_plan(self.db, plan_id)

    def _add_bgm_packaging(self, *, license_name: str, missing_file: bool) -> None:
        pack_dir = self.root / "assets" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        bgm_path = pack_dir / "review-bgm.mp3"
        bgm_path.write_bytes(valid_test_audio())
        (self.manifest_dir / "bgm.json").write_text(json.dumps({
            "items": [{
                "id": "review-bgm",
                "file": "assets/pack/review-bgm.mp3",
                "license": "commercial-approved",
            }]
        }), encoding="utf-8")
        plan_id = packaging_plan.clone_plan(
            self.db, self.subtitle_plan_id, plan_name="bgm packaging fixture"
        )
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO packaging_plan_items(plan_id, slot, asset_ref, asset_file) "
                "VALUES(?, 'bgm', 'review-bgm', 'assets/pack/review-bgm.mp3')",
                (plan_id,),
            )
        packaging_plan.confirm_plan(self.db, plan_id)
        self.subtitle_plan_id = plan_id
        if license_name != "commercial-approved":
            (self.manifest_dir / "bgm.json").write_text(json.dumps({
                "items": [{
                    "id": "review-bgm",
                    "file": "assets/pack/review-bgm.mp3",
                    "license": license_name,
                }]
            }), encoding="utf-8")
        if missing_file:
            bgm_path.unlink()

    def test_packaging_license_blocker_precedes_invalid_subtitle_sources(self):
        self._add_bgm_packaging(license_name="restricted", missing_file=False)
        with self.assertRaisesRegex(RenderMixJobError, r"^PACKAGING_LICENSE_BLOCKED:"):
            self.compile_job(subtitle_sources={
                "script_path": self.root / "missing.script.txt",
                "words_path": self.root / "missing.words.json",
                "semantics_path": self.root / "missing.semantics.json",
            })
        self.assertEqual(self.subtitle_calls, [])

    def test_packaging_file_blocker_precedes_subtitle_tool_execution(self):
        self._add_bgm_packaging(
            license_name="commercial-approved", missing_file=True
        )
        with self.assertRaisesRegex(RenderMixJobError, r"^PACKAGING_FILE_MISSING:"):
            compile_render_mix_job(
                self.db,
                "task-1",
                self.work_dir,
                self.output_path,
                None,
                subtitle_sources=self.locked_sources(),
                subtitle_runner=lambda *_args, **_kwargs: self.fail(
                    "subtitle tool must not run before packaging preflight"
                ),
            )

    def test_semantics_changes_immutable_digest_and_mutation_breaks_snapshot(self):
        first = self.compile_job(subtitle_sources=self.locked_sources())
        first_ass = Path(first["ass_path"])
        first_snapshot = first["dependency_snapshot"]

        self.semantics_path.write_text(
            json.dumps({"keywords": ["算力"], "punchLines": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertFalse(verify_catalog_snapshot(
            self.db, "task-1", self.output_path, first_snapshot
        ))

        second = self.compile_job(subtitle_sources=self.locked_sources())
        self.assertNotEqual(Path(second["ass_path"]), first_ass)

    def test_malformed_semantics_fails_closed_before_subtitle_execution(self):
        self.semantics_path.write_text(
            json.dumps({"keywords": "新疆", "punchLines": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_SOURCE_INVALID:"):
            self.compile_job(subtitle_sources=self.locked_sources())
        self.assertEqual(self.subtitle_calls, [])


if __name__ == "__main__":
    unittest.main()
