import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from media_catalog.ad_workflow import CONFIRMATIONS as AD_CONFIRMATIONS
from media_catalog.database import CatalogDatabase
from media_catalog.digital_workflow import CONFIRMATIONS as DIGITAL_CONFIRMATIONS
from media_catalog.qa_evidence import qa_evidence_complete
from media_catalog.tasks import create_task
from media_catalog.workbench_actions import build_action_handlers
from media_catalog.workflow import WORKFLOW_STAGES, project_workflow, seed_stage_states
from workbench.api import ApiError, WorkbenchAPI


EXPECTED_CONFIRMATIONS = ("brief", "picture", "packaging", "full_review")
EXPECTED_QA_CHECKS = {
    "video_evidence",
    "black_frame",
    "freeze",
    "silence",
    "clipping",
    "subtitle_evidence",
    "loudness_integrated",
    "true_peak",
    "material_fingerprint",
    "material_scope",
    "material_authorization",
}


class TwoRouteDesignClosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        for task_id, route in (("digital", "digital_human"), ("ad", "ad_mix")):
            create_task(self.db, task_id, f"{route} fixture", route)
            seed_stage_states(self.db, task_id)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_digital_and_ad_routes_share_required_workbench_capabilities(self):
        self.assertEqual(
            tuple(item["key"] for item in DIGITAL_CONFIRMATIONS),
            EXPECTED_CONFIRMATIONS,
        )
        self.assertEqual(
            tuple(item["key"] for item in AD_CONFIRMATIONS),
            EXPECTED_CONFIRMATIONS,
        )
        self.assertEqual(len(WORKFLOW_STAGES), 12)
        for task_id in ("digital", "ad"):
            projected = project_workflow(self.db, task_id)
            self.assertEqual(len(projected["stages"]), 12)
            self.assertEqual(
                set(projected["gates"]),
                {
                    "brief_locked",
                    "picture_lock",
                    "packaging_confirmed",
                    "qa_passed",
                    "human_reviewed",
                },
            )

        digital_render = Mock(return_value={"route": "digital_human", "action": "render"})
        ad_render = Mock(return_value={"route": "ad_mix", "action": "render"})
        digital_publish = Mock(return_value={"route": "digital_human", "action": "publish"})
        ad_publish = Mock(return_value={"route": "ad_mix", "action": "publish"})
        with (
            patch("media_catalog.workbench_actions.render_and_qa", digital_render),
            patch("media_catalog.workbench_actions.build_publish_package", digital_publish),
            patch("media_catalog.ad_actions.render_ad_and_qa", ad_render),
            patch("media_catalog.ad_actions.build_ad_publish_package", ad_publish),
        ):
            handlers = build_action_handlers(self.db, self.root, runner=lambda *_a, **_k: None)
        self.assertTrue({"render_and_qa", "build_publish_package"}.issubset(handlers))
        self.assertEqual(
            handlers["render_and_qa"](SimpleNamespace(task_id="digital", payload={}))["route"],
            "digital_human",
        )
        self.assertEqual(
            handlers["render_and_qa"](SimpleNamespace(task_id="ad", payload={}))["route"],
            "ad_mix",
        )
        self.assertEqual(
            handlers["build_publish_package"](SimpleNamespace(task_id="digital", payload={}))["route"],
            "digital_human",
        )
        self.assertEqual(
            handlers["build_publish_package"](SimpleNamespace(task_id="ad", payload={}))["route"],
            "ad_mix",
        )

    def test_both_routes_share_one_fail_closed_release_qa_contract(self):
        complete = {
            "issues": [
                {"check": check, "ok": True, "severity": "info"}
                for check in EXPECTED_QA_CHECKS
            ]
        }
        self.assertTrue(qa_evidence_complete(complete))
        complete["issues"] = [
            item for item in complete["issues"] if item["check"] != "material_authorization"
        ]
        self.assertFalse(qa_evidence_complete(complete))

    def test_park_route_remains_unavailable_at_both_write_boundaries(self):
        api = WorkbenchAPI(self.db, self.root)
        for path in ("/api/tasks/preflight", "/api/tasks"):
            with self.subTest(path=path), self.assertRaises(ApiError) as raised:
                api.dispatch("POST", path, {"route": "park_video"})
            self.assertEqual(raised.exception.status, 400)
            self.assertEqual(raised.exception.code, "ROUTE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
