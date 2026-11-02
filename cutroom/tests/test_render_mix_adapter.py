import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog import packaging_plan
from media_catalog.tasks import create_task, set_status
from media_catalog.scene_cards import set_picture_lock
from media_catalog.subtitles import bind_external_ass
from media_catalog.packaging_plan import confirm_plan
from media_catalog.packaging_layers import (
    PackagingTimingContext,
    compile_packaging_layers,
)
from media_catalog.render_mix_adapter import (
    RenderMixJobError,
    build_packaging_preview_mix_job,
    build_render_mix_job,
    compile_render_mix_job,
    run_render_mix,
    write_render_mix_plan,
    _controlled_render_env,
    _default_atr_path,
    _default_process_runner,
)


def valid_test_audio() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\0\0" * 800)
    return buffer.getvalue()


class RenderMixCatalogFixture:
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.digital_human = self.media / "digital.mp4"
        self.broll = self.media / "broll.mp4"
        self.digital_human.write_bytes(b"digital-human")
        self.broll.write_bytes(b"broll")
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.project_root_patch = patch(
            "media_catalog.render_mix_adapter.PROJECT_ROOT", self.root
        )
        self.project_root_patch.start()
        self.runtime_deps_patch = patch(
            "media_catalog.render_mix_adapter._runtime_file_dependencies",
            return_value=([], {"PATH": "controlled", "LANG": "C.UTF-8"}),
        )
        self.runtime_deps_patch.start()
        self._seed_subtitle_fixture()
        self.packaging_manifest_patch = patch.object(
            packaging_plan, "MANIFEST_DIR", self.manifest_dir
        )
        self.packaging_manifest_patch.start()
        self._seed_ready_task()
        set_status(self.db, "task-1", "brief_locked")
        set_picture_lock(self.db, "task-1")
        confirm_plan(self.db, "plan-1")

    def tearDown(self):
        self.packaging_manifest_patch.stop()
        self.runtime_deps_patch.stop()
        self.project_root_patch.stop()
        self.db.close()
        self.temp_dir.cleanup()

    def _seed_subtitle_fixture(self):
        self.manifest_dir = self.root / "assets" / "manifests"
        self.manifest_dir.mkdir(parents=True)
        font_path = self.root / "assets" / "fonts" / "test.otf"
        font_path.parent.mkdir(parents=True)
        font_path.write_bytes(b"test-font")
        for relative in (
            "scripts/make-ass.ts",
            "src/subtitle-ass.ts",
            "src/subtitle-alignment.ts",
        ):
            runtime_path = self.root / relative
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_path.write_text(f"// {relative}", encoding="utf-8")
        (self.manifest_dir / "subtitle-presets.json").write_text(json.dumps({
            "items": [{
                "id": "subtitle-test",
                "font": font_path.name,
                "fontname_guess": "Test",
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
        }), encoding="utf-8")
        sources = []
        for name, content in (
            ("script", "fixture script"),
            ("words", "[]"),
            ("semantics", '{"keywords":[],"punchLines":[]}'),
        ):
            source = self.root / f"fixture.{name}.json"
            source.write_text(content, encoding="utf-8")
            stat = source.stat()
            sources.append((name, {
                "path": str(source.resolve()),
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }))
        self.bound_ass_path = self.root / "fixture.ass"
        self.bound_ass_path.write_bytes(b"[Events]\n")
        self.bound_ass_path.with_suffix(".metadata.json").write_text(json.dumps({
            "schema": 1,
            "preset": "subtitle-test",
            "coverage": 1.0,
            "cues": [],
            "assSha256": hashlib.sha256(self.bound_ass_path.read_bytes()).hexdigest(),
            **dict(sources),
        }), encoding="utf-8")

    def _seed_ready_task(self):
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO libraries(
                  library_id, name, kind, mount_path, online, created_at, updated_at
                ) VALUES('lib', 'library', 'local', ?, 1, datetime('now'), datetime('now'))
                """,
                (str(self.media),),
            )
            for asset_id, relative_path in (
                ("asset-digital", "digital.mp4"),
                ("asset-broll", "broll.mp4"),
            ):
                connection.execute(
                    """
                    INSERT INTO assets(
                      asset_id, media_kind, size_bytes, quick_fingerprint, created_at
                    ) VALUES(?, 'video', 10, ?, datetime('now'))
                    """,
                    (asset_id, f"fingerprint-{asset_id}"),
                )
                connection.execute(
                    """
                    INSERT INTO asset_locations(
                      asset_id, library_id, relative_path, mtime_ns, state
                    ) VALUES(?, 'lib', ?, 1, 'available')
                    """,
                    (asset_id, relative_path),
                )
            connection.execute(
                """
                INSERT INTO tasks(task_id, title, task_type, status)
                VALUES('task-1', 'task', '数字人混剪', 'packaging_confirmed')
                """
            )
            connection.execute(
                "INSERT INTO task_assets(task_id, asset_id, role) VALUES('task-1', 'asset-digital', 'digital_human')"
            )
            connection.execute(
                "INSERT INTO task_assets(task_id, asset_id, role) VALUES('task-1', 'asset-broll', 'broll')"
            )
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id, task_id, idx, narrative_start, narrative_end, asset_id,
                  mix_form, enter_tc, exit_tc, audio_policy_draft, confirm_status
                ) VALUES(
                  'task-1#0', 'task-1', 0, 0.0, 6.0, 'asset-digital',
                  '数字人全屏', 0.0, 0.0, 'keep', 1
                )
                """
            )
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id, task_id, idx, narrative_start, narrative_end, asset_id,
                  mix_form, enter_tc, exit_tc, audio_policy_draft, confirm_status
                ) VALUES(
                  'task-1#1', 'task-1', 1, 6.0, 9.0, 'asset-broll',
                  'broll全屏', 0.0, 0.0, 'mute', 1
                )
                """
            )
            connection.execute(
                "INSERT INTO picture_lock(task_id, lock_version, changed_since_lock) VALUES('task-1', 'v1', 0)"
            )
            connection.execute(
                """
                INSERT INTO packaging_plans(plan_id, task_id, route, plan_name, status)
                VALUES('plan-1', 'task-1', '数字人混剪', 'plan', 'draft')
                """
            )
            connection.execute(
                "INSERT INTO packaging_plan_items(plan_id,slot,asset_ref) "
                "VALUES('plan-1','subtitle','subtitle-test')"
            )

    def compile(self):
        return compile_render_mix_job(
            self.db,
            "task-1",
            self.root / "work",
            self.root / "out" / "task-1.mp4",
            self.bound_ass_path,
        )

    def make_manifests(self, *, license_name="commercial-approved", missing_bgm=False):
        manifest_dir = self.root / "assets" / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        pack_dir = self.root / "assets" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "bgm": pack_dir / "bgm.mp3",
            "sfx": pack_dir / "sfx.wav",
            "brand": pack_dir / "brand.png",
            "sticker": pack_dir / "sticker.png",
        }
        for key, file_path in files.items():
            if not (key == "bgm" and missing_bgm):
                file_path.write_bytes(
                    valid_test_audio() if key in {"bgm", "sfx"} else key.encode("utf-8")
                )
        manifests = {
            "voice-profiles.json": [{
                "id": "voice-test",
                "license": "commercial-approved",
            }],
            "bgm.json": [{
                "id": "bgm-test",
                "file": "assets/pack/bgm.mp3",
                "license": license_name,
                "default_gain": 0.17,
            }],
            "sfx.json": [{
                "id": "sfx-test",
                "file": "assets/pack/sfx.wav",
                "license": "commercial-approved",
                "default_gain": 0.35,
            }],
            "visual-cards.json": [
                {
                    "id": "brand-test",
                    "file": "assets/pack/brand.png",
                    "license": "commercial-approved",
                },
                {
                    "id": "card-data-emphasis",
                    "kind": "title_card",
                    "font": "test.otf",
                    "fontname_guess": "Test",
                    "background_hex": "#171717",
                    "accent_hex": "#FD9437",
                    "license": "commercial-approved",
                },
            ],
            "subtitle-presets.json": [{
                "id": "subtitle-test",
                "font": "test.otf",
                "fontname_guess": "Test",
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
            }, {
                "id": "subtitle-alt",
                "font": "test.otf",
                "fontname_guess": "Test",
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
            }],
            "stickers.json": [{
                "id": "sticker-test",
                "file": "assets/pack/sticker.png",
                "license": "commercial-approved",
            }],
            "fancy-text-presets.json": [{
                "id": "fancy-test",
                "license": "commercial-approved",
            }],
        }
        for filename, items in manifests.items():
            (manifest_dir / filename).write_text(
                json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
            )
        return manifest_dir, files

    def add_packaging_items(self, manifest_dir, *, include_subtitle=True):
        plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="packaging fixture"
        )
        with self.db.transaction() as connection:
            for slot, asset_ref, asset_file in (
                ("bgm", "bgm-test", "assets/pack/bgm.mp3"),
                ("brand", "brand-test", "assets/pack/brand.png"),
                *((("subtitle", "subtitle-test", None),) if include_subtitle else ()),
                ("sticker", "sticker-test", "assets/pack/sticker.png"),
                ("fancy_text", "fancy-test", None),
            ):
                connection.execute(
                    "DELETE FROM packaging_plan_items WHERE plan_id=? AND slot=?",
                    (plan_id, slot),
                )
                connection.execute(
                    """
                    INSERT INTO packaging_plan_items(plan_id, slot, asset_ref, asset_file)
                    VALUES(?, ?, ?, ?)
                    """,
                    (plan_id, slot, asset_ref, asset_file),
                )
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            confirm_plan(self.db, plan_id)
        return plan_id


class PackagingLayerTests(RenderMixCatalogFixture, unittest.TestCase):
    def _all_slot_plan(self):
        manifest_dir, files = self.make_manifests()
        plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="all packaging slots"
        )
        selections = (
            ("voice", "voice-test", None),
            ("bgm", "bgm-test", "assets/pack/bgm.mp3"),
            ("sfx", "sfx-test", "assets/pack/sfx.wav"),
            ("subtitle", "subtitle-test", None),
            ("title_card", "card-data-emphasis", None),
            ("sticker", "sticker-test", "assets/pack/sticker.png"),
            ("fancy_text", "fancy-test", None),
            ("brand", "brand-test", "assets/pack/brand.png"),
        )
        for slot, asset_ref, asset_file in selections:
            packaging_plan.replace_item(
                self.db, plan_id, slot, asset_ref, asset_file
            )
        return manifest_dir, files, plan_id

    def test_compiler_emits_real_layers_and_explicit_coverage(self):
        manifest_dir, files, plan_id = self._all_slot_plan()
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            compiled = compile_packaging_layers(
                self.db,
                plan_id,
                PackagingTimingContext(
                    10.0, "测试标题", "核心优势", "original"
                ),
            )

        self.assertEqual(compiled.plan_fields["titles"][0]["text"], "测试标题")
        self.assertEqual(
            compiled.plan_fields["titles"][0]["presetId"],
            "card-data-emphasis",
        )
        self.assertEqual(
            compiled.plan_fields["stickers"][0]["presetId"], "sticker-test"
        )
        self.assertEqual(
            compiled.plan_fields["fancyTexts"][0]["text"], "核心优势"
        )
        self.assertEqual(
            compiled.plan_fields["sfx"][0]["src"], str(files["sfx"].resolve())
        )
        self.assertEqual(
            {item["slot"] for item in compiled.coverage},
            {
                "voice",
                "bgm",
                "sfx",
                "subtitle",
                "title_card",
                "sticker",
                "fancy_text",
                "brand",
            },
        )

    def test_no_optional_slots_produces_subtitle_only_coverage(self):
        with patch.object(packaging_plan, "MANIFEST_DIR", self.manifest_dir):
            compiled = compile_packaging_layers(
                self.db,
                "plan-1",
                PackagingTimingContext(6.0, "标题", "重点", "original"),
            )
        self.assertEqual(
            compiled.coverage,
            [{"slot": "subtitle", "status": "rendered", "reasonCode": None}],
        )

    def test_original_voice_is_not_applicable_with_reason(self):
        manifest_dir, _, plan_id = self._all_slot_plan()
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            compiled = compile_packaging_layers(
                self.db,
                plan_id,
                PackagingTimingContext(10.0, "标题", "重点", "original"),
            )
        voice = next(item for item in compiled.coverage if item["slot"] == "voice")
        self.assertEqual(voice["status"], "not_applicable")
        self.assertEqual(voice["reasonCode"], "ORIGINAL_VOICE_IN_USE")


class PackagingPreviewMixJobTests(RenderMixCatalogFixture, unittest.TestCase):
    def test_read_only_plan_validation_preserves_callers_transaction(self):
        self.db.connection.execute("BEGIN")
        try:
            packaging_plan.validate_plan_for_render(
                self.db, "plan-1", require_picture_lock=False
            )
            self.assertTrue(self.db.connection.in_transaction)
        finally:
            if self.db.connection.in_transaction:
                self.db.connection.rollback()

    def test_preview_job_uses_draft_plan_without_packaging_gate(self):
        draft_plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="preview draft"
        )
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM task_gate_events "
                "WHERE task_id='task-1' AND gate_type='packaging'"
            )
        output = (
            self.root
            / "runtime/workbench/cache/packaging-previews/task-1/preview.mp4"
        )

        with patch.object(packaging_plan, "MANIFEST_DIR", self.manifest_dir):
            job = build_packaging_preview_mix_job(
                self.db,
                "task-1",
                draft_plan_id,
                self.root / "work",
                output,
                10.0,
                subtitle_sources=None,
            )

        self.assertEqual(job["packaging"]["plan_id"], draft_plan_id)
        self.assertEqual(job["plan"]["output"]["maxDuration"], 10.0)
        self.assertEqual(job["plan"]["output"]["width"], 540)
        self.assertEqual(job["plan"]["output"]["height"], 960)
        self.assertIsNone(
            self.db.connection.execute(
                "SELECT 1 FROM task_gate_events "
                "WHERE task_id='task-1' AND gate_type='packaging'"
            ).fetchone()
        )

    def test_preview_job_binds_subtitles_from_explicit_sources(self):
        draft_plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="preview subtitle draft"
        )
        binding = bind_external_ass(
            self.root, "subtitle-test", self.bound_ass_path
        )
        sources = {
            "script_path": str(self.root / "fixture.script.json"),
            "words_path": str(self.root / "fixture.words.json"),
            "semantics_path": str(self.root / "fixture.semantics.json"),
        }
        output = (
            self.root
            / "runtime/workbench/cache/packaging-previews/task-1/subtitle.mp4"
        )
        with (
            patch.object(packaging_plan, "MANIFEST_DIR", self.manifest_dir),
            patch(
                "media_catalog.render_mix_adapter.compile_task_ass",
                return_value=binding,
            ) as compile_ass,
        ):
            job = build_packaging_preview_mix_job(
                self.db,
                "task-1",
                draft_plan_id,
                self.root / "work",
                output,
                8.0,
                subtitle_sources=sources,
            )

        self.assertEqual(job["ass_path"], str(self.bound_ass_path.resolve()))
        self.assertEqual(job["dependency_snapshot"]["subtitle"]["preset_id"], "subtitle-test")
        compile_ass.assert_called_once()


class RenderMixGateTests(RenderMixCatalogFixture, unittest.TestCase):
    def assert_blocked(self, code: str):
        with self.assertRaisesRegex(RenderMixJobError, rf"^{code}:"):
            self.compile()

    def test_zero_task_assets_is_blocked(self):
        self.db.connection.execute("DELETE FROM task_assets WHERE task_id='task-1'")
        self.db.connection.commit()
        self.assert_blocked("NO_TASK_ASSETS")

    def test_zero_scene_cards_is_blocked(self):
        self.db.connection.execute("DELETE FROM scene_cards WHERE task_id='task-1'")
        self.db.connection.commit()
        self.assert_blocked("NO_SCENE_CARDS")

    def test_unconfirmed_scene_card_is_blocked(self):
        self.db.connection.execute("UPDATE scene_cards SET confirm_status=0 WHERE task_id='task-1'")
        self.db.connection.commit()
        self.assert_blocked("SCENE_CARD_UNCONFIRMED")

    def test_missing_picture_lock_is_blocked(self):
        self.db.connection.execute("DELETE FROM picture_lock WHERE task_id='task-1'")
        self.db.connection.commit()
        self.assert_blocked("PICTURE_LOCK_MISSING")

    def test_changed_picture_lock_is_blocked(self):
        self.db.connection.execute("UPDATE picture_lock SET changed_since_lock=1 WHERE task_id='task-1'")
        self.db.connection.commit()
        self.assert_blocked("PICTURE_LOCK_CHANGED")

    def test_gate_bound_packaging_plan_must_still_exist(self):
        self.db.connection.execute(
            "DELETE FROM packaging_plans WHERE task_id='task-1'"
        )
        self.db.connection.commit()
        self.assert_blocked("PACKAGING_NOT_CONFIRMED")

    def test_missing_digital_human_role_is_blocked(self):
        self.db.connection.execute(
            "UPDATE task_assets SET role='other' WHERE asset_id='asset-digital'"
        )
        self.db.connection.commit()
        self.assert_blocked("DIGITAL_HUMAN_MISSING")

    def test_brief_change_through_normal_task_write_invalidates_locked_gate(self):
        create_task(self.db, "task-1", "task", task_type="数字人混剪",
                    brief={"text": "changed after lock"})
        self.assert_blocked("BRIEF_GATE_DIGEST_MISMATCH")


class RenderMixCompilationTests(RenderMixCatalogFixture, unittest.TestCase):
    def assert_blocked(self, code: str):
        with self.assertRaisesRegex(RenderMixJobError, rf"^{code}:"):
            self.compile()

    def test_compiles_catalog_assets_and_timed_broll_into_isolated_mix_plan(self):
        manifest_dir, packaging_files = self.make_manifests()
        active_plan_id = self.add_packaging_items(manifest_dir)

        alternate = self.root / "alternate"
        alternate.mkdir()
        alternate_broll = alternate / "broll.mp4"
        alternate_broll.write_bytes(b"alternate-broll")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO libraries(
                  library_id, name, kind, mount_path, online, created_at, updated_at
                ) VALUES('aaa', 'preferred', 'local', ?, 1, datetime('now'), datetime('now'))
                """,
                (str(alternate),),
            )
            connection.execute(
                """
                INSERT INTO asset_locations(
                  asset_id, library_id, relative_path, mtime_ns, state
                ) VALUES('asset-broll', 'aaa', 'broll.mp4', 2, 'available')
                """
            )
        set_picture_lock(self.db, "task-1")
        confirm_plan(self.db, active_plan_id)

        legacy_plan = self.root / "work" / "public" / "scripts" / "task-1.plan.json"
        legacy_plan.parent.mkdir(parents=True)
        legacy_plan.write_text("do-not-touch", encoding="utf-8")
        changes_before = self.db.connection.total_changes

        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            job = self.compile()

        self.assertEqual(self.db.connection.total_changes, changes_before)
        self.assertEqual(legacy_plan.read_text(encoding="utf-8"), "do-not-touch")
        self.assertEqual(Path(job["plan_path"]).parent,
                         (self.root / "work/runtime/jobs/task-1").resolve())
        self.assertIn(job["plan_digest"], Path(job["plan_path"]).name)
        self.assertEqual(job["input_path"], str(self.digital_human.resolve()))
        self.assertEqual(len(job["plan"]["inserts"]), 1)
        self.assertEqual(job["plan"]["inserts"][0]["src"], str(alternate_broll.resolve()))
        self.assertEqual(job["plan"]["inserts"][0]["start"], 6.0)
        self.assertEqual(job["plan"]["inserts"][0]["end"], 9.0)
        self.assertEqual(job["plan"]["inserts"][0]["sourceStart"], 0.0)
        self.assertEqual(job["plan"]["inserts"][0]["sourceEnd"], 3.0)
        self.assertEqual(job["plan"]["inserts"][0]["audio"], "mute")
        self.assertEqual(job["plan"]["bgm"]["src"], str(packaging_files["bgm"].resolve()))
        self.assertEqual(job["plan"]["bgm"]["gain"], 0.17)
        self.assertTrue(job["plan"]["bgm"]["duck"])
        self.assertEqual(job["plan"]["watermark"]["src"], str(packaging_files["brand"].resolve()))
        self.assertEqual(job["plan"]["stickers"][0]["presetId"], "sticker-test")
        self.assertEqual(job["plan"]["fancyTexts"][0]["presetId"], "fancy-test")
        self.assertEqual(
            {item["slot"] for item in job["packaging"]["items"]},
            {"bgm", "brand", "subtitle", "sticker", "fancy_text"},
        )
        self.assertEqual(
            json.loads(Path(job["plan_path"]).read_text(encoding="utf-8")),
            job["plan"],
        )
        self.assertTrue(job["dependency_digest"])

    def test_render_uses_current_gate_bound_plan_not_another_confirmed_plan(self):
        manifest_dir, packaging_files = self.make_manifests()
        alternate_bgm = self.root / "assets" / "pack" / "bgm-alt.mp3"
        alternate_bgm.write_bytes(valid_test_audio())
        bgm_manifest = manifest_dir / "bgm.json"
        payload = json.loads(bgm_manifest.read_text(encoding="utf-8"))
        payload["items"].append({
            "id": "bgm-alt",
            "file": "assets/pack/bgm-alt.mp3",
            "license": "commercial-approved",
            "default_gain": 0.25,
        })
        bgm_manifest.write_text(json.dumps(payload), encoding="utf-8")
        original_plan_id = self.add_packaging_items(manifest_dir)
        unbound_plan_id = packaging_plan.clone_plan(
            self.db, original_plan_id, plan_name="alternate confirmed plan"
        )
        current_plan_id = packaging_plan.clone_plan(
            self.db, original_plan_id, plan_name="current gate-bound plan"
        )

        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            packaging_plan.replace_item(
                self.db,
                unbound_plan_id,
                "bgm",
                "bgm-alt",
                "assets/pack/bgm-alt.mp3",
            )
            packaging_plan.replace_item(
                self.db, unbound_plan_id, "subtitle", "subtitle-alt"
            )
            confirm_plan(self.db, unbound_plan_id)
            confirm_plan(self.db, current_plan_id)

        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            job = self.compile()

        self.assertNotEqual(unbound_plan_id, current_plan_id)
        self.assertEqual(job["packaging"]["plan_id"], current_plan_id)
        self.assertEqual(job["plan"]["bgm"]["src"], str(packaging_files["bgm"].resolve()))

    def test_task_asset_without_available_location_is_blocked(self):
        self.db.connection.execute(
            "UPDATE asset_locations SET state='missing' WHERE asset_id='asset-digital'"
        )
        self.db.connection.commit()
        self.assert_blocked("ASSET_LOCATION_UNAVAILABLE")

    def test_offline_library_location_is_blocked(self):
        self.db.connection.execute("UPDATE libraries SET online=0 WHERE library_id='lib'")
        self.db.connection.commit()
        self.assert_blocked("ASSET_LOCATION_UNAVAILABLE")

    def test_available_location_with_missing_file_is_blocked(self):
        self.broll.unlink()
        self.assert_blocked("ASSET_FILE_MISSING")

    def test_scene_card_asset_must_belong_to_task_assets(self):
        self.db.connection.execute(
            "DELETE FROM task_assets WHERE task_id='task-1' AND asset_id='asset-broll'"
        )
        self.db.connection.commit()
        self.assert_blocked("SCENE_ASSET_NOT_IN_TASK")

    def test_invalid_narrative_timecode_is_blocked(self):
        self.db.connection.execute(
            "UPDATE scene_cards SET narrative_end=narrative_start WHERE card_id='task-1#1'"
        )
        self.db.connection.commit()
        self.assert_blocked("INVALID_TIMECODE")

    def test_nonzero_source_window_is_compiled_separately_from_narrative_timeline(self):
        self.db.connection.execute(
            "UPDATE scene_cards SET enter_tc=1.0, exit_tc=4.0 WHERE card_id='task-1#1'"
        )
        self.db.connection.commit()
        set_picture_lock(self.db, "task-1")
        job = self.compile()
        insert = job["plan"]["inserts"][0]
        self.assertEqual((insert["start"], insert["end"]), (6.0, 9.0))
        self.assertEqual((insert["sourceStart"], insert["sourceEnd"]), (1.0, 4.0))

    def test_segment_at_30_seconds_compiles_a_30_to_33_source_window(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO segments(segment_id, asset_id, start_sec, end_sec) "
                "VALUES('seg-30-33', 'asset-broll', 30.0, 33.0)"
            )
            connection.execute(
                "UPDATE scene_cards SET segment_id='seg-30-33',enter_tc=30,exit_tc=33 WHERE card_id='task-1#1'"
            )
        set_picture_lock(self.db, "task-1")
        job = self.compile()
        insert = job["plan"]["inserts"][0]
        self.assertEqual((insert["sourceStart"], insert["sourceEnd"]), (30.0, 33.0))

    def test_segment_must_belong_to_scene_card_asset(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO segments(segment_id, asset_id, start_sec, end_sec) "
                "VALUES('wrong-owner', 'asset-digital', 0.0, 3.0)"
            )
            connection.execute(
                "UPDATE scene_cards SET segment_id='wrong-owner' WHERE card_id='task-1#1'"
            )
        self.assert_blocked("SEGMENT_ASSET_MISMATCH")

    def test_zero_offset_segment_must_cover_narrative_duration_and_is_snapshotted(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO segments(segment_id, asset_id, start_sec, end_sec) "
                "VALUES('short', 'asset-broll', 0.0, 2.0)"
            )
            connection.execute(
                "UPDATE scene_cards SET segment_id='short' WHERE card_id='task-1#1'"
            )
        self.assert_blocked("SEGMENT_TOO_SHORT")

    def test_image_insert_has_stable_zero_source_window(self):
        image = self.media / "info.png"
        image.write_bytes(b"image")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) VALUES('asset-info','image',5,'info-fp',datetime('now'))"
            )
            connection.execute(
                "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) VALUES('asset-info','lib','info.png',1,'available')"
            )
            connection.execute(
                "INSERT INTO task_assets(task_id,asset_id,role) VALUES('task-1','asset-info','information_card')"
            )
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_start,narrative_end,asset_id,mix_form,
                  enter_tc,exit_tc,audio_policy_draft,confirm_status
                ) VALUES('task-1#2','task-1',2,9,11,'asset-info','信息卡',0,0,'mute',1)
                """
            )
        set_picture_lock(self.db, "task-1")
        job = self.compile()
        image_insert = next(item for item in job["plan"]["inserts"] if item["src"].endswith("info.png"))
        self.assertEqual((image_insert["sourceStart"], image_insert["sourceEnd"]), (0.0, 0.0))

    def test_fullscreen_broll_photo_compiles_as_fullscreen_still(self):
        photo = self.media / "photo-broll.jpg"
        photo.write_bytes(b"photo")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) VALUES('asset-photo','image',5,'photo-fp',datetime('now'))"
            )
            connection.execute(
                "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) VALUES('asset-photo','lib','photo-broll.jpg',1,'available')"
            )
            connection.execute(
                "INSERT INTO task_assets(task_id,asset_id,role) VALUES('task-1','asset-photo','broll')"
            )
            connection.execute(
                "UPDATE scene_cards SET asset_id='asset-photo',segment_id=NULL,enter_tc=0,exit_tc=0 WHERE card_id='task-1#1'"
            )
        set_picture_lock(self.db, "task-1")

        job = self.compile()
        photo_insert = next(item for item in job["plan"]["inserts"] if item["src"].endswith("photo-broll.jpg"))
        self.assertEqual(photo_insert["type"], "broll")
        self.assertNotIn("position", photo_insert)
        self.assertEqual((photo_insert["sourceStart"], photo_insert["sourceEnd"]), (0.0, 0.0))

    def test_reordered_visual_payload_is_compiled_in_the_new_fixed_narrative_slot(self):
        from media_catalog.rough_cut import apply_edit, preview_edit_impact

        second_broll = self.media / "broll-second.mp4"
        second_broll.write_bytes(b"second-broll")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO assets(asset_id,media_kind,size_bytes,quick_fingerprint,created_at) VALUES('asset-second','video',12,'second-fp',datetime('now'))"
            )
            connection.execute(
                "INSERT INTO asset_locations(asset_id,library_id,relative_path,mtime_ns,state) VALUES('asset-second','lib','broll-second.mp4',1,'available')"
            )
            connection.execute(
                "INSERT INTO task_assets(task_id,asset_id,role) VALUES('task-1','asset-second','broll')"
            )
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                  asset_id,mix_form,enter_tc,exit_tc,audio_policy_draft,confirm_status
                ) VALUES('task-1#2','task-1',2,'second slot',9,12,'asset-second','broll全屏',5,8,'mute',1)
                """
            )
        set_picture_lock(self.db, "task-1")
        edit = {
            "action": "reorder",
            "card_ids": ["task-1#0", "task-1#2", "task-1#1"],
        }
        preview = preview_edit_impact(self.db, "task-1", edit)
        apply_edit(self.db, "task-1", edit, impact_token=preview["impact_token"])
        set_picture_lock(self.db, "task-1")

        job = self.compile()
        self.assertEqual(
            [
                (Path(item["src"]).name, item["start"], item["end"], item["sourceStart"], item["sourceEnd"])
                for item in job["plan"]["inserts"]
            ],
            [
                ("broll-second.mp4", 6.0, 9.0, 5.0, 8.0),
                ("broll.mp4", 9.0, 12.0, 0.0, 3.0),
            ],
        )

    def test_invalid_segment_and_source_windows_are_blocked(self):
        cases = (
            ("UPDATE scene_cards SET enter_tc=4,exit_tc=3 WHERE card_id='task-1#1'", "INVALID_TIMECODE"),
            ("UPDATE scene_cards SET enter_tc=?,exit_tc=3 WHERE card_id='task-1#1'", "INVALID_TIMECODE"),
        )
        for sql, code in cases:
            with self.subTest(sql=sql):
                params = (float("inf"),) if "?" in sql else ()
                self.db.connection.execute(sql, params)
                self.db.connection.commit()
                set_picture_lock(self.db, "task-1")
                self.assert_blocked(code)
                self.tearDown()
                self.setUp()

    def test_build_is_pure_and_identical_plan_write_is_safely_reused(self):
        output = self.root / "out" / "task-1.mp4"
        with patch("media_catalog.render_mix_adapter.PROJECT_ROOT", self.root):
            job = build_render_mix_job(self.db, "task-1", output, None)
            self.assertFalse((self.root / "runtime").exists())
            written = write_render_mix_plan(job, self.root / "work")
            plan_path = Path(written["plan_path"])
            self.assertIn(written["plan_digest"], plan_path.name)
            original = plan_path.read_bytes()
            reused = write_render_mix_plan(job, self.root / "work")
            self.assertEqual(reused["plan_path"], written["plan_path"])
            self.assertEqual(plan_path.read_bytes(), original)

    def test_windows_long_work_dir_uses_short_project_render_plan_path(self):
        job = build_render_mix_job(
            self.db, "task-1", self.root / "out" / "task-1.mp4", None
        )
        long_work_dir = self.root.joinpath(*(["candidate-" + "x" * 48] * 4))

        with patch("media_catalog.render_mix_adapter.os.name", "nt"):
            written = write_render_mix_plan(job, long_work_dir)

        plan_path = Path(written["plan_path"])
        self.assertEqual(
            plan_path.parent,
            (self.root / "runtime" / "render-plans").resolve(),
        )
        self.assertLessEqual(len(str(plan_path)), 240)

    def test_existing_plan_with_different_bytes_is_never_reused_or_overwritten(self):
        job = build_render_mix_job(self.db, "task-1", self.root / "out/task-1.mp4", None)
        written = write_render_mix_plan(job, self.root / "work")
        plan_path = Path(written["plan_path"])
        plan_path.write_text('{"tampered": true}', encoding="utf-8")
        with self.assertRaisesRegex(RenderMixJobError, r"^PLAN_CONTENT_MISMATCH:"):
            write_render_mix_plan(job, self.root / "work")
        self.assertEqual(plan_path.read_text(encoding="utf-8"), '{"tampered": true}')

    def test_ass_with_mismatched_binding_is_blocked_after_file_lookup(self):
        external = self.root / "mismatched.ass"
        external.write_bytes(b"mismatched")
        external.with_suffix(".metadata.json").write_text(json.dumps({
            "schema": 1,
            "preset": "wrong-preset",
            "coverage": 1.0,
            "cues": [],
            "assSha256": hashlib.sha256(external.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        with self.assertRaisesRegex(RenderMixJobError, r"^ASS_PRESET_MISMATCH:"):
            compile_render_mix_job(
                self.db,
                "task-1",
                self.root / "work",
                self.root / "out/task-1.mp4",
                external,
            )

    def test_manifest_restricted_prohibited_and_unknown_licenses_are_blocked(self):
        confirmed_manifest_dir, _ = self.make_manifests()
        self.add_packaging_items(confirmed_manifest_dir)
        for license_name in ("restricted", "prohibited", "unknown"):
            with self.subTest(license=license_name):
                manifest_dir, _ = self.make_manifests(license_name=license_name)
                with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
                    self.assert_blocked("PACKAGING_LICENSE_BLOCKED")

    def test_referenced_manifest_file_must_exist(self):
        manifest_dir, files = self.make_manifests()
        self.add_packaging_items(manifest_dir)
        files["bgm"].unlink()
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            self.assert_blocked("PACKAGING_FILE_MISSING")

    def test_backup_must_be_registered_in_manifest(self):
        manifest_dir, _ = self.make_manifests()
        draft_plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="unknown backup fixture"
        )
        packaging_plan.add_item(
            self.db, draft_plan_id, "bgm", "not-in-manifest", is_backup=True, rank=1
        )
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            with self.assertRaisesRegex(RuntimeError, r"^PACKAGING_MANIFEST_UNKNOWN:"):
                confirm_plan(self.db, draft_plan_id)

    def test_registered_backup_file_must_exist(self):
        manifest_dir, _ = self.make_manifests()
        bgm_manifest = manifest_dir / "bgm.json"
        payload = json.loads(bgm_manifest.read_text(encoding="utf-8"))
        payload["items"].append({"id": "missing-backup", "file": "assets/pack/missing-backup.mp3", "license": "commercial-approved"})
        bgm_manifest.write_text(json.dumps(payload), encoding="utf-8")
        draft_plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="missing backup fixture"
        )
        packaging_plan.add_item(
            self.db, draft_plan_id, "bgm", "missing-backup", is_backup=True, rank=1
        )
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            with self.assertRaisesRegex(RuntimeError, r"^PACKAGING_FILE_MISSING:"):
                confirm_plan(self.db, draft_plan_id)

    def test_prohibited_backup_is_blocked_even_when_primary_is_approved(self):
        manifest_dir, _ = self.make_manifests()
        bgm_manifest = manifest_dir / "bgm.json"
        payload = json.loads(bgm_manifest.read_text(encoding="utf-8"))
        payload["items"].append({"id": "prohibited-backup", "file": "assets/pack/bgm.mp3", "license": "prohibited"})
        bgm_manifest.write_text(json.dumps(payload), encoding="utf-8")
        draft_plan_id = packaging_plan.clone_plan(
            self.db, "plan-1", plan_name="prohibited backup fixture"
        )
        packaging_plan.add_item(
            self.db, draft_plan_id, "bgm", "prohibited-backup", is_backup=True, rank=1
        )
        with patch.object(packaging_plan, "MANIFEST_DIR", manifest_dir):
            with self.assertRaisesRegex(RuntimeError, r"^PACKAGING_LICENSE_BLOCKED:"):
                confirm_plan(self.db, draft_plan_id)

class RenderMixRunnerTests(unittest.TestCase):
    def test_default_atr_path_follows_current_windows_profile(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": r"D:\Users\colleague\AppData\Local"}):
            self.assertEqual(
                Path(r"D:\Users\colleague\AppData\Local\PeterAgentRuntime\bin\atr.exe"),
                _default_atr_path(),
            )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "out").mkdir()
        self.input_path = self.root / "digital.mp4"
        self.plan_path = self.root / "render-mix.plan.json"
        self.output_path = self.project / "out" / "final.mp4"
        self.node_path = self.root / "node.exe"
        self.ts_node_path = self.root / "node_modules/ts-node/dist/bin.js"
        self.render_script = self.project / "scripts/render-mix.ts"
        for file_path, content in (
            (self.input_path, b"input"),
            (self.plan_path, b"{}"),
            (self.node_path, b"node"),
            (self.ts_node_path, b"ts-node"),
            (self.render_script, b"render"),
        ):
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        self.job = {
            "task_id": "task-1",
            "project_root": str(self.project),
            "work_dir": str(self.root / "work"),
            "input_path": str(self.input_path),
            "plan_path": str(self.plan_path),
            "output_path": str(self.output_path),
            "ass_path": None,
            "asset_root": str(self.root / "assets"),
            "node_path": str(self.node_path),
            "ts_node_path": str(self.ts_node_path),
            "render_script": str(self.render_script),
            "timeout_seconds": 321,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_runs_explicit_local_render_command_with_controlled_process_options(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            output = Path(command[command.index("--output") + 1])
            self.assertIn(".part-", output.name)
            output.write_bytes(b"fresh-render")
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        result = run_render_mix(
            self.job,
            runner=fake_runner,
            env={"PATH": "controlled", "HTTP_PROXY": "dead", "HTTPS_PROXY": "dead",
                 "NODE_OPTIONS": "--require malicious", "UNRELATED_SECRET": "nope"},
        )

        self.assertEqual(result, self.output_path.resolve())
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command[:3], [
            str(self.node_path.resolve()),
            str(self.ts_node_path.resolve()),
            str(self.render_script.resolve()),
        ])
        self.assertNotIn("npx", " ".join(command).lower())
        for flag, value in (
            ("--input", self.input_path.resolve()),
            ("--plan", self.plan_path.resolve()),
            ("--asset-root", (self.root / "assets").resolve()),
        ):
            self.assertEqual(Path(command[command.index(flag) + 1]), value)
        self.assertIn("--ass", command)
        sentinel = Path(command[command.index("--ass") + 1])
        self.assertFalse(sentinel.exists())
        self.assertIn("subtitles-disabled-", sentinel.name)
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(Path(kwargs["cwd"]), self.project.resolve())
        self.assertEqual(kwargs["timeout"], 321)
        self.assertEqual(kwargs["env"]["PATH"], "controlled")
        self.assertEqual(kwargs["env"]["HTTP_PROXY"], "")
        self.assertEqual(kwargs["env"]["HTTPS_PROXY"], "")
        self.assertEqual(kwargs["env"]["NODE_OPTIONS"], "")
        self.assertNotIn("UNRELATED_SECRET", kwargs["env"])

    def test_preview_profile_uses_cache_root_and_explicit_cli_flags(self):
        job = dict(self.job)
        preview = (
            self.project
            / "runtime/workbench/cache/packaging-previews/task-1/preview.mp4"
        )
        job["output_path"] = str(preview)
        job["output_profile"] = {
            "maxDuration": 10.0,
            "width": 540,
            "height": 960,
            "crf": 28,
        }
        commands = []

        def fake_runner(command, **_kwargs):
            commands.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(b"preview")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertEqual(run_render_mix(job, runner=fake_runner), preview.resolve())
        command = commands[0]
        self.assertEqual(command[command.index("--max-duration") + 1], "10.0")
        self.assertEqual(command[command.index("--width") + 1], "540")
        self.assertEqual(command[command.index("--height") + 1], "960")
        self.assertEqual(command[command.index("--crf") + 1], "28")

    def test_controlled_env_removes_ts_node_configuration_overrides(self):
        job = dict(self.job)
        job["controlled_env"] = {
            "TS_NODE_PROJECT": "declared-project.json",
            "TS_NODE_TRANSPILE_ONLY": "true",
            "TS_NODE_COMPILER_OPTIONS": "{\"module\":\"declared\"}",
        }
        source = {
            "PATH": "controlled",
            "HTTP_PROXY": "dead",
            "TS_NODE_PROJECT": "malicious-project.json",
            "TS_NODE_TRANSPILE_ONLY": "1",
            "TS_NODE_COMPILER_OPTIONS": "{\"module\":\"broken\"}",
        }

        controlled = _controlled_render_env(job, source, self.node_path)

        self.assertEqual(controlled["HTTP_PROXY"], "")
        for name in (
            "TS_NODE_PROJECT",
            "TS_NODE_TRANSPILE_ONLY",
            "TS_NODE_COMPILER_OPTIONS",
        ):
            self.assertNotIn(name, controlled)

    def test_nonzero_return_code_is_blocked(self):
        def failed_runner(_command, **_kwargs):
            return SimpleNamespace(returncode=7, stdout="", stderr="ffmpeg failed")

        with self.assertRaisesRegex(RenderMixJobError, r"^RENDER_PROCESS_FAILED:") as raised:
            run_render_mix(self.job, runner=failed_runner)
        self.assertEqual(raised.exception.code, "RENDER_PROCESS_FAILED")

    def test_success_without_output_is_blocked(self):
        def no_output_runner(_command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self.assertRaisesRegex(RenderMixJobError, r"^RENDER_OUTPUT_MISSING:"):
            run_render_mix(self.job, runner=no_output_runner)

    def test_unchanged_preexisting_output_is_not_accepted_as_fresh(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(b"stale")

        def stale_runner(_command, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self.assertRaisesRegex(RenderMixJobError, r"^RENDER_OUTPUT_EXISTS:"):
            run_render_mix(self.job, runner=stale_runner)
        self.assertEqual(self.output_path.read_bytes(), b"stale")

    def test_output_must_be_contained_in_project_out(self):
        self.job["output_path"] = str(self.project.parent / "escaped.mp4")
        with self.assertRaisesRegex(RenderMixJobError, r"^OUTPUT_OUTSIDE_PROJECT:"):
            run_render_mix(self.job, runner=lambda *_a, **_k: None)

    def test_output_must_not_alias_input(self):
        input_inside_out = self.project / "out" / "input.mp4"
        input_inside_out.write_bytes(b"source")
        self.job["input_path"] = str(input_inside_out)
        self.job["output_path"] = str(input_inside_out)
        with self.assertRaisesRegex(RenderMixJobError, r"^OUTPUT_PATH_COLLISION:"):
            run_render_mix(self.job, runner=lambda *_a, **_k: None)
        self.assertEqual(input_inside_out.read_bytes(), b"source")

    def test_failed_render_cleans_part_without_touching_final(self):
        seen = []
        def failed(command, **_kwargs):
            part = Path(command[command.index("--output") + 1])
            seen.append(part)
            part.write_bytes(b"partial")
            return SimpleNamespace(returncode=9, stdout="", stderr="failed")
        with self.assertRaisesRegex(RenderMixJobError, r"^RENDER_PROCESS_FAILED:"):
            run_render_mix(self.job, runner=failed)
        self.assertFalse(self.output_path.exists())
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())

    def test_timeout_is_reported_as_stable_render_error(self):
        def timeout_runner(_command, **_kwargs):
            raise subprocess.TimeoutExpired("node", 321)

        with self.assertRaisesRegex(RenderMixJobError, r"^RENDER_TIMEOUT:"):
            run_render_mix(self.job, runner=timeout_runner)

    def test_default_runner_prefers_full_atr_contract_then_falls_back_on_internal_error(self):
        atr = self.root / "atr.exe"
        atr.write_bytes(b"atr")
        atr_result = SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"status": "permanent_failure",
                               "error": {"code": "runtime.internal_error"}}),
            stderr="",
        )
        fallback_result = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        kwargs = {
            "shell": False, "cwd": str(self.project), "env": {"PATH": "x"},
            "timeout": 9, "check": False, "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8",
            "errors": "replace",
        }
        with (
            patch("media_catalog.render_mix_adapter.ATR_PATH", atr),
            patch("media_catalog.render_mix_adapter.subprocess.run", return_value=atr_result) as run,
            patch("media_catalog.render_mix_adapter._run_process_tree",
                  return_value=fallback_result) as fallback,
        ):
            result = _default_process_runner([str(self.node_path), "-e", "ok"], **kwargs)
        self.assertIs(result, fallback_result)
        argv = run.call_args.args[0]
        for flag in ("--exe", "--cwd", "--timeout-ms", "--max-output-bytes",
                     "--encoding", "--caller-agent", "--json"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--timeout-ms") + 1], "9000")
        self.assertEqual(argv[argv.index("--caller-agent") + 1], "codex")
        fallback.assert_called_once()

    def test_default_runner_falls_back_on_atr_request_protocol_errors(self):
        atr = self.root / "atr.exe"
        atr.write_bytes(b"atr")
        fallback_result = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        kwargs = {
            "shell": False, "cwd": str(self.project), "env": {"PATH": "x"},
            "timeout": 9, "check": False, "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8",
            "errors": "replace",
        }
        for error_code in ("request.flag_unknown", "request.argument_unexpected"):
            with self.subTest(error_code=error_code):
                atr_result = SimpleNamespace(
                    returncode=2,
                    stdout=json.dumps({
                        "status": "permanent_failure",
                        "error": {"code": error_code},
                    }),
                    stderr="",
                )
                with (
                    patch("media_catalog.render_mix_adapter.ATR_PATH", atr),
                    patch("media_catalog.render_mix_adapter.subprocess.run", return_value=atr_result),
                    patch("media_catalog.render_mix_adapter._run_process_tree",
                          return_value=fallback_result) as fallback,
                ):
                    result = _default_process_runner(
                        [str(self.node_path), "-e", "ok"], **kwargs
                    )
                self.assertIs(result, fallback_result)
                fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
