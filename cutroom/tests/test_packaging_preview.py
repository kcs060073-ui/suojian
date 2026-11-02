import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog import packaging_plan
from media_catalog.packaging_preview import (
    build_preview_input,
    confirm_plan_after_preview,
    current_preview_matches,
    PackagingPreviewInput,
    PreviewIdentity,
    create_preview_artifact,
    finalize_preview_artifact,
    find_reusable_preview,
    get_preview_artifact,
    mark_preview_viewed,
    request_preview,
    render_packaging_preview,
    resolve_packaging_preview_media,
    set_preview_status,
)
from media_catalog.packaging_plan import create_blank_plan, replace_item
from media_catalog.scene_cards import set_picture_lock
from media_catalog.tasks import create_task, set_status


class PackagingPreviewRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        create_task(self.db, "task-1", "包装预览测试", "digital_human")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO packaging_plans(plan_id,task_id,route,plan_name) "
                "VALUES(?,?,?,?)",
                ("plan-1", "task-1", "数字人混剪", "方案一"),
            )
        self.identity = PreviewIdentity(
            task_id="task-1",
            plan_id="plan-1",
            picture_lock_digest="picture-a",
            plan_digest="plan-a",
            renderer_version="packaging-preview-v1",
        )

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_migration_creates_preview_and_catalog_check_tables(self):
        names = {
            row[0]
            for row in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("packaging_preview_artifacts", names)
        self.assertIn("packaging_catalog_checks", names)

    def test_same_canonical_input_has_one_active_artifact(self):
        first = create_preview_artifact(self.db, self.identity)
        second = create_preview_artifact(self.db, self.identity)
        self.assertEqual(first["preview_id"], second["preview_id"])
        self.assertEqual(first["canonical_key"], second["canonical_key"])
        self.assertEqual(find_reusable_preview(self.db, self.identity)["preview_id"], first["preview_id"])

    def test_ready_artifact_is_signed_viewable_and_resolvable(self):
        preview = create_preview_artifact(self.db, self.identity)
        media = (
            self.root
            / "runtime"
            / "workbench"
            / "cache"
            / "packaging-previews"
            / "task-1"
            / "preview.mp4"
        )
        media.parent.mkdir(parents=True)
        media.write_bytes(b"valid-mp4")

        ready = finalize_preview_artifact(
            self.db,
            preview["preview_id"],
            project_root=self.root,
            rel_path=media.relative_to(self.root).as_posix(),
            duration_seconds=9.5,
            coverage=[{"slot": "subtitle", "status": "rendered"}],
        )
        viewed = mark_preview_viewed(self.db, "task-1", preview["preview_id"])

        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["size_bytes"], len(b"valid-mp4"))
        self.assertEqual(ready["coverage"], [{"slot": "subtitle", "status": "rendered"}])
        self.assertIsNotNone(viewed["viewed_at"])
        self.assertEqual(
            resolve_packaging_preview_media(self.db, preview["preview_id"], self.root),
            media.resolve(),
        )

    def test_deleted_or_tampered_cache_is_not_resolved(self):
        preview = create_preview_artifact(self.db, self.identity)
        media = (
            self.root
            / "runtime"
            / "workbench"
            / "cache"
            / "packaging-previews"
            / "task-1"
            / "preview.mp4"
        )
        media.parent.mkdir(parents=True)
        media.write_bytes(b"valid-mp4")
        finalize_preview_artifact(
            self.db,
            preview["preview_id"],
            project_root=self.root,
            rel_path=media.relative_to(self.root).as_posix(),
            duration_seconds=8.0,
            coverage=[],
        )

        media.write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_MEDIA_CHANGED"):
            resolve_packaging_preview_media(self.db, preview["preview_id"], self.root)

        media.unlink()
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_MEDIA_MISSING"):
            resolve_packaging_preview_media(self.db, preview["preview_id"], self.root)
        self.assertEqual(get_preview_artifact(self.db, preview["preview_id"])["status"], "missing")

    def test_invalid_transitions_and_view_before_ready_fail_closed(self):
        preview = create_preview_artifact(self.db, self.identity)
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_NOT_READY"):
            mark_preview_viewed(self.db, "task-1", preview["preview_id"])
        set_preview_status(self.db, preview["preview_id"], "rendering")
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_STATE_INVALID"):
            set_preview_status(self.db, preview["preview_id"], "queued")


class PackagingPreviewInputTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        self.manifest_dir = self.root / "assets" / "manifests"
        self.manifest_dir.mkdir(parents=True)
        pack_dir = self.root / "assets" / "pack"
        pack_dir.mkdir(parents=True)
        self.subtitle_path = self.root / "assets" / "fonts" / "subtitle.otf"
        self.subtitle_path.parent.mkdir(parents=True)
        self.subtitle_path.write_bytes(b"font-data")
        self.bgm_a = pack_dir / "bgm-a.wav"
        self.bgm_b = pack_dir / "bgm-b.wav"
        self._write_wave(self.bgm_a)
        self._write_wave(self.bgm_b)
        (self.manifest_dir / "subtitle-presets.json").write_text(
            '{"category":"subtitle","items":[{"id":"sub-a",'
            '"font":"subtitle.otf","license":"commercial-approved",'
            '"default_for":["数字人混剪","园区宣传","信息流广告"]}]}',
            encoding="utf-8",
        )
        (self.manifest_dir / "bgm.json").write_text(
            '{"category":"bgm","items":['
            '{"id":"bgm-a","file":"assets/pack/bgm-a.wav",'
            '"license":"commercial-approved","default_for":["数字人混剪"]},'
            '{"id":"bgm-b","file":"assets/pack/bgm-b.wav",'
            '"license":"commercial-approved","default_for":["数字人混剪"]}]}',
            encoding="utf-8",
        )
        self.manifest_patch = patch.object(
            packaging_plan, "MANIFEST_DIR", self.manifest_dir
        )
        self.manifest_patch.start()
        self.plan_id = self._seed_task("task-1", 20.0)
        self.short_plan_id = self._seed_task("task-short", 5.25)

    def tearDown(self):
        self.manifest_patch.stop()
        self.db.close()
        self.temp_dir.cleanup()

    @staticmethod
    def _write_wave(path: Path) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 800)

    def _seed_task(self, task_id: str, duration: float) -> str:
        create_task(
            self.db,
            task_id,
            task_id,
            "digital_human",
            brief={"script": "测试"},
        )
        set_status(self.db, task_id, "brief_locked")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                  mix_form,confirm_status
                ) VALUES(?,?,?,?,?,?,?,1)
                """,
                (f"{task_id}#1", task_id, 1, "测试", 0.0, duration, "数字人全屏"),
            )
        set_picture_lock(self.db, task_id)
        plan_id = create_blank_plan(self.db, task_id, "数字人混剪", "预览方案")
        replace_item(self.db, plan_id, "subtitle", "sub-a")
        replace_item(self.db, plan_id, "bgm", "bgm-a")
        return plan_id

    def _seed_ad_task(self, task_id: str, duration: float) -> tuple[str, Path]:
        task_root = self.root / "runtime/workbench/tasks" / task_id
        script_path = task_root / "inputs/final-script.txt"
        script_path.parent.mkdir(parents=True)
        script_path.write_text("锁定广告文案", encoding="utf-8")
        create_task(
            self.db,
            task_id,
            task_id,
            "ad_mix",
            brief={
                "route": "ad_mix",
                "final_script": {
                    "relative_path": "inputs/final-script.txt",
                    "sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
                },
            },
        )
        set_status(self.db, task_id, "brief_locked")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                  mix_form,confirm_status
                ) VALUES(?,?,?,?,?,?,?,1)
                """,
                (f"{task_id}#1", task_id, 1, "广告", 0.0, duration, "素材全屏"),
            )
        set_picture_lock(self.db, task_id)
        plan_id = create_blank_plan(self.db, task_id, "信息流广告", "广告预览方案")
        replace_item(self.db, plan_id, "subtitle", "sub-a")
        return plan_id, script_path

    def test_preview_input_is_owned_and_digest_changes_with_plan(self):
        before = build_preview_input(self.db, "task-1", self.plan_id)
        replace_item(self.db, self.plan_id, "bgm", "bgm-b")
        after = build_preview_input(self.db, "task-1", self.plan_id)

        self.assertNotEqual(before.identity.plan_digest, after.identity.plan_digest)
        self.assertEqual(after.duration_seconds, 12.0)
        self.assertEqual(after.coverage_targets, ("bgm", "subtitle"))

        with self.assertRaisesRegex(PermissionError, "PACKAGING_PLAN_NOT_OWNED"):
            build_preview_input(self.db, "task-short", self.plan_id)

    def test_preview_identity_changes_when_packaging_bytes_or_manifest_change(self):
        before = build_preview_input(self.db, "task-1", self.plan_id)
        with self.bgm_a.open("ab") as output:
            output.write(b"replacement")
        after_file = build_preview_input(self.db, "task-1", self.plan_id)
        self.assertNotEqual(before.identity.input_digest, after_file.identity.input_digest)

        manifest_path = self.manifest_dir / "bgm.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["items"][0]["default_gain"] = 0.07
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        after_manifest = build_preview_input(self.db, "task-1", self.plan_id)
        self.assertNotEqual(after_file.identity.input_digest, after_manifest.identity.input_digest)

    def test_ready_viewed_preview_becomes_stale_when_packaging_file_changes(self):
        value = build_preview_input(self.db, "task-1", self.plan_id)
        preview = create_preview_artifact(self.db, value.identity)
        media = self.root / "runtime/workbench/cache/packaging-previews/task-1/stale-file.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"preview-media")
        finalize_preview_artifact(
            self.db,
            preview["preview_id"],
            project_root=self.root,
            rel_path=media.relative_to(self.root).as_posix(),
            duration_seconds=8.0,
            coverage=[],
        )
        mark_preview_viewed(self.db, "task-1", preview["preview_id"])
        with self.bgm_a.open("ab") as output:
            output.write(b"changed-after-preview")

        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_STALE"):
            confirm_plan_after_preview(
                self.db,
                "task-1",
                self.plan_id,
                preview["preview_id"],
                project_root=self.root,
            )

    def test_ad_preview_becomes_stale_when_locked_script_bytes_change(self):
        plan_id, script_path = self._seed_ad_task("task-ad", 10.0)
        value = build_preview_input(self.db, "task-ad", plan_id)
        preview = create_preview_artifact(self.db, value.identity)
        media = self.root / "runtime/workbench/cache/packaging-previews/task-ad/script.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"preview-media")
        finalize_preview_artifact(
            self.db,
            preview["preview_id"],
            project_root=self.root,
            rel_path=media.relative_to(self.root).as_posix(),
            duration_seconds=8.0,
            coverage=[],
        )
        mark_preview_viewed(self.db, "task-ad", preview["preview_id"])
        script_path.write_text("锁定广告文案已被修改", encoding="utf-8")

        self.assertFalse(current_preview_matches(self.db, preview))
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_STALE"):
            confirm_plan_after_preview(
                self.db,
                "task-ad",
                plan_id,
                preview["preview_id"],
                project_root=self.root,
            )

    def test_worker_result_is_stale_when_picture_lock_changes_during_render(self):
        value = build_preview_input(self.db, "task-1", self.plan_id)
        preview = create_preview_artifact(self.db, value.identity)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE scene_cards SET narrative_text='已经修改' WHERE card_id='task-1#1'"
            )
        set_picture_lock(self.db, "task-1")

        self.assertFalse(current_preview_matches(self.db, preview))

    def test_task_shorter_than_eight_seconds_keeps_real_duration(self):
        value = build_preview_input(self.db, "task-short", self.short_plan_id)
        self.assertEqual(value.duration_seconds, 5.25)

    def test_unsupported_route_fails_closed(self):
        create_task(self.db, "task-park", "园区", "park", brief={"script": "园区"})
        set_status(self.db, "task-park", "brief_locked")
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scene_cards(
                  card_id,task_id,idx,narrative_start,narrative_end,confirm_status
                ) VALUES('task-park#1','task-park',1,0,2,1)
                """
            )
        set_picture_lock(self.db, "task-park")
        plan_id = create_blank_plan(self.db, "task-park", "园区宣传", "园区方案")
        replace_item(self.db, plan_id, "subtitle", "sub-a")
        with self.assertRaisesRegex(RuntimeError, "UNSUPPORTED_ROUTE"):
            build_preview_input(self.db, "task-park", plan_id)

    def test_empty_timeline_fails_closed(self):
        empty_plan_id = self._seed_task("task-empty", 0.0)
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_TIMELINE_EMPTY"):
            build_preview_input(self.db, "task-empty", empty_plan_id)

    def test_confirmation_requires_current_viewed_preview_and_records_evidence(self):
        value = build_preview_input(self.db, "task-1", self.plan_id)
        preview = create_preview_artifact(self.db, value.identity)
        media = (
            self.root
            / "runtime/workbench/cache/packaging-previews/task-1/confirm.mp4"
        )
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"preview-media")
        finalize_preview_artifact(
            self.db,
            preview["preview_id"],
            project_root=self.root,
            rel_path=media.relative_to(self.root).as_posix(),
            duration_seconds=8.0,
            coverage=[{"slot": "subtitle", "status": "rendered"}],
        )
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_NOT_VIEWED"):
            confirm_plan_after_preview(
                self.db,
                "task-1",
                self.plan_id,
                preview["preview_id"],
                project_root=self.root,
            )
        mark_preview_viewed(self.db, "task-1", preview["preview_id"])

        confirmed = confirm_plan_after_preview(
            self.db,
            "task-1",
            self.plan_id,
            preview["preview_id"],
            project_root=self.root,
        )
        gate = self.db.connection.execute(
            "SELECT details_json FROM task_gate_events "
            "WHERE task_id='task-1' AND gate_type='packaging' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        details = json.loads(gate["details_json"])
        self.assertEqual(confirmed["preview_id"], preview["preview_id"])
        self.assertEqual(
            details["confirmation_evidence"]["preview_id"],
            preview["preview_id"],
        )


class PackagingPreviewRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        create_task(self.db, "task-1", "预览路由", "digital_human")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO packaging_plans(plan_id,task_id,route,plan_name) "
                "VALUES('plan-1','task-1','数字人混剪','预览方案')"
            )
        self.identity = PreviewIdentity(
            task_id="task-1",
            plan_id="plan-1",
            picture_lock_digest="picture-a",
            plan_digest="plan-a",
            renderer_version="packaging-preview-v1",
        )
        self.value = PackagingPreviewInput(
            identity=self.identity,
            duration_seconds=9.0,
            task_type="digital_human",
            plan={"plan_id": "plan-1", "items": []},
            coverage_targets=("subtitle",),
        )
        self.preview = create_preview_artifact(self.db, self.identity)

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_digital_preview_dispatches_to_mix_renderer(self):
        output = (
            self.root
            / "runtime/workbench/cache/packaging-previews/task-1/preview.mp4"
        )

        def fake_renderer(_db, current, project_root, preview_id, _runner):
            self.assertEqual(current, self.value)
            self.assertEqual(Path(project_root), self.root)
            self.assertEqual(preview_id, self.preview["preview_id"])
            output.parent.mkdir(parents=True)
            output.write_bytes(b"preview")
            return {
                "output_path": output,
                "duration_seconds": 9.0,
                "coverage": [{"slot": "subtitle", "status": "rendered"}],
            }

        with (
            patch(
                "media_catalog.packaging_preview.build_preview_input",
                return_value=self.value,
            ),
            patch(
                "media_catalog.packaging_preview.render_digital_packaging_preview",
                side_effect=fake_renderer,
            ) as renderer,
        ):
            result = render_packaging_preview(
                self.db, self.preview["preview_id"], self.root, runner=None
            )

        renderer.assert_called_once()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["route"], "digital_human")
        self.assertEqual(result["profile"], "portrait-540p-v1")

    def test_resumed_failed_preview_can_render_without_a_new_preview_request(self):
        set_preview_status(self.db, self.preview["preview_id"], "failed", error_code="PROCESS_FAILED")
        output = self.root / "runtime/workbench/cache/packaging-previews/task-1/retry.mp4"
        def render(*_args):
            self.assertEqual(get_preview_artifact(self.db, self.preview["preview_id"])["status"], "rendering")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"retry-preview")
            return {"output_path": output, "duration_seconds": 9.0}
        with (
            patch("media_catalog.packaging_preview.build_preview_input", return_value=self.value),
            patch("media_catalog.packaging_preview.render_digital_packaging_preview", side_effect=render),
        ):
            result = render_packaging_preview(self.db, self.preview["preview_id"], self.root)
        self.assertEqual(result["status"], "ready")
        self.assertIsNone(result["error_code"])
        self.assertIsNone(result["viewed_at"])

    def test_post_render_input_change_marks_artifact_stale(self):
        output = (
            self.root
            / "runtime/workbench/cache/packaging-previews/task-1/stale.mp4"
        )
        changed = PackagingPreviewInput(
            identity=PreviewIdentity(
                task_id="task-1",
                plan_id="plan-1",
                picture_lock_digest="picture-b",
                plan_digest="plan-a",
                renderer_version="packaging-preview-v1",
            ),
            duration_seconds=9.0,
            task_type="digital_human",
            plan=self.value.plan,
            coverage_targets=self.value.coverage_targets,
        )

        def fake_renderer(*_args):
            output.parent.mkdir(parents=True)
            output.write_bytes(b"stale-preview")
            return {"output_path": output, "duration_seconds": 9.0, "coverage": []}

        with (
            patch(
                "media_catalog.packaging_preview.build_preview_input",
                side_effect=[self.value, changed],
            ),
            patch(
                "media_catalog.packaging_preview.render_digital_packaging_preview",
                side_effect=fake_renderer,
            ),
        ):
            result = render_packaging_preview(
                self.db, self.preview["preview_id"], self.root, runner=None
            )

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["error_code"], "PACKAGING_PREVIEW_STALE")


class PackagingPreviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        create_task(self.db, "task-1", "预览队列", "digital_human")
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO packaging_plans(plan_id,task_id,route,plan_name) "
                "VALUES('plan-1','task-1','数字人混剪','预览方案')"
            )
        self.value = PackagingPreviewInput(
            identity=PreviewIdentity(
                task_id="task-1",
                plan_id="plan-1",
                picture_lock_digest="picture-a",
                plan_digest="plan-a",
                renderer_version="packaging-preview-v1",
            ),
            duration_seconds=8.0,
            task_type="digital_human",
            plan={"plan_id": "plan-1", "items": []},
            coverage_targets=("subtitle",),
        )

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def test_repeated_preview_request_reuses_one_job_and_artifact(self):
        with patch(
            "media_catalog.packaging_preview.build_preview_input",
            return_value=self.value,
        ):
            first = request_preview(self.db, "task-1", "plan-1")
            second = request_preview(self.db, "task-1", "plan-1")

        self.assertEqual(first["previewId"], second["previewId"])
        self.assertEqual(first["jobId"], second["jobId"])
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_type='packaging_preview'"
            ).fetchone()[0],
            1,
        )
        row = self.db.connection.execute(
            "SELECT is_paid FROM job_payloads WHERE job_id=?", (first["jobId"],)
        ).fetchone()
        self.assertEqual(row["is_paid"], 0)

    def test_terminal_retry_uses_one_bounded_suffix(self):
        with patch(
            "media_catalog.packaging_preview.build_preview_input",
            return_value=self.value,
        ):
            first = request_preview(self.db, "task-1", "plan-1")
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE jobs SET status='failed' WHERE job_id=?",
                    (first["jobId"],),
                )
            second = request_preview(self.db, "task-1", "plan-1")
            third = request_preview(self.db, "task-1", "plan-1")

        self.assertNotEqual(first["jobId"], second["jobId"])
        self.assertEqual(second["jobId"], third["jobId"])
        self.assertTrue(second["jobId"].endswith("-r1"))

    def test_worker_action_returns_stale_result_after_claimed_input_changes(self):
        from media_catalog.workbench_actions import run_packaging_preview_action

        artifact = create_preview_artifact(self.db, self.value.identity)
        stale = {**artifact, "status": "stale", "error_code": "PACKAGING_PREVIEW_STALE"}
        job = SimpleNamespace(
            task_id="task-1",
            payload={"preview_id": artifact["preview_id"]},
        )
        with patch(
            "media_catalog.packaging_preview.render_packaging_preview",
            return_value=stale,
        ) as renderer:
            result = run_packaging_preview_action(
                self.db, job, self.root, runner=None
            )

        renderer.assert_called_once_with(
            self.db, artifact["preview_id"], self.root.resolve(), None
        )
        self.assertEqual(result["result"]["status"], "stale")


if __name__ == "__main__":
    unittest.main()
