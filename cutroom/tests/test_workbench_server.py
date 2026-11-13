import http.client
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch

from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.packaging_preview import (
    PreviewIdentity,
    create_preview_artifact,
    finalize_preview_artifact,
)
from media_catalog.snapshots import record_render_review_candidate
from media_catalog.tasks import create_task
from media_catalog.workbench_worker import claim_next_heavy_job, enqueue_job


class FakePicker:
    def __init__(self, *, video=None, directory=None, image=None):
        self.video = video
        self.directory = directory
        self.image = image

    def pick_video(self):
        return self.video

    def pick_directory(self):
        return self.directory

    def pick_image(self):
        return self.image


class WorkbenchServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.static = self.root / "public"
        self.static.mkdir()
        (self.static / "index.html").write_text("<h1>workbench</h1>", encoding="utf-8")
        (self.static / "app.js").write_text("console.log('ok')", encoding="utf-8")
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"not-a-real-video")
        self.image = self.root / "cover.png"
        self.image.write_bytes(b"not-a-real-image")
        self.folder = self.root / "park-materials"
        self.folder.mkdir()
        self.db_path = self.root / "catalog.sqlite"
        self.server = None
        self.thread = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.tmp.cleanup()

    def start_server(self, **overrides):
        from workbench.server import create_server

        options = {
            "db_path": self.db_path,
            "port": 0,
            "static_root": self.static,
            "project_root": self.root,
            "picker": FakePicker(
                video=str(self.video), directory=str(self.folder), image=str(self.image)
            ),
            "startup_key": "one-time-startup-key",
            "worker_handlers": {},
        }
        options.update(overrides)
        self.server = create_server(**options)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    @unittest.skipUnless(os.name == "nt", "Windows socket semantics only")
    def test_second_server_cannot_share_the_bound_workbench_port(self):
        from workbench.server import create_server

        first = self.start_server()
        duplicate = None
        try:
            with self.assertRaises(OSError):
                duplicate = create_server(
                    db_path=self.root / "duplicate.sqlite",
                    port=first.server_address[1],
                    static_root=self.static,
                    project_root=self.root,
                    picker=FakePicker(),
                    startup_key="duplicate-startup-key",
                    worker_handlers={},
                )
        finally:
            if duplicate is not None:
                duplicate.server_close()

    def request(self, method, path, *, headers=None, body=None):
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def bootstrap_cookie(self):
        status, headers, _ = self.request(
            "GET", "/auth/bootstrap?key=one-time-startup-key"
        )
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        return cookie.split(";", 1)[0]

    def json_request(self, path, *, cookie=None, origin=None, payload=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        if cookie is not None:
            request_headers["Cookie"] = cookie
        if origin is not None:
            request_headers["Origin"] = origin
        if headers:
            request_headers.update(headers)
        body = json.dumps({} if payload is None else payload).encode("utf-8")
        return self.request("POST", path, headers=request_headers, body=body)

    def seed_review_candidate(self, task_id="review-task"):
        review_video = self.root / "review.mp4"
        review_video.write_bytes(b"0123456789")
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            create_task(db, task_id, "待审候选", "digital_human")
            qa = {
                "report_id": "qa-review",
                "task_id": task_id,
                "video_path": str(review_video.resolve()),
                "status": "passed",
                "blocking_count": 0,
                "video_spec": {"duration": 10.0},
                "issues": [],
            }
            db.connection.execute(
                "INSERT INTO qa_reports("
                "report_id,task_id,video_path,status,issue_count,blocking_count,report_json"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    qa["report_id"],
                    task_id,
                    qa["video_path"],
                    "passed",
                    0,
                    0,
                    json.dumps(qa),
                ),
            )
            db.connection.commit()
            record_render_review_candidate(
                db,
                task_id,
                {
                    "output_path": str(review_video.resolve()),
                    "dependency_digest": "digest-review",
                    "dependency_snapshot": {"files": []},
                    "plan_path": str(self.root / "review.plan.json"),
                    "ass_path": None,
                    "packaging": {"plan_id": "review-plan"},
                },
                qa,
            )
        return review_video

    def seed_packaging_preview(self, task_id="packaging-task"):
        media = (
            self.root / "runtime" / "workbench" / "cache"
            / "packaging-previews" / task_id / "preview.mp4"
        )
        media.parent.mkdir(parents=True)
        media.write_bytes(b"0123456789")
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            create_task(db, task_id, "包装预览", "digital_human")
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO packaging_plans(plan_id,task_id,route,plan_name,status) "
                    "VALUES('plan-preview',?,'digital_human','预览方案','draft')",
                    (task_id,),
                )
            preview = create_preview_artifact(db, PreviewIdentity(
                task_id=task_id,
                plan_id="plan-preview",
                picture_lock_digest="picture-a",
                plan_digest="plan-a",
                renderer_version="packaging-preview-v1",
            ))
            ready = finalize_preview_artifact(
                db,
                preview["preview_id"],
                project_root=self.root,
                rel_path=media.relative_to(self.root).as_posix(),
                duration_seconds=2.0,
                coverage=[],
            )
        return media, ready

    def seed_rough_media_task(self):
        self.rough_library_a = self.root / "rough-library-a"
        self.rough_library_b = self.root / "rough-library-b"
        self.rough_library_a.mkdir()
        self.rough_library_b.mkdir()
        self.rough_image = self.rough_library_a / "image-a.jpg"
        self.rough_video = self.rough_library_a / "video-a.mp4"
        self.rough_other = self.rough_library_b / "image-b.jpg"
        self.image_bytes = b"jpeg-image-a"
        self.rough_image.write_bytes(self.image_bytes)
        self.rough_video.write_bytes(b"0123456789")
        self.rough_other.write_bytes(b"private-image-b")
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            register_library(
                db,
                "rough-a",
                "任务 A 素材",
                "local",
                str(self.rough_library_a),
                purpose="task_specific",
            )
            register_library(
                db,
                "rough-b",
                "任务 B 素材",
                "local",
                str(self.rough_library_b),
                purpose="task_specific",
            )
            create_task(db, "task-a", "任务 A", "digital_human")
            create_task(db, "task-b", "任务 B", "digital_human")
            with db.transaction() as connection:
                for asset_id, media_kind, library_id, path in (
                    ("image-a", "image", "rough-a", self.rough_image),
                    ("video-a", "video", "rough-a", self.rough_video),
                    ("image-b", "image", "rough-b", self.rough_other),
                ):
                    connection.execute(
                        """
                        INSERT INTO assets(
                          asset_id,media_kind,size_bytes,quick_fingerprint,created_at
                        ) VALUES(?,?,?,?,datetime('now'))
                        """,
                        (
                            asset_id,
                            media_kind,
                            path.stat().st_size,
                            f"fp-{asset_id}",
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO asset_locations(
                          asset_id,library_id,relative_path,mtime_ns,state
                        ) VALUES(?,?,?,?, 'available')
                        """,
                        (
                            asset_id,
                            library_id,
                            path.name,
                            path.stat().st_mtime_ns,
                        ),
                    )
                connection.executemany(
                    """
                    INSERT INTO scene_cards(
                      card_id,task_id,idx,narrative_text,narrative_start,narrative_end,
                      asset_id,mix_form,enter_tc,exit_tc,audio_policy_draft,source_type
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        (
                            "task-a-image",
                            "task-a",
                            0,
                            "图片",
                            0.0,
                            2.0,
                            "image-a",
                            "信息卡",
                            0.0,
                            0.0,
                            "mute",
                            "用户指定",
                        ),
                        (
                            "task-a-video",
                            "task-a",
                            1,
                            "视频",
                            2.0,
                            4.0,
                            "video-a",
                            "broll全屏",
                            0.0,
                            2.0,
                            "mute",
                            "用户指定",
                        ),
                        (
                            "task-b-image",
                            "task-b",
                            0,
                            "私有图片",
                            0.0,
                            2.0,
                            "image-b",
                            "信息卡",
                            0.0,
                            0.0,
                            "mute",
                            "用户指定",
                        ),
                    ),
                )

    def test_packaged_user_manual_is_served_without_sign_in(self):
        project_root = Path(__file__).resolve().parents[1]
        self.start_server(static_root=project_root / "web-ui" / "public")

        status, headers, body = self.request("GET", "/help.html")

        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        page = body.decode("utf-8")
        self.assertIn("<title>剪辑工作台 使用手册</title>", page)
        self.assertIn('data-manual-section="model-settings"', page)
        self.assertIn('data-manual-section="packaging-assets"', page)
        self.assertIn('data-manual-section="video-workflows"', page)

    def test_binds_only_to_ipv4_loopback(self):
        server = self.start_server()
        self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_startup_key_exchanges_for_hardened_cookie_and_cleans_url(self):
        self.start_server()
        status, headers, body = self.request(
            "GET", "/auth/bootstrap?key=one-time-startup-key"
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("Path=/", headers["Set-Cookie"])
        self.assertEqual(body, b"")

        status, headers, body = self.request("GET", "/auth/bootstrap?key=wrong")
        self.assertEqual(status, 403)
        self.assert_error(body, "AUTH_INVALID")
        self.assertNotIn("Set-Cookie", headers)

    def test_bootstrap_cookie_is_persistent_for_one_year(self):
        self.start_server()
        status, headers, _ = self.request(
            "GET", "/auth/bootstrap?key=one-time-startup-key"
        )
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=31536000", headers["Set-Cookie"])

    def test_persistent_cookie_remains_valid_after_server_restart(self):
        first = self.start_server()
        cookie = self.bootstrap_cookie()
        first.shutdown()
        first.server_close()
        self.thread.join(timeout=5)
        self.server = None
        self.thread = None

        self.start_server(startup_key="second-key")
        status, _, body = self.request(
            "GET", "/api/service/status", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200, body.decode("utf-8"))

    def test_persistent_cookie_expires_at_its_signed_deadline(self):
        from workbench.security import HttpError, SESSION_COOKIE, SessionSecurity

        now = [1_000.0]
        security = SessionSecurity(
            "startup-key",
            session_secret=b"x" * 32,
            session_ttl_seconds=60,
            clock=lambda: now[0],
        )
        session = security.exchange("startup-key")
        security.require_session({"Cookie": f"{SESSION_COOKIE}={session}"})

        now[0] = 1_061.0
        with self.assertRaises(HttpError) as raised:
            security.require_session({"Cookie": f"{SESSION_COOKIE}={session}"})
        self.assertEqual(raised.exception.code, "AUTH_REQUIRED")

    def test_malformed_non_ascii_cookie_fails_as_auth_error(self):
        from workbench.security import HttpError, SESSION_COOKIE, SessionSecurity

        security = SessionSecurity("startup-key", session_secret=b"x" * 32)
        with self.assertRaises(HttpError) as raised:
            security.require_session(
                {"Cookie": f"{SESSION_COOKIE}=v1.9999999999.noncé.signature"}
            )
        self.assertEqual(raised.exception.code, "AUTH_REQUIRED")

    def test_write_requires_session_exact_origin_and_json(self):
        server = self.start_server()
        origin = f"http://127.0.0.1:{server.server_address[1]}"

        status, _, body = self.json_request(
            "/api/picker/video", origin=origin
        )
        self.assertEqual(status, 403)
        self.assert_error(body, "AUTH_REQUIRED")

        cookie = self.bootstrap_cookie()
        status, _, body = self.json_request(
            "/api/picker/video", cookie=cookie, origin="http://evil.invalid"
        )
        self.assertEqual(status, 403)
        self.assert_error(body, "ORIGIN_REJECTED")

        status, _, body = self.request(
            "POST",
            "/api/picker/video",
            headers={"Cookie": cookie, "Origin": origin, "Content-Type": "text/plain"},
            body=b"{}",
        )
        self.assertEqual(status, 415)
        self.assert_error(body, "JSON_REQUIRED")

        status, _, body = self.json_request(
            "/api/picker/video", cookie=cookie, origin=origin
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"path": str(self.video.resolve())})

        status, _, body = self.json_request(
            "/api/picker/image", cookie=cookie, origin=origin
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"path": str(self.image.resolve())})

        status, _, body = self.json_request(
            "/api/not-a-real-write", origin=origin
        )
        self.assertEqual(status, 403)
        self.assert_error(body, "AUTH_REQUIRED")

    def test_json_body_limit_is_checked_before_reading(self):
        server = self.start_server(max_json_bytes=32)
        cookie = self.bootstrap_cookie()
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        status, _, body = self.request(
            "POST",
            "/api/picker/video",
            headers={
                "Cookie": cookie,
                "Origin": origin,
                "Content-Type": "application/json",
                "Content-Length": "33",
            },
            body=b"",
        )
        self.assertEqual(status, 413)
        self.assert_error(body, "BODY_TOO_LARGE")

    def test_rejected_small_post_with_delayed_body_returns_original_http_error(self):
        self.start_server()
        cookie = self.bootstrap_cookie()
        cases = [
            ({"Origin": self.server.runtime.origin}, 403, "AUTH_REQUIRED"),
            ({"Cookie": cookie, "Origin": "http://evil.invalid"}, 403, "ORIGIN_REJECTED"),
            ({"Cookie": cookie, "Origin": self.server.runtime.origin,
              "Content-Type": "text/plain"}, 415, "JSON_REQUIRED"),
        ]
        for extra, expected_status, expected_code in cases:
            with self.subTest(code=expected_code):
                connection = http.client.HTTPConnection(*self.server.server_address[:2], timeout=2)
                try:
                    connection.putrequest("POST", "/api/picker/video")
                    headers = {"Content-Type": "application/json", "Content-Length": "2", **extra}
                    for key, value in headers.items():
                        connection.putheader(key, value)
                    connection.endheaders()
                    time.sleep(0.02)
                    connection.send(b"{}")
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected_status)
                    self.assert_error(response.read(), expected_code)
                finally:
                    connection.close()

    def test_rejected_missing_or_unsafe_body_is_bounded_and_never_read_when_unsafe(self):
        from workbench.routes import WorkbenchRouter

        self.start_server(max_json_bytes=32)
        cookie = self.bootstrap_cookie()
        cases = [
            ("2", {}, "http://evil.invalid", 403, "ORIGIN_REJECTED"),
            ("33", {}, self.server.runtime.origin, 413, "BODY_TOO_LARGE"),
            ("100000000", {}, "http://evil.invalid", 403, "ORIGIN_REJECTED"),
            (None, {}, self.server.runtime.origin, 411, "BODY_LENGTH_REQUIRED"),
            ("wrong", {}, self.server.runtime.origin, 400, "BODY_LENGTH_INVALID"),
            ("-1", {}, self.server.runtime.origin, 400, "BODY_LENGTH_INVALID"),
            ("2", {"Transfer-Encoding": "chunked"}, self.server.runtime.origin, 400, "BODY_LENGTH_REQUIRED"),
        ]
        native_dispatch = WorkbenchRouter.dispatch
        reads = []

        class TrackedBody:
            def __init__(self, stream):
                self.stream = stream
            def read(self, size=-1):
                reads.append(size)
                return self.stream.read(size)
            def read1(self, size=-1):
                reads.append(size)
                return self.stream.read1(size)
            def __getattr__(self, key):
                return getattr(self.stream, key)

        def dispatch(router, handler):
            handler.rfile = TrackedBody(handler.rfile)
            return native_dispatch(router, handler)

        with patch.object(WorkbenchRouter, "dispatch", dispatch):
            for length, extra, origin, status, code in cases:
                with self.subTest(length=length, code=code):
                    reads.clear()
                    connection = http.client.HTTPConnection(*self.server.server_address[:2], timeout=1)
                    try:
                        connection.putrequest("POST", "/api/picker/video")
                        headers = {"Cookie": cookie, "Origin": origin, "Content-Type": "application/json", **extra}
                        if length is not None:
                            headers["Content-Length"] = length
                        for key, value in headers.items():
                            connection.putheader(key, value)
                        started = time.monotonic()
                        connection.endheaders()
                        response = connection.getresponse()
                        self.assertEqual(response.status, status)
                        self.assert_error(response.read(), code)
                        self.assertLess(time.monotonic() - started, 0.6)
                        if length != "2" or extra:
                            self.assertEqual(reads, [], "unsafe framing/oversized bodies must never be read")
                    finally:
                        connection.close()

    def test_rejected_slow_body_has_one_total_deadline(self):
        from workbench.server import _RequestHandler

        self.start_server()
        cookie = self.bootstrap_cookie()
        stopped = threading.Event()
        sent = []
        native_response = _RequestHandler.send_response

        def response_started(handler, code, message=None):
            # Stop further attack bytes once the server has decided to respond;
            # this only observes the decision and avoids a second reset race.
            if code == 403:
                stopped.set()
            return native_response(handler, code, message)

        connection = http.client.HTTPConnection(*self.server.server_address[:2], timeout=2)
        writer = None
        try:
            with patch.object(_RequestHandler, "send_response", response_started):
                connection.putrequest("POST", "/api/picker/video")
                for key, value in {"Cookie": cookie, "Origin": "http://evil.invalid",
                                   "Content-Type": "application/json", "Content-Length": "20"}.items():
                    connection.putheader(key, value)
                started = time.monotonic()
                connection.endheaders()

                def drip():
                    for _ in range(20):
                        if stopped.wait(0.04):
                            return
                        try:
                            connection.send(b"x")
                            sent.append(1)
                        except OSError:
                            return

                writer = threading.Thread(target=drip, daemon=True)
                writer.start()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assert_error(response.read(), "ORIGIN_REJECTED")
                self.assertLess(time.monotonic() - started, 0.6)
                self.assertLess(len(sent), 20, "continuous bytes must not extend the total drain deadline")
        finally:
            stopped.set()
            if writer is not None:
                writer.join(timeout=1)
            connection.close()

    def test_rejected_large_body_and_consumed_json_are_not_drained(self):
        from workbench.routes import WorkbenchRouter

        self.start_server()
        cookie = self.bootstrap_cookie()
        native_dispatch = WorkbenchRouter.dispatch
        drain_reads = []

        class Body:
            def __init__(self, stream):
                self.stream = stream
            def read1(self, size=-1):
                drain_reads.append(size)
                return self.stream.read1(size)
            def __getattr__(self, key):
                return getattr(self.stream, key)

        def dispatch(router, handler):
            handler.rfile = Body(handler.rfile)
            return native_dispatch(router, handler)

        with patch.object(WorkbenchRouter, "dispatch", dispatch):
            for payload, code in ((b"[]", "JSON_OBJECT_REQUIRED"), (b"bad", "JSON_INVALID")):
                status, _, body = self.request("POST", "/api/picker/video", body=payload, headers={
                    "Cookie": cookie, "Origin": self.server.runtime.origin,
                    "Content-Type": "application/json",
                })
                self.assertEqual(status, 400)
                self.assert_error(body, code)
            started = time.monotonic()
            status, _, body = self.request("POST", "/api/picker/video", body=b"", headers={
                "Cookie": cookie, "Origin": "http://evil.invalid",
                "Content-Type": "application/json", "Content-Length": "16385",
            })
            self.assertEqual(status, 403)
            self.assert_error(body, "ORIGIN_REJECTED")
            self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(drain_reads, [])

    def test_static_traversal_arbitrary_download_and_link_escape_are_rejected(self):
        self.start_server()
        secret = self.root / "secret.txt"
        secret.write_text("private", encoding="utf-8")

        status, _, body = self.request("GET", "/..%2Fsecret.txt")
        self.assertEqual(status, 403)
        self.assert_error(body, "PATH_REJECTED")

        status, _, body = self.request(
            "GET",
            "/api/download?path=" + quote(str(secret).replace("\\", "/"), safe="/:"),
            headers={"Cookie": self.bootstrap_cookie()},
        )
        self.assertEqual(status, 404)
        self.assert_error(body, "NOT_FOUND")

        link = self.static / "outside.txt"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            link = None
        if link is not None:
            status, _, body = self.request("GET", "/outside.txt")
            self.assertEqual(status, 403)
            self.assert_error(body, "PATH_REJECTED")

    def test_voice_preview_serves_only_authenticated_hashed_mp3_paths(self):
        profile_hash = "a" * 64
        voice_hash = "b" * 64
        preview = (
            self.root
            / "runtime"
            / "workbench"
            / "voice-previews"
            / profile_hash
            / f"{voice_hash}.mp3"
        )
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"ID3voice-preview")
        self.start_server()
        path = f"/api/voice-previews/{profile_hash}/{voice_hash}.mp3"

        status, _, body = self.request("GET", path)
        self.assertEqual(status, 403)
        self.assert_error(body, "AUTH_REQUIRED")

        cookie = self.bootstrap_cookie()
        status, headers, body = self.request(
            "GET", path, headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/mpeg")
        self.assertEqual(body, b"ID3voice-preview")

        status, _, body = self.request(
            "GET",
            f"/api/voice-previews/{profile_hash}/../secret.mp3",
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 404)
        self.assertNotIn(b"ID3voice-preview", body)

    def test_review_media_streams_only_the_authenticated_current_candidate_with_ranges(self):
        review_video = self.seed_review_candidate()
        self.start_server()
        path = "/api/tasks/review-task/review-media"

        status, _, body = self.request("GET", path)
        self.assertEqual(status, 403)
        self.assert_error(body, "AUTH_REQUIRED")

        cookie = self.bootstrap_cookie()
        status, headers, body = self.request(
            "GET",
            path,
            headers={"Cookie": cookie, "Range": "bytes=2-5"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(headers["Content-Length"], "4")
        self.assertEqual(body, b"2345")

        review_video.write_bytes(b"changed")
        status, _, body = self.request("GET", path, headers={"Cookie": cookie})
        self.assertEqual(status, 409)
        self.assert_error(body, "CANDIDATE_VIDEO_CHANGED")

    def test_packaging_preview_media_requires_session_ownership_and_valid_signature(self):
        media, preview = self.seed_packaging_preview()
        self.start_server()
        path = (
            f"/api/tasks/packaging-task/packaging-previews/"
            f"{preview['preview_id']}/media"
        )
        status, _, body = self.request("GET", path)
        self.assertEqual(status, 403)
        self.assert_error(body, "AUTH_REQUIRED")

        cookie = self.bootstrap_cookie()
        status, headers, body = self.request(
            "GET", path, headers={"Cookie": cookie, "Range": "bytes=2-5"}
        )
        self.assertEqual((status, body), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")

        wrong = path.replace("packaging-task", "other-task")
        status, _, body = self.request("GET", wrong, headers={"Cookie": cookie})
        self.assertEqual(status, 404)
        self.assert_error(body, "PACKAGING_PREVIEW_NOT_FOUND")

        media.write_bytes(b"tampered")
        status, _, body = self.request("GET", path, headers={"Cookie": cookie})
        self.assertEqual(status, 409)
        self.assert_error(body, "PACKAGING_PREVIEW_MEDIA_CHANGED")

    def test_rough_media_requires_session_and_streams_task_image(self):
        self.seed_rough_media_task()
        self.start_server()
        path = "/api/tasks/task-a/rough-media/image-a/preview"

        status, _, body = self.request("GET", path)
        self.assertEqual(status, 403)
        self.assert_error(body, "AUTH_REQUIRED")

        cookie = self.bootstrap_cookie()
        status, headers, body = self.request(
            "GET",
            path,
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(body, self.image_bytes)

    def test_rough_video_supports_head_and_byte_ranges(self):
        self.seed_rough_media_task()
        self.start_server()
        cookie = self.bootstrap_cookie()
        path = "/api/tasks/task-a/rough-media/video-a/preview"

        status, headers, body = self.request(
            "GET",
            path,
            headers={"Cookie": cookie, "Range": "bytes=2-5"},
        )
        self.assertEqual((status, body), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        status, headers, body = self.request(
            "HEAD",
            path,
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], "10")
        self.assertEqual(body, b"")

        status, headers, body = self.request(
            "GET",
            path,
            headers={"Cookie": cookie, "Range": "bytes=99-100"},
        )
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */10")
        self.assertEqual(body, b"")

        status, _, body = self.request("HEAD", path)
        self.assertEqual(status, 403)
        self.assertEqual(body, b"")

    def test_rough_media_hides_assets_outside_the_task(self):
        self.seed_rough_media_task()
        self.start_server()
        cookie = self.bootstrap_cookie()

        status, _, body = self.request(
            "GET",
            "/api/tasks/task-a/rough-media/image-b/preview",
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 404)
        self.assert_error(body, "ROUGH_MEDIA_NOT_ALLOWED")
        self.assertNotIn(str(self.rough_library_b).encode(), body)

    def test_rough_media_rejects_a_file_suffix_that_disagrees_with_catalog_kind(self):
        self.seed_rough_media_task()
        disguised = self.rough_library_a / "image-a.exe"
        disguised.write_bytes(b"not-an-image")
        with CatalogDatabase.open(self.db_path) as db:
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE asset_locations SET relative_path=? WHERE asset_id='image-a'",
                    (disguised.name,),
                )
        self.start_server()
        cookie = self.bootstrap_cookie()

        status, _, body = self.request(
            "GET",
            "/api/tasks/task-a/rough-media/image-a/preview",
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 415)
        self.assert_error(body, "ROUGH_MEDIA_UNSUPPORTED")

    def test_client_disconnect_during_stream_does_not_escape_router(self):
        server = self.start_server()

        class DisconnectingWriter:
            def write(self, _body):
                raise ConnectionResetError("browser closed the response")

        class Handler:
            path = "/"
            command = "GET"
            headers = {}
            wfile = DisconnectingWriter()

            def send_response(self, _status):
                pass

            def send_header(self, _name, _value):
                pass

            def end_headers(self):
                pass

        server.router.dispatch(Handler())

    def test_picker_is_injectable_and_server_revalidates_returned_path(self):
        missing = self.root / "missing.mp4"
        self.start_server(picker=FakePicker(
            video=str(missing), directory=str(self.video), image=str(self.video)
        ))
        cookie = self.bootstrap_cookie()
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"

        status, _, body = self.json_request(
            "/api/picker/video", cookie=cookie, origin=origin
        )
        self.assertEqual(status, 400)
        self.assert_error(body, "PICKER_RESULT_INVALID")

        status, _, body = self.json_request(
            "/api/picker/image", cookie=cookie, origin=origin
        )
        self.assertEqual(status, 400)
        self.assert_error(body, "PICKER_RESULT_INVALID")

        status, _, body = self.json_request(
            "/api/picker/directory", cookie=cookie, origin=origin
        )
        self.assertEqual(status, 400)
        self.assert_error(body, "PICKER_RESULT_INVALID")

    def test_restart_reads_tasks_from_sqlite_instead_of_process_memory(self):
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            create_task(db, "persisted-task", "重启后仍可见", "digital_human")

        first = self.start_server()
        cookie = self.bootstrap_cookie()
        status, _, body = self.request(
            "GET", "/api/service/status", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["taskCount"], 1)
        first.shutdown()
        first.server_close()
        self.thread.join(timeout=5)
        self.server = None
        self.thread = None

        second = self.start_server(startup_key="second-key")
        status, headers, _ = self.request("GET", "/auth/bootstrap?key=second-key")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request(
            "GET", "/api/service/status", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["taskCount"], 1)
        self.assertTrue(second.runtime.worker_thread.is_alive())

    def test_startup_recovers_only_expired_persistent_job_leases(self):
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            enqueue_job(db, job_id="abandoned", job_type="render", payload={})
            claim_next_heavy_job(db, worker_id="dead-worker", lease_seconds=30)
            db.connection.execute(
                "UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00Z' "
                "WHERE job_id='abandoned'"
            )
            db.connection.commit()

        self.start_server()
        with CatalogDatabase.open(self.db_path) as db:
            row = db.connection.execute(
                "SELECT status, cursor FROM jobs WHERE job_id='abandoned'"
            ).fetchone()
            self.assertEqual(row["status"], "interrupted")
            self.assertIsNone(row["cursor"])

    def test_one_background_worker_processes_persisted_jobs_without_browser(self):
        handled = threading.Event()
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            enqueue_job(
                db,
                job_id="queued-render",
                job_type="render",
                payload={"task_id": "offline-browser"},
            )

        self.start_server(worker_handlers={"render": lambda _job: handled.set()})
        self.assertTrue(handled.wait(timeout=5))
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            with CatalogDatabase.open(self.db_path) as db:
                status = db.connection.execute(
                    "SELECT status FROM jobs WHERE job_id='queued-render'"
                ).fetchone()[0]
            if status == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(status, "completed")

    def test_default_server_registers_the_trusted_digital_human_handlers(self):
        server = self.start_server(worker_handlers=None)
        self.assertEqual(
            set(server.runtime.worker_handlers),
            {
                "scan",
                "generate_preview",
                "transcribe",
                "sync_sources",
                "recommend_storyboard",
                "packaging_preview",
                "packaging_catalog_check",
                "render_and_qa",
                "build_publish_package",
            },
        )

    def test_default_server_processes_library_scan_jobs(self):
        (self.folder / "factory.mp4").write_bytes(b"factory-footage")
        with CatalogDatabase.open(self.db_path) as db:
            db.migrate()
            register_library(
                db,
                "park",
                "园区实拍",
                "local",
                str(self.folder),
                purpose="task_specific",
            )
            enqueue_job(
                db,
                job_id="scan-park",
                job_type="scan",
                library_id="park",
                payload={
                    "action": "scan",
                    "library_id": "park",
                    "catalog_only": True,
                },
            )

        self.start_server(worker_handlers=None)
        deadline = time.monotonic() + 5
        status = None
        asset_count = 0
        while time.monotonic() < deadline:
            with CatalogDatabase.open(self.db_path) as db:
                status = db.connection.execute(
                    "SELECT status FROM jobs WHERE job_id='scan-park'"
                ).fetchone()[0]
                asset_count = db.connection.execute(
                    "SELECT count(*) FROM asset_locations WHERE library_id='park'"
                ).fetchone()[0]
            if status in {"completed", "failed"}:
                break
            time.sleep(0.02)

        self.assertEqual(status, "completed")
        self.assertEqual(asset_count, 1)

    def test_legacy_launchers_delegate_to_python_persistent_service(self):
        project_root = Path(__file__).resolve().parents[1]
        batch = (project_root / "启动出片工具.bat").read_text(
            encoding="utf-8-sig"
        )
        node_launcher = (project_root / "web-ui" / "server.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('cd /d "%~dp0"', batch)
        self.assertNotIn('cd /d "%~dp0.."', batch)
        self.assertIn("-m workbench.server", batch)
        self.assertIn('"-m", "workbench.server"', node_launcher)
        self.assertNotIn("new Map", node_launcher)

    @unittest.skipUnless(os.name == "nt", "Windows Task Scheduler only")
    def test_autostart_installer_describes_correct_cutroom_service(self):
        project_root = Path(__file__).resolve().parents[1]
        powershell = (
            Path(os.environ.get("WINDIR", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        result = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(project_root / "workbench" / "install-autostart.ps1"),
                "-Describe",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            encoding="utf-8",
        )
        config = json.loads(result.stdout)
        self.assertEqual(config["taskName"], "CutroomLocal")
        self.assertEqual(config["fixedUrl"], "http://127.0.0.1:8765/")
        self.assertEqual(config["module"], "workbench.server")
        self.assertEqual(Path(config["projectRoot"]), project_root)
        self.assertEqual(
            Path(config["childExecutable"]),
            project_root / ".venv" / "Scripts" / "python.exe",
        )
        self.assertEqual(
            Path(config["supervisorScript"]),
            project_root / "workbench" / "start-local.ps1",
        )
        self.assertEqual(Path(config["taskExecutable"]), powershell)

    @unittest.skipUnless(os.name == "nt", "Windows supervisor only")
    def test_local_supervisor_never_opens_the_browser_automatically(self):
        project_root = Path(__file__).resolve().parents[1]
        powershell = (
            Path(os.environ.get("WINDIR", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        result = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(project_root / "workbench" / "start-local.ps1"),
                "-Describe",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            encoding="utf-8",
        )
        config = json.loads(result.stdout)
        self.assertIn("--no-open", config.get("childArguments", ""))

    def test_error_shape_is_stable_and_does_not_leak_exception_details(self):
        class ExplodingPicker(FakePicker):
            def pick_video(self):
                raise RuntimeError("C:/secret/provider-key.txt")

        server = self.start_server(picker=ExplodingPicker())
        cookie = self.bootstrap_cookie()
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        status, headers, body = self.json_request(
            "/api/picker/video", cookie=cookie, origin=origin
        )
        self.assertEqual(status, 500)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        parsed = json.loads(body)
        self.assertEqual(set(parsed), {"error"})
        self.assertEqual(set(parsed["error"]), {"code", "message", "diagnosticId"})
        self.assertEqual(parsed["error"]["code"], "INTERNAL_ERROR")
        self.assertRegex(parsed["error"]["diagnosticId"], r"^diag-[a-f0-9]{16}$")
        self.assertNotIn("secret", parsed["error"]["message"].lower())

    def assert_error(self, body, code):
        parsed = json.loads(body)
        self.assertEqual(parsed["error"]["code"], code)
        self.assertIsInstance(parsed["error"]["message"], str)


if __name__ == "__main__":
    unittest.main()
