import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from media_catalog.database import CatalogDatabase
from media_catalog.libraries import register_library
from media_catalog.rough_media import (
    RoughMediaError,
    assert_picture_media_available,
    decode_video_frame,
    describe_rough_media,
    resolve_rough_media,
)


class RoughMediaResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library_a = self.root / "library-a"
        self.library_b = self.root / "library-b"
        self.library_a.mkdir()
        self.library_b.mkdir()
        self.source_image = self.library_a / "image-a.jpg"
        self.source_video = self.library_a / "video-a.mp4"
        self.other_image = self.library_b / "image-b.jpg"
        self.source_image.write_bytes(b"image-a")
        self.source_video.write_bytes(b"0123456789")
        self.other_image.write_bytes(b"image-b")

        self.db = CatalogDatabase.open(self.root / "catalog.sqlite")
        self.db.migrate()
        register_library(
            self.db,
            "library-a",
            "任务 A 素材",
            "local",
            str(self.library_a),
            purpose="task_specific",
        )
        register_library(
            self.db,
            "library-b",
            "任务 B 素材",
            "local",
            str(self.library_b),
            purpose="task_specific",
        )
        self._seed_catalog()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed_catalog(self):
        assets = (
            ("image-a", "image", self.source_image, "library-a"),
            ("video-a", "video", self.source_video, "library-a"),
            ("image-b", "image", self.other_image, "library-b"),
        )
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT INTO tasks(task_id,title,status,brief_json) VALUES(?,?,?,?)",
                (
                    ("task-a", "任务 A", "draft", "{}"),
                    ("task-b", "任务 B", "draft", "{}"),
                ),
            )
            for asset_id, media_kind, path, library_id in assets:
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
                        f"fingerprint-{asset_id}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO asset_locations(
                      asset_id,library_id,relative_path,mtime_ns,file_identity,state
                    ) VALUES(?,?,?,?,?,'available')
                    """,
                    (
                        asset_id,
                        library_id,
                        path.name,
                        path.stat().st_mtime_ns,
                        f"identity-{asset_id}",
                    ),
                )
            connection.executemany(
                """
                INSERT INTO task_source_scopes(
                  scope_id,task_id,library_id,role,relative_path,
                  source_mode,active,priority
                ) VALUES(?,?,?,?,?,?,1,0)
                """,
                (
                    (
                        "scope-a",
                        "task-a",
                        "library-a",
                        "primary",
                        "",
                        "primary",
                    ),
                    (
                        "scope-b",
                        "task-b",
                        "library-b",
                        "primary",
                        "",
                        "primary",
                    ),
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
                        "图片镜头",
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
                        "视频镜头",
                        2.0,
                        4.0,
                        "video-a",
                        "broll全屏",
                        0.0,
                        2.0,
                        "mute",
                        "系统推荐",
                    ),
                    (
                        "task-b-image",
                        "task-b",
                        0,
                        "其他任务镜头",
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

    def _source_signature(self, asset_id, path):
        stat = path.stat()
        payload = json.dumps(
            [
                f"fingerprint-{asset_id}",
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ino,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _seed_video_proxy(self):
        cache = self.root / "cache"
        cache.mkdir()
        proxy = cache / "video-a.proxy.mp4"
        proxy.write_bytes(b"proxy-video")
        signature = self._source_signature("video-a", self.source_video)
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO preview_cache(asset_id,kind,rel_path,size_bytes)
                VALUES('video-a','proxy',?,?)
                """,
                (proxy.name, proxy.stat().st_size),
            )
            connection.execute(
                """
                INSERT INTO analysis_runs(
                  asset_id,stage,status,result_json,updated_at
                ) VALUES('video-a','preview','completed',?,datetime('now'))
                """,
                (
                    json.dumps(
                        {"source_signature": signature, "proxy": proxy.name},
                        ensure_ascii=False,
                    ),
                ),
            )
        return proxy

    def test_selected_scene_image_resolves_without_exposing_absolute_path(self):
        resolved = resolve_rough_media(self.db, "task-a", "image-a", "preview")

        self.assertEqual(resolved.media_kind, "image")
        self.assertEqual(resolved.delivery, "source")
        self.assertEqual(resolved.path.read_bytes(), b"image-a")
        descriptor = describe_rough_media(self.db, "task-a", "image-a")
        self.assertNotIn(str(self.library_a), str(descriptor))
        self.assertEqual(
            set(descriptor),
            {
                "media_kind",
                "thumbnail_url",
                "preview_url",
                "preview_delivery",
                "preview_available",
                "preview_problem",
                "preview_version",
            },
        )
        self.assertEqual(
            descriptor["preview_url"],
            "/api/tasks/task-a/rough-media/image-a/preview",
        )

    def test_task_cannot_resolve_another_tasks_asset(self):
        with self.assertRaisesRegex(RoughMediaError, "ROUGH_MEDIA_NOT_ALLOWED"):
            resolve_rough_media(self.db, "task-a", "image-b", "preview")

    def test_valid_cache_wins_and_stale_cache_falls_back_to_source(self):
        proxy = self._seed_video_proxy()

        cached = resolve_rough_media(self.db, "task-a", "video-a", "preview")
        self.assertEqual(cached.delivery, "cache")
        self.assertEqual(cached.path, proxy.resolve())

        self.source_video.write_bytes(b"changed-source")
        fallback = resolve_rough_media(self.db, "task-a", "video-a", "preview")
        self.assertEqual(fallback.delivery, "source")
        self.assertEqual(fallback.path, self.source_video.resolve())

    def test_offline_asset_blocks_picture_lock_with_scene_number(self):
        self.source_image.unlink()

        with self.assertRaisesRegex(
            RoughMediaError,
            "ROUGH_MEDIA_OFFLINE: 第 1 镜头",
        ):
            assert_picture_media_available(self.db, "task-a")

    def test_picture_lock_requires_the_versions_shown_and_decodable_media(self):
        image_version = describe_rough_media(self.db, "task-a", "image-a")["preview_version"]
        video_version = describe_rough_media(self.db, "task-a", "video-a")["preview_version"]
        expected = {
            "task-a-image": image_version,
            "task-a-video": video_version,
        }
        image_probe = Mock(return_value={"kind": "image", "width": 10, "height": 10})
        video_probe = Mock(return_value={"streams": [{"codec_type": "video", "codec_name": "h264"}]})
        video_decode = Mock(return_value=None)

        assert_picture_media_available(
            self.db,
            "task-a",
            expected,
            image_probe_fn=image_probe,
            video_probe_fn=video_probe,
            ffprobe_path=Path("ffprobe"),
            ffmpeg_path=Path("ffmpeg"),
            video_decode_fn=video_decode,
        )
        image_probe.assert_called_once()
        video_probe.assert_called_once()
        video_decode.assert_called_once_with(
            self.source_video.resolve(), Path("ffmpeg"), 0.0
        )

        with self.assertRaisesRegex(RoughMediaError, "预览版本已变化"):
            assert_picture_media_available(
                self.db,
                "task-a",
                {**expected, "task-a-image": "stale"},
                image_probe_fn=image_probe,
                video_probe_fn=video_probe,
                ffprobe_path=Path("ffprobe"),
                ffmpeg_path=Path("ffmpeg"),
                video_decode_fn=video_decode,
            )

        bad_video_probe = Mock(return_value={"streams": [{"codec_type": "video", "codec_name": "hevc"}]})
        with self.assertRaisesRegex(RoughMediaError, "浏览器不支持") as caught:
            assert_picture_media_available(
                self.db,
                "task-a",
                expected,
                image_probe_fn=Mock(return_value={"kind": "image"}),
                video_probe_fn=bad_video_probe,
                ffprobe_path=Path("ffprobe"),
                ffmpeg_path=Path("ffmpeg"),
                video_decode_fn=video_decode,
            )
        self.assertEqual(caught.exception.details["assetId"], "video-a")
        self.assertEqual(caught.exception.details["cardId"], "task-a-video")
        self.assertEqual(caught.exception.details["position"], 2)

        with self.assertRaisesRegex(RoughMediaError, "无法解码"):
            assert_picture_media_available(
                self.db,
                "task-a",
                expected,
                image_probe_fn=Mock(return_value={"kind": "image"}),
                video_probe_fn=video_probe,
                ffprobe_path=Path("ffprobe"),
                ffmpeg_path=Path("ffmpeg"),
                video_decode_fn=Mock(side_effect=RuntimeError("damaged frame")),
            )

        with self.assertRaisesRegex(RoughMediaError, "无法解码"):
            assert_picture_media_available(
                self.db,
                "task-a",
                expected,
                image_probe_fn=Mock(return_value={"kind": "image"}),
                video_probe_fn=video_probe,
                ffprobe_path=Path("ffprobe"),
                ffmpeg_path=Path("ffmpeg"),
                video_decode_fn=Mock(
                    side_effect=subprocess.TimeoutExpired("ffmpeg", 60)
                ),
            )

    def test_location_escape_is_rejected_even_for_a_selected_scene(self):
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE asset_locations SET relative_path='../outside.jpg' "
                "WHERE asset_id='image-a'"
            )

        with self.assertRaisesRegex(RoughMediaError, "ROUGH_MEDIA_NOT_ALLOWED"):
            resolve_rough_media(self.db, "task-a", "image-a", "preview")

    def test_video_frame_decode_requires_an_actual_output_frame(self):
        calls = []

        def no_frame(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with self.assertRaisesRegex(RuntimeError, "没有解码出目标视频帧"):
            decode_video_frame(
                self.source_video,
                Path("ffmpeg"),
                1.25,
                runner=no_frame,
            )
        command, kwargs = calls[0]
        self.assertIn("1.250000", command)
        self.assertIn("image2pipe", command)
        self.assertFalse(kwargs["text"])


if __name__ == "__main__":
    unittest.main()
