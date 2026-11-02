import hashlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog import packaging_plan
from media_catalog.database import CatalogDatabase
from media_catalog.render_mix_adapter import RenderMixJobError, compile_render_mix_job
from media_catalog.scene_cards import set_picture_lock
from media_catalog.tasks import set_status
from media_catalog.v2_candidate import (
    V2CandidateError,
    _resolve_generated_file,
    prepare_v2_candidate,
)


KEYS = (
    "xinjiang-wind-farm",
    "modern-data-center",
    "xinjiang-computing-park",
)
CARD_FIXTURES = (
    ("新疆戈壁风电与绿电能源", "xinjiang-wind-farm"),
    ("昌吉新疆信息产业园", "xinjiang-computing-park"),
    ("能源资源与新疆数据中心", "modern-data-center"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class V2CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_library = self.root / "source-library"
        self.generated_library = self.root / "generated-library"
        self.source_library.mkdir()
        self.generated_library.mkdir()
        self.script = self.root / "xinjiang.script.txt"
        self.words = self.root / "xinjiang.words.json"
        self.semantics = self.root / "xinjiang.semantics.json"
        self.script.write_text("新疆算力", encoding="utf-8")
        self.words.write_text("[]", encoding="utf-8")
        self.semantics.write_text(
            json.dumps({"keywords": ["新疆"], "punchLines": ["新疆算力"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self._build_source_task()
        self.files = {}
        for index, key in enumerate(KEYS, 1):
            path = self.generated_library / f"seed-run-20260824-{index:02d}-{key}.mp4"
            path.write_bytes(f"new-{key}".encode("utf-8"))
            self.files[key] = path
        self.manifest = self.root / "generated.manifest.json"
        self._write_manifest()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _add_source_asset(self, asset_id: str, relative: str, payload: bytes, role: str):
        path = self.source_library / relative
        path.write_bytes(payload)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,full_sha256,created_at) "
                "VALUES(?, 'video', ?, ?, ?, datetime('now'))",
                (asset_id, len(payload), f"quick-{asset_id}", sha256(path)),
            )
            conn.execute(
                "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) "
                "VALUES(?, 'source', ?, ?, 'available')",
                (asset_id, relative, path.stat().st_mtime_ns),
            )
            conn.execute(
                "INSERT INTO task_assets(task_id,asset_id,role) VALUES('p2-xinjiang',?,?)",
                (asset_id, role),
            )

    def _build_source_task(self):
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO libraries(library_id,name,kind,mount_path,online,created_at,updated_at) "
                "VALUES('source','source','local',?,1,datetime('now'),datetime('now'))",
                (str(self.source_library),),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,title,task_type,status,brief_json) "
                "VALUES('p2-xinjiang','source','digital_human','draft',?)",
                (json.dumps({"route": "数字人混剪", "source": "fixture"}, ensure_ascii=False),),
            )
        self._add_source_asset("digital", "digital.mp4", b"digital", "digital_human")
        for index, (narrative, key) in enumerate(CARD_FIXTURES, 1):
            self._add_source_asset(f"old-{index}", f"old-{index}.mp4", f"old-{index}".encode(), "broll")
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO scene_cards(card_id,task_id,idx,narrative_text,narrative_start,narrative_end,"
                    "asset_id,mix_form,confirm_status) VALUES(?,?,?,?,?,?,?,?,1)",
                    (f"p2-xinjiang#{index}", "p2-xinjiang", index, narrative, float(index), float(index + 1),
                     f"old-{index}", "broll全屏"),
                )
        set_status(self.db, "p2-xinjiang", "brief_locked")
        set_picture_lock(self.db, "p2-xinjiang")

        manifest_dir = self.root / "assets" / "manifests"
        manifest_dir.mkdir(parents=True)
        source_manifests = {
            "voice-profiles.json": [{
                "id": "voice-1", "license": "commercial-approved",
                "default_for": ["数字人混剪"],
            }],
            "bgm.json": [{
                "id": "bgm-1", "license": "commercial-approved",
                "default_for": ["数字人混剪"],
            }],
            "subtitle-presets.json": [{
                "id": "sub-heavy-orange", "license": "commercial-approved",
                "default_for": ["数字人混剪"],
            }],
        }
        for name, items in source_manifests.items():
            (manifest_dir / name).write_text(
                json.dumps({"items": items}), encoding="utf-8"
            )
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            source_plan_id = packaging_plan.create_blank_plan(
                self.db, "p2-xinjiang", None, "source plan"
            )
            for slot, ref in (
                ("voice", "voice-1"),
                ("bgm", "bgm-1"),
                ("subtitle", "sub-heavy-orange"),
            ):
                packaging_plan.add_item(self.db, source_plan_id, slot, ref)
            packaging_plan.confirm_plan(self.db, source_plan_id)

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO render_snapshots(snapshot_id,task_id,stage,params_json,producer,input_digest) "
                "VALUES('source-snapshot','p2-xinjiang','mix','{}','fixture','source-input')"
            )

    def _write_manifest(self, *, items=None):
        items = items or [
            {
                "key": key,
                "prompt": f"prompt {key}",
                "model": "seedance-1.0",
                "taskId": f"task-{index}",
                "file": str(self.files[key]),
                "createdAt": "2026-08-24T00:00:00Z",
                "sha256": sha256(self.files[key]),
            }
            for index, key in enumerate(KEYS, 1)
        ]
        self.manifest.write_text(json.dumps({
            "schemaVersion": 1,
            "runId": "run-20260824",
            "items": items,
        }, ensure_ascii=False), encoding="utf-8")

    def _source_state(self):
        result = {}
        query_by_table = {
            "tasks": "SELECT * FROM tasks WHERE task_id='p2-xinjiang'",
            "task_assets": "SELECT * FROM task_assets WHERE task_id='p2-xinjiang'",
            "scene_cards": "SELECT * FROM scene_cards WHERE task_id='p2-xinjiang'",
            "picture_lock": "SELECT * FROM picture_lock WHERE task_id='p2-xinjiang'",
            "packaging_plans": "SELECT * FROM packaging_plans WHERE task_id='p2-xinjiang'",
            "packaging_plan_items": "SELECT i.* FROM packaging_plan_items i JOIN packaging_plans p USING(plan_id) WHERE p.task_id='p2-xinjiang'",
            "task_gate_events": "SELECT * FROM task_gate_events WHERE task_id='p2-xinjiang'",
            "render_snapshots": "SELECT * FROM render_snapshots WHERE task_id='p2-xinjiang'",
        }
        for table, query in query_by_table.items():
            result[table] = [tuple(row) for row in self.db.connection.execute(query).fetchall()]
        return result

    def _prepare(self):
        return prepare_v2_candidate(
            self.db, "p2-xinjiang", "p2-xinjiang-v2", self.manifest, self.generated_library,
            script_path=self.script, words_path=self.words, semantics_path=self.semantics,
        )

    def _write_render_dependencies(self) -> Path:
        manifest_dir = self.root / "assets" / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        pack_dir = self.root / "assets" / "pack"
        pack_dir.mkdir()
        audio = io.BytesIO()
        with wave.open(audio, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(b"\0\0" * 800)
        (pack_dir / "bgm.mp3").write_bytes(audio.getvalue())
        font_dir = self.root / "assets" / "fonts"
        font_dir.mkdir()
        (font_dir / "TestFont.otf").write_bytes(b"font")
        manifests = {
            "voice-profiles.json": [{"id": "voice-1", "license": "commercial-approved"}],
            "bgm.json": [{
                "id": "bgm-1", "file": "assets/pack/bgm.mp3",
                "license": "commercial-approved",
            }],
            "subtitle-presets.json": [{
                "id": "sub-modern-clean-v1", "font": "TestFont.otf",
                "license": "commercial-approved",
            }],
        }
        for name, items in manifests.items():
            (manifest_dir / name).write_text(
                json.dumps({"items": items}), encoding="utf-8"
            )
        for relative in (
            "scripts/make-ass.ts",
            "src/subtitle-ass.ts",
            "src/subtitle-alignment.ts",
            "node_modules/ts-node/dist/bin.js",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture", encoding="utf-8")

        ass_path = self.root / "candidate.ass"
        ass_path.write_text("[Script Info]\n", encoding="utf-8")

        def signature(path: Path) -> dict:
            stat = path.stat()
            return {
                "path": str(path.resolve()),
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": sha256(path),
            }

        ass_path.with_suffix(".metadata.json").write_text(
            json.dumps({
                "schema": 1,
                "preset": "sub-modern-clean-v1",
                "coverage": 1.0,
                "cues": [],
                "assSha256": sha256(ass_path),
                "script": signature(self.script),
                "words": signature(self.words),
                "semantics": signature(self.semantics),
            }),
            encoding="utf-8",
        )
        return ass_path

    def test_successfully_creates_isolated_candidate_with_only_new_broll(self):
        before = self._source_state()
        result = self._prepare()

        self.assertEqual(result["source_task_id"], "p2-xinjiang")
        self.assertEqual(result["target_task_id"], "p2-xinjiang-v2")
        self.assertEqual(result["run_id"], "run-20260824")
        self.assertEqual(result["subtitle_preset"], "sub-modern-clean-v1")
        self.assertEqual(result["status"], "packaging_confirmed")
        self.assertTrue(result["brief_locked"])
        self.assertTrue(result["picture_locked"])
        self.assertEqual([item["key"] for item in result["assets"]], list(KEYS))
        self.assertEqual([item["sha256"] for item in result["assets"]], [sha256(self.files[key]) for key in KEYS])
        target_brief = json.loads(self.db.connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id='p2-xinjiang-v2'"
        ).fetchone()[0])
        self.assertEqual(target_brief["subtitle_sources"], {
            "script_path": str(self.script.resolve()),
            "words_path": str(self.words.resolve()),
            "semantics_path": str(self.semantics.resolve()),
        })

        target_assets = self.db.connection.execute(
            "SELECT asset_id,role FROM task_assets WHERE task_id='p2-xinjiang-v2' ORDER BY role,asset_id"
        ).fetchall()
        self.assertEqual(sum(row["role"] == "digital_human" for row in target_assets), 1)
        self.assertEqual(sum(row["role"] == "broll" for row in target_assets), 3)
        self.assertFalse(any(row["asset_id"].startswith("old-") for row in target_assets))
        cards = self.db.connection.execute(
            "SELECT idx,narrative_text,narrative_start,narrative_end,asset_id,confirm_status "
            "FROM scene_cards WHERE task_id='p2-xinjiang-v2' ORDER BY idx"
        ).fetchall()
        self.assertEqual([(row["idx"], row["narrative_text"], row["narrative_start"], row["narrative_end"]) for row in cards],
                         [(1, CARD_FIXTURES[0][0], 1.0, 2.0), (2, CARD_FIXTURES[1][0], 2.0, 3.0),
                          (3, CARD_FIXTURES[2][0], 3.0, 4.0)])
        by_key = {item["key"]: item["asset_id"] for item in result["assets"]}
        self.assertEqual([row["asset_id"] for row in cards], [
            by_key["xinjiang-wind-farm"],
            by_key["xinjiang-computing-park"],
            by_key["modern-data-center"],
        ])
        self.assertTrue(all(row["confirm_status"] for row in cards))
        plan = self.db.connection.execute(
            "SELECT plan_id,status FROM packaging_plans WHERE task_id='p2-xinjiang-v2'"
        ).fetchone()
        items = self.db.connection.execute(
            "SELECT slot,asset_ref,is_backup FROM packaging_plan_items WHERE plan_id=? ORDER BY slot,rank",
            (plan["plan_id"],),
        ).fetchall()
        subtitles = [row for row in items if row["slot"] == "subtitle"]
        self.assertEqual(plan["status"], "confirmed")
        self.assertEqual([(row["asset_ref"], row["is_backup"]) for row in subtitles], [("sub-modern-clean-v1", 0)])
        self.assertEqual(before, self._source_state())

    def test_prepared_v2_candidate_compiles_without_packaging_gate_digest_mismatch(self):
        result = self._prepare()
        self._write_render_dependencies()
        (self.root / "out").mkdir()
        target_brief = json.loads(self.db.connection.execute(
            "SELECT brief_json FROM tasks WHERE task_id=?", (result["target_task_id"],)
        ).fetchone()[0])
        subtitle_calls = []

        def subtitle_runner(command, **_kwargs):
            subtitle_calls.append(command)
            output = Path(command[command.index("--output") + 1])
            metadata = Path(command[command.index("--metadata") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("[Script Info]\n", encoding="utf-8")
            metadata.write_text(json.dumps({
                "schema": 1,
                "preset": "sub-modern-clean-v1",
                "coverage": 1.0,
                "cues": [],
                "assSha256": sha256(output),
            }), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with (
            patch.object(packaging_plan, "MANIFEST_DIR", self.root / "assets" / "manifests"),
            patch("media_catalog.render_mix_adapter.PROJECT_ROOT", self.root),
            patch(
                "media_catalog.render_mix_adapter._runtime_file_dependencies",
                return_value=([], {"PATH": "controlled", "LANG": "C.UTF-8"}),
            ),
        ):
            job = compile_render_mix_job(
                self.db,
                result["target_task_id"],
                self.root / "work",
                self.root / "out" / "candidate.mp4",
                None,
                subtitle_sources=target_brief["subtitle_sources"],
                subtitle_runner=subtitle_runner,
            )

        self.assertEqual(job["task_id"], result["target_task_id"])
        self.assertTrue(Path(job["plan_path"]).is_file())
        self.assertEqual(len(subtitle_calls), 1)
        self.assertEqual(
            Path(subtitle_calls[0][subtitle_calls[0].index("--semantics") + 1]),
            self.semantics.resolve(),
        )

    def test_prepared_v2_candidate_accepts_matching_external_ass_binding(self):
        result = self._prepare()
        ass_path = self._write_render_dependencies()
        (self.root / "out").mkdir()

        with (
            patch.object(packaging_plan, "MANIFEST_DIR", self.root / "assets" / "manifests"),
            patch("media_catalog.render_mix_adapter.PROJECT_ROOT", self.root),
            patch(
                "media_catalog.render_mix_adapter._runtime_file_dependencies",
                return_value=([], {"PATH": "controlled", "LANG": "C.UTF-8"}),
            ),
        ):
            job = compile_render_mix_job(
                self.db,
                result["target_task_id"],
                self.root / "work",
                self.root / "out" / "candidate.mp4",
                ass_path,
            )

        self.assertTrue(Path(job["plan_path"]).is_file())
        self.assertEqual(Path(job["ass_path"]), ass_path.resolve())

    def test_prepared_v2_candidate_rejects_mismatched_external_binding_before_plan_write(self):
        result = self._prepare()
        ass_path = self._write_render_dependencies()
        (self.root / "out").mkdir()
        other_semantics = self.root / "other.semantics.json"
        other_semantics.write_text(self.semantics.read_text(encoding="utf-8"), encoding="utf-8")
        metadata_path = ass_path.with_suffix(".metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        stat = other_semantics.stat()
        metadata["semantics"] = {
            "path": str(other_semantics.resolve()),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256(other_semantics),
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with (
            patch.object(packaging_plan, "MANIFEST_DIR", self.root / "assets" / "manifests"),
            patch("media_catalog.render_mix_adapter.PROJECT_ROOT", self.root),
            patch(
                "media_catalog.render_mix_adapter._runtime_file_dependencies",
                return_value=([], {"PATH": "controlled", "LANG": "C.UTF-8"}),
            ),
        ):
            with self.assertRaisesRegex(RenderMixJobError, "ASS_SOURCE_MISMATCH"):
                compile_render_mix_job(
                    self.db,
                    result["target_task_id"],
                    self.root / "work",
                    self.root / "out" / "candidate.mp4",
                    ass_path,
                )

        self.assertEqual(list((self.root / "work").rglob("render-mix.*.plan.json")), [])

    def test_v2_rejects_malformed_semantics_before_target_writes(self):
        self.semantics.write_text(
            json.dumps({"keywords": "新疆", "punchLines": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(V2CandidateError, "SUBTITLE_SEMANTICS_INVALID"):
            self._prepare()
        self.assertIsNone(self.db.connection.execute(
            "SELECT 1 FROM tasks WHERE task_id='p2-xinjiang-v2'"
        ).fetchone())

    def test_v2_rejects_mismatched_locked_subtitle_sources_before_execution(self):
        result = self._prepare()
        self._write_render_dependencies()
        (self.root / "out").mkdir()
        other_semantics = self.root / "other.semantics.json"
        other_semantics.write_text(self.semantics.read_text(encoding="utf-8"), encoding="utf-8")

        with (
            patch.object(packaging_plan, "MANIFEST_DIR", self.root / "assets" / "manifests"),
            patch("media_catalog.render_mix_adapter.PROJECT_ROOT", self.root),
            patch(
                "media_catalog.render_mix_adapter._runtime_file_dependencies",
                return_value=([], {"PATH": "controlled", "LANG": "C.UTF-8"}),
            ),
        ):
            with self.assertRaisesRegex(RenderMixJobError, "ASS_SOURCE_MISMATCH"):
                compile_render_mix_job(
                    self.db,
                    result["target_task_id"],
                    self.root / "work",
                    self.root / "out" / "candidate.mp4",
                    None,
                    subtitle_sources={
                        "script_path": self.script,
                        "words_path": self.words,
                        "semantics_path": other_semantics,
                    },
                    subtitle_runner=lambda *_args, **_kwargs: self.fail(
                        "mismatched sources must be rejected before subtitle execution"
                    ),
                )

    def test_relative_paths_resolve_from_generated_library_and_escapes_fail_closed(self):
        relative = json.loads(self.manifest.read_text(encoding="utf-8"))
        for item in relative["items"]:
            item["file"] = Path(item["file"]).name
        self._write_manifest(items=relative["items"])
        result = self._prepare()
        self.assertEqual(result["target_task_id"], "p2-xinjiang-v2")

    def test_resolver_rejects_parent_and_resolved_file_or_directory_escapes(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        outside_dir = self.root / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "clip.mp4").write_bytes(b"outside-dir")

        def resolver(path: Path) -> Path:
            if path.name == "file-link.mp4":
                return outside
            if "directory-link" in path.parts:
                return outside_dir / "clip.mp4"
            return path.resolve()

        with self.assertRaisesRegex(V2CandidateError, "GENERATED_FILE_OUTSIDE_LIBRARY"):
            _resolve_generated_file("../outside.mp4", self.generated_library, resolver=resolver)
        with self.assertRaisesRegex(V2CandidateError, "GENERATED_FILE_OUTSIDE_LIBRARY"):
            _resolve_generated_file("file-link.mp4", self.generated_library, resolver=resolver)
        with self.assertRaisesRegex(V2CandidateError, "GENERATED_FILE_OUTSIDE_LIBRARY"):
            _resolve_generated_file("directory-link/clip.mp4", self.generated_library, resolver=resolver)
    def test_ambiguous_or_unclassified_source_windows_fail_closed(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE scene_cards SET narrative_text='普通介绍' WHERE card_id='p2-xinjiang#3'")
        with self.assertRaisesRegex(V2CandidateError, "SOURCE_SCENE_CARD_UNCLASSIFIED"):
            self._prepare()
    def test_ambiguous_source_window_mapping_fails_closed(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE scene_cards SET narrative_text='昌吉信息产业园数据中心' WHERE card_id='p2-xinjiang#3'")
        with self.assertRaisesRegex(V2CandidateError, "SOURCE_SCENE_CARD_AMBIGUOUS"):
            self._prepare()
    def test_duplicate_source_window_mapping_fails_closed(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE scene_cards SET narrative_text='昌吉信息产业园' WHERE card_id='p2-xinjiang#3'")
        with self.assertRaisesRegex(V2CandidateError, "SOURCE_SCENE_CARD_MAPPING_INVALID"):
            self._prepare()
    def test_manifest_validation_fails_before_any_target_write(self):
        bad = json.loads(self.manifest.read_text(encoding="utf-8"))
        bad["items"] = bad["items"][:2]
        self.manifest.write_text(json.dumps(bad), encoding="utf-8")
        before = self._source_state()

        with self.assertRaisesRegex(V2CandidateError, "MANIFEST_ITEM_COUNT"):
            self._prepare()

        self.assertIsNone(self.db.connection.execute(
            "SELECT 1 FROM tasks WHERE task_id='p2-xinjiang-v2'"
        ).fetchone())
        self.assertEqual(before, self._source_state())

    def test_rejects_missing_outside_mismatched_duplicate_and_old_hashes(self):
        cases = []
        missing = json.loads(self.manifest.read_text(encoding="utf-8"))
        missing["items"][0].pop("taskId")
        cases.append((missing, "MANIFEST_PROVENANCE_MISSING"))
        outside = json.loads(self.manifest.read_text(encoding="utf-8"))
        outside["items"][0]["file"] = str(self.root / "outside.mp4")
        cases.append((outside, "GENERATED_FILE_OUTSIDE_LIBRARY"))
        mismatch = json.loads(self.manifest.read_text(encoding="utf-8"))
        mismatch["items"][0]["sha256"] = "0" * 64
        cases.append((mismatch, "GENERATED_HASH_MISMATCH"))
        duplicate = json.loads(self.manifest.read_text(encoding="utf-8"))
        duplicate["items"][1]["sha256"] = duplicate["items"][0]["sha256"]
        cases.append((duplicate, "GENERATED_HASH_DUPLICATE"))
        old = json.loads(self.manifest.read_text(encoding="utf-8"))
        cases.append((old, "OLD_BROLL_HASH_REUSED"))

        for payload, code in cases:
            with self.subTest(code=code):
                for key in KEYS:
                    self.files[key].write_bytes(f"new-{key}".encode("utf-8"))
                if code == "GENERATED_HASH_DUPLICATE":
                    self.files[KEYS[1]].write_bytes(self.files[KEYS[0]].read_bytes())
                    payload["items"][1]["sha256"] = sha256(self.files[KEYS[1]])
                if code == "OLD_BROLL_HASH_REUSED":
                    self.files[KEYS[0]].write_bytes((self.source_library / "old-1.mp4").read_bytes())
                    payload["items"][0]["sha256"] = sha256(self.files[KEYS[0]])
                self._write_manifest(items=payload["items"])
                before = self._source_state()
                with self.assertRaisesRegex(V2CandidateError, code):
                    self._prepare()
                self.assertIsNone(self.db.connection.execute(
                    "SELECT 1 FROM tasks WHERE task_id='p2-xinjiang-v2'"
                ).fetchone())
                self.assertEqual(before, self._source_state())
                self._write_manifest()

    def test_existing_target_rejected_without_mutating_source(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO tasks(task_id,title,status) VALUES('p2-xinjiang-v2','existing','draft')")
        before = self._source_state()

        with self.assertRaisesRegex(V2CandidateError, "TARGET_TASK_EXISTS"):
            self._prepare()

        self.assertEqual(before, self._source_state())
        self.assertEqual(self.db.connection.execute(
            "SELECT title FROM tasks WHERE task_id='p2-xinjiang-v2'"
        ).fetchone()[0], "existing")

    def test_midway_failure_rolls_back_target_task_and_bindings(self):
        before = self._source_state()
        with patch("media_catalog.v2_candidate._confirm_target_packaging", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self._prepare()

        self.assertIsNone(self.db.connection.execute(
            "SELECT 1 FROM tasks WHERE task_id='p2-xinjiang-v2'"
        ).fetchone())
        self.assertEqual(before, self._source_state())


if __name__ == "__main__":
    unittest.main()
