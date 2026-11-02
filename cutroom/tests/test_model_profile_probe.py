import json
import base64
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from media_catalog.database import CatalogDatabase
from media_catalog.model_profile_probe import (
    create_tts_voice_preview,
    ModelProfileProbeError,
    list_provider_models,
    probe_model_profile,
)
from media_catalog.model_profiles import create_model_profile, create_provider_account
from media_catalog.tts_voice_catalog import (
    get_provider_voice,
    replace_provider_voices,
)


class ProbeHandler(BaseHTTPRequestHandler):
    requests = []
    models = ["deepseek-v4", "doubao-seedance-2-0-260128"]
    force_status = None

    def log_message(self, _format, *_args):
        return

    def _record(self, body=b""):
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "x_api_key": self.headers.get("X-Api-Key"),
                "x_api_resource_id": self.headers.get("X-Api-Resource-Id"),
                "x_api_request_id": self.headers.get("X-Api-Request-Id"),
                "body": body,
            }
        )

    def do_GET(self):
        self._record()
        if self.force_status is not None:
            self.send_error(self.force_status)
            return
        payload = {"data": [{"id": item} for item in self.models]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        if self.force_status is not None:
            self.send_error(self.force_status)
            return
        if self.path == "/api/v3/tts/unidirectional/sse":
            data = (
                'data: {"code":0,"data":"SUQz"}\n\n'
                'data: {"code":0,"data":"dGlueS1hdWRpbw=="}\n\n'
                'data: {"code":20000000,"message":"done"}\n\n'
            ).encode()
            content_type = "text/event-stream"
        elif self.path == "/api/v1/tts":
            data = json.dumps({
                "code": 3000,
                "data": base64.b64encode(b"ID3tiny-audio").decode("ascii"),
            }).encode()
            content_type = "application/json"
        elif self.path == "/v1/audio/speech":
            data = b"ID3tiny-audio"
            content_type = "audio/mpeg"
        else:
            data = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            ).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ModelProfileProbeTests(unittest.TestCase):
    def setUp(self):
        ProbeHandler.requests = []
        ProbeHandler.models = ["deepseek-v4", "doubao-seedance-2-0-260128"]
        ProbeHandler.force_status = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase.open(Path(self.tmp.name) / "catalog.sqlite")
        self.db.migrate()
        create_provider_account(
            self.db,
            account_id="gateway",
            name="New API",
            provider="new_api",
            base_url=f"http://127.0.0.1:{self.httpd.server_port}",
            credential_ref="cutroom/provider/gateway",
            settings={"channel_id": 7},
        )

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _profile(self, profile_id, capability, model, settings):
        create_model_profile(
            self.db,
            profile_id=profile_id,
            capability=capability,
            display_name=profile_id,
            account_id="gateway",
            model=model,
            settings=settings,
            is_default=True,
        )

    def test_general_text_probe_makes_real_pinned_model_request(self):
        self._profile(
            "text-profile",
            "general_text",
            "deepseek-v4",
            {"protocol": "openai_chat"},
        )
        result = probe_model_profile(
            self.db,
            "text-profile",
            secret_reader=lambda _reference: "sk-gateway",
        )

        self.assertEqual(result["availabilityStatus"], "available")
        request = ProbeHandler.requests[-1]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["authorization"], "Bearer sk-gateway-7")
        self.assertEqual(json.loads(request["body"])["model"], "deepseek-v4")

    def test_each_split_text_profile_makes_a_real_chat_probe(self):
        for capability in ("copywriting", "storyboard", "packaging"):
            profile_id = f"{capability}-profile"
            self._profile(
                profile_id,
                capability,
                f"model-{capability}",
                {"protocol": "openai_chat"},
            )
            result = probe_model_profile(
                self.db,
                profile_id,
                secret_reader=lambda _reference: "sk-gateway",
            )
            self.assertEqual(result["availabilityStatus"], "available")
            request = ProbeHandler.requests[-1]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(
                json.loads(request["body"])["model"], f"model-{capability}"
            )

    def test_planning_profile_makes_a_real_chat_probe(self):
        self._profile(
            "planning-profile",
            "planning",
            "glm-5.3",
            {"protocol": "openai"},
        )

        result = probe_model_profile(
            self.db,
            "planning-profile",
            secret_reader=lambda _reference: "planning-key",
        )

        self.assertEqual(result["availabilityStatus"], "available")
        self.assertEqual(ProbeHandler.requests[-1]["path"], "/v1/chat/completions")
        self.assertEqual(
            json.loads(ProbeHandler.requests[-1]["body"])["model"],
            "glm-5.3",
        )

    def test_tts_probe_generates_a_small_real_audio_response(self):
        self._profile(
            "tts-profile",
            "tts",
            "tts-1",
            {"protocol": "openai_speech", "voice": "zh_male_test"},
        )
        result = probe_model_profile(
            self.db,
            "tts-profile",
            secret_reader=lambda _reference: "sk-gateway",
        )

        self.assertEqual(result["availabilityStatus"], "available")
        request = ProbeHandler.requests[-1]
        self.assertEqual(request["path"], "/v1/audio/speech")
        payload = json.loads(request["body"])
        self.assertEqual(payload["model"], "tts-1")
        self.assertEqual(payload["voice"], "zh_male_test")
        self.assertEqual(payload["input"], "连接测试")

    def test_direct_doubao_tts_probe_uses_official_contract(self):
        from media_catalog.credentials import encode_provider_credentials

        create_provider_account(
            self.db,
            account_id="doubao-tts-direct",
            name="豆包 TTS 直连",
            provider="doubao_tts",
            base_url=f"http://127.0.0.1:{self.httpd.server_port}/api/v1/tts",
            credential_ref="cutroom/provider/doubao-tts-direct",
            settings={
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "app_id": "appid-direct",
                "cluster": "volcano_tts",
            },
        )
        create_model_profile(
            self.db,
            profile_id="direct-tts",
            capability="tts",
            display_name="豆包直连配音",
            account_id="doubao-tts-direct",
            model="seed-tts-2.0",
            settings={
                "protocol": "volcengine_tts",
                "resource_id": "seed-tts-2.0",
                "default_voice_id": "voice-direct",
            },
        )

        result = probe_model_profile(
            self.db,
            "direct-tts",
            secret_reader=lambda _reference: encode_provider_credentials(
                "doubao-access-token",
                "cloud-sk-not-for-synthesis",
            ),
        )

        self.assertEqual(result["availabilityStatus"], "available")
        request = ProbeHandler.requests[-1]
        self.assertEqual(request["path"], "/api/v1/tts")
        self.assertEqual(request["authorization"], "Bearer;doubao-access-token")
        payload = json.loads(request["body"])
        self.assertEqual(payload["app"]["appid"], "appid-direct")
        self.assertEqual(payload["app"]["cluster"], "volcano_tts")
        self.assertEqual(payload["audio"]["voice_type"], "voice-direct")

    def test_doubao_api_key_probe_prefers_v3_sse_contract(self):
        from media_catalog.credentials import encode_provider_credentials

        create_provider_account(
            self.db,
            account_id="doubao-v3",
            name="豆包新版 TTS",
            provider="doubao_tts",
            base_url=f"http://127.0.0.1:{self.httpd.server_port}/api/v3/tts/unidirectional/sse",
            credential_ref="cutroom/provider/doubao-v3",
            settings={
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "tts_resource_id": "seed-tts-1.0",
            },
        )
        create_model_profile(
            self.db,
            profile_id="doubao-v3-profile",
            capability="tts",
            display_name="豆包新版配音",
            account_id="doubao-v3",
            model="seed-tts-1.0",
            settings={
                "protocol": "volcengine_tts",
                "resource_id": "seed-tts-1.0",
                "voice": "zh_female_vv_uranus_bigtts",
            },
        )

        result = probe_model_profile(
            self.db,
            "doubao-v3-profile",
            secret_reader=lambda _reference: encode_provider_credentials(
                "legacy-token-not-used",
                "catalog-sk-not-used",
                "speech-api-key",
            ),
        )

        self.assertEqual(result["availabilityStatus"], "available")
        request = ProbeHandler.requests[-1]
        self.assertEqual(request["path"], "/api/v3/tts/unidirectional/sse")
        self.assertIsNone(request["authorization"])
        self.assertEqual(request["x_api_key"], "speech-api-key")
        self.assertEqual(request["x_api_resource_id"], "seed-tts-1.0")
        self.assertTrue(request["x_api_request_id"])
        payload = json.loads(request["body"])
        self.assertEqual(payload["req_params"]["speaker"], "zh_female_vv_uranus_bigtts")
        self.assertEqual(payload["req_params"]["audio_params"]["format"], "mp3")

    def test_voice_preview_is_atomically_cached_and_marks_voice_available(self):
        from media_catalog.credentials import encode_provider_credentials

        create_provider_account(
            self.db,
            account_id="doubao-preview",
            name="豆包试听",
            provider="doubao_tts",
            base_url=f"http://127.0.0.1:{self.httpd.server_port}/api/v1/tts",
            credential_ref="cutroom/provider/doubao-preview",
            settings={
                "preset": "doubao_tts",
                "protocol": "volcengine_tts",
                "app_id": "appid-preview",
                "tts_resource_id": "seed-tts-2.0",
            },
        )
        replace_provider_voices(
            self.db,
            "doubao-preview",
            "seed-tts-2.0",
            [{
                "voiceId": "voice-preview",
                "displayName": "试听音色",
                "resourceId": "seed-tts-2.0",
            }],
        )
        create_model_profile(
            self.db,
            profile_id="preview-profile",
            capability="tts",
            display_name="试听配置",
            account_id="doubao-preview",
            model="seed-tts-2.0",
            settings={
                "protocol": "volcengine_tts",
                "resource_id": "seed-tts-2.0",
                "voice": "voice-preview",
            },
        )

        result = create_tts_voice_preview(
            self.db,
            "preview-profile",
            "voice-preview",
            Path(self.tmp.name),
            secret_reader=lambda _reference: encode_provider_credentials(
                "tts-token", "catalog-sk"
            ),
        )

        url_parts = result["previewUrl"].strip("/").split("/")
        target = (
            Path(self.tmp.name)
            / "runtime"
            / "workbench"
            / "voice-previews"
            / url_parts[-2]
            / url_parts[-1]
        )
        self.assertEqual(target.read_bytes(), b"ID3tiny-audio")
        self.assertNotIn("voice-preview", target.parts[-2:])
        voice = get_provider_voice(
            self.db,
            "doubao-preview",
            "seed-tts-2.0",
            "voice-preview",
        )
        self.assertTrue(voice["selectable"])
        self.assertEqual(voice["testStatus"], "available")

        ProbeHandler.force_status = 403
        with self.assertRaisesRegex(ModelProfileProbeError, "API Key"):
            create_tts_voice_preview(
                self.db,
                "preview-profile",
                "voice-preview",
                Path(self.tmp.name),
                secret_reader=lambda _reference: encode_provider_credentials(
                    "tts-token", "catalog-sk"
                ),
            )
        voice = get_provider_voice(
            self.db,
            "doubao-preview",
            "seed-tts-2.0",
            "voice-preview",
        )
        self.assertTrue(voice["selectable"])
        self.assertEqual(voice["testStatus"], "unavailable")
        self.assertEqual(voice["lastTestErrorCode"], "MODEL_AUTH_FAILED")

    def test_video_probe_checks_catalog_without_submitting_a_paid_generation(self):
        self._profile(
            "video-profile",
            "video_generation",
            "doubao-seedance-2-0-260128",
            {"protocol": "doubao_video"},
        )
        result = probe_model_profile(
            self.db,
            "video-profile",
            secret_reader=lambda _reference: "sk-gateway",
        )

        self.assertEqual(result["availabilityStatus"], "available")
        self.assertEqual(result["verificationLevel"], "catalog")
        self.assertEqual(
            [(item["method"], item["path"]) for item in ProbeHandler.requests],
            [("GET", "/v1/models")],
        )

    def test_broll_profile_checks_catalog_without_paid_generation(self):
        self._profile(
            "broll-profile",
            "broll",
            "doubao-seedance-2-0-260128",
            {"protocol": "doubao_video"},
        )

        result = probe_model_profile(
            self.db,
            "broll-profile",
            secret_reader=lambda _reference: "broll-key",
        )

        self.assertEqual(result["availabilityStatus"], "available")
        self.assertEqual(result["verificationLevel"], "catalog")
        self.assertEqual(
            [(item["method"], item["path"]) for item in ProbeHandler.requests],
            [("GET", "/v1/models")],
        )

    def test_direct_provider_model_catalog_uses_exact_key_and_returns_sorted_ids(self):
        ProbeHandler.models = ["model-b", "model-a", "model-a"]
        create_provider_account(
            self.db,
            account_id="catalog-direct",
            name="直连模型目录",
            provider="openai_compatible",
            base_url=f"http://127.0.0.1:{self.httpd.server_port}/v1",
            credential_ref="cutroom/provider/catalog-direct",
            settings={"preset": "openai_compatible", "protocol": "openai"},
        )

        models = list_provider_models(
            self.db,
            "catalog-direct",
            secret_reader=lambda _reference: "direct-catalog-key",
        )

        self.assertEqual(models, ["model-a", "model-b"])
        request = ProbeHandler.requests[-1]
        self.assertEqual(request["path"], "/v1/models")
        self.assertEqual(request["authorization"], "Bearer direct-catalog-key")

    def test_version_four_api_root_does_not_gain_a_v1_segment(self):
        create_provider_account(
            self.db,
            account_id="zhipu-v4-root",
            name="智谱 v4 根地址",
            provider="openai_compatible",
            base_url=(
                f"http://127.0.0.1:{self.httpd.server_port}/api/paas/v4"
            ),
            credential_ref="cutroom/provider/zhipu-v4-root",
            settings={"preset": "openai_compatible", "protocol": "openai"},
        )

        list_provider_models(
            self.db,
            "zhipu-v4-root",
            secret_reader=lambda _reference: "zhipu-test-key",
        )

        self.assertEqual(
            ProbeHandler.requests[-1]["path"],
            "/api/paas/v4/models",
        )

    def test_full_chat_completion_url_is_reused_without_duplicate_path(self):
        create_provider_account(
            self.db,
            account_id="zhipu-full-chat",
            name="智谱完整地址",
            provider="openai_compatible",
            base_url=(
                f"http://127.0.0.1:{self.httpd.server_port}"
                "/api/paas/v4/chat/completions"
            ),
            credential_ref="cutroom/provider/zhipu-full-chat",
            settings={"preset": "openai_compatible", "protocol": "openai"},
        )
        create_model_profile(
            self.db,
            profile_id="zhipu-full-chat-profile",
            capability="copywriting",
            display_name="智谱文案",
            account_id="zhipu-full-chat",
            model="glm-5.3",
            settings={"protocol": "openai"},
        )

        probe_model_profile(
            self.db,
            "zhipu-full-chat-profile",
            secret_reader=lambda _reference: "zhipu-test-key",
        )

        self.assertEqual(
            ProbeHandler.requests[-1]["path"],
            "/api/paas/v4/chat/completions",
        )

    def test_provider_without_catalog_requires_manual_model_entry(self):
        create_provider_account(
            self.db,
            account_id="tts-no-catalog",
            name="豆包 TTS",
            provider="doubao_tts",
            base_url="https://openspeech.bytedance.com/api/v1/tts",
            credential_ref="cutroom/provider/tts-no-catalog",
            settings={"preset": "doubao_tts", "protocol": "volcengine_tts"},
        )

        with self.assertRaisesRegex(ModelProfileProbeError, "手工填写"):
            list_provider_models(
                self.db,
                "tts-no-catalog",
                secret_reader=lambda _reference: "token",
            )

    def test_new_api_without_optional_channel_uses_raw_token(self):
        create_provider_account(
            self.db,
            account_id="unpinned",
            name="未固定渠道",
            provider="new_api",
            base_url=f"http://127.0.0.1:{self.httpd.server_port}",
            credential_ref="cutroom/provider/unpinned",
            settings={},
        )
        create_model_profile(
            self.db,
            profile_id="unpinned-text",
            capability="general_text",
            display_name="未固定渠道文本模型",
            account_id="unpinned",
            model="deepseek-v4",
            settings={"protocol": "openai"},
        )

        result = probe_model_profile(
            self.db,
            "unpinned-text",
            secret_reader=lambda _reference: "sk-gateway",
        )

        self.assertEqual(result["availabilityStatus"], "available")
        self.assertEqual(
            ProbeHandler.requests[-1]["authorization"],
            "Bearer sk-gateway",
        )

    def test_direct_provider_probe_uses_exact_api_key(self):
        create_provider_account(
            self.db,
            account_id="ark-direct",
            name="火山方舟直连",
            provider="volcengine_ark",
            base_url=f"http://127.0.0.1:{self.httpd.server_port}",
            credential_ref="cutroom/provider/ark-direct",
            settings={"preset": "volcengine_ark", "protocol": "openai"},
        )
        create_model_profile(
            self.db,
            profile_id="direct-text",
            capability="storyboard",
            display_name="直连分镜",
            account_id="ark-direct",
            model="deepseek-v4",
            settings={"protocol": "openai"},
        )

        result = probe_model_profile(
            self.db,
            "direct-text",
            secret_reader=lambda _reference: "ark-direct-key",
        )

        self.assertEqual(result["availabilityStatus"], "available")
        self.assertEqual(
            ProbeHandler.requests[-1]["authorization"],
            "Bearer ark-direct-key",
        )

    def test_probe_records_actionable_auth_and_endpoint_error_codes(self):
        self._profile(
            "diagnostic-text",
            "storyboard",
            "deepseek-v4",
            {"protocol": "openai"},
        )

        for status, expected_code in (
            (401, "MODEL_AUTH_FAILED"),
            (403, "MODEL_AUTH_FAILED"),
            (404, "MODEL_OR_ENDPOINT_NOT_FOUND"),
        ):
            with self.subTest(status=status):
                ProbeHandler.force_status = status
                with self.assertRaises(ModelProfileProbeError):
                    probe_model_profile(
                        self.db,
                        "diagnostic-text",
                        secret_reader=lambda _reference: "sk-gateway",
                    )
                row = self.db.connection.execute(
                    "SELECT last_error_code FROM model_profiles "
                    "WHERE profile_id='diagnostic-text'"
                ).fetchone()
                self.assertEqual(row["last_error_code"], expected_code)
