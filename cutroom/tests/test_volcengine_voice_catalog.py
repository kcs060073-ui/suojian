import json
import unittest

from media_catalog.volcengine_voice_catalog import (
    VoiceCatalogError,
    fetch_volcengine_voices,
)


class FakeRequest:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def list_speakers(self, request, _request_timeout):
        self.calls.append((request, _request_timeout))
        return self.pages.pop(0)


class VolcengineVoiceCatalogTests(unittest.TestCase):
    def test_list_speakers_paginates_and_normalizes_official_fields(self):
        client = FakeClient([
            {
                "Speakers": [{
                    "VoiceType": "zh_male_sunshine_junior",
                    "Name": "阳光青年 2.0",
                    "ResourceID": "seed-tts-2.0",
                    "Gender": "male",
                    "Languages": ["zh-CN"],
                    "Emotions": ["happy"],
                    "Categories": ["通用场景"],
                    "NormalLabels": ["清晰"],
                    "SpecialLabels": ["大模型"],
                    "Age": "青年",
                    "Avatar": "https://example.invalid/avatar.png",
                    "Description": "明亮自然",
                    "TrialURL": "https://example.invalid/full.mp3",
                    "ShortTrialURL": "https://example.invalid/short.mp3",
                }],
                "Total": 2,
            },
            {
                "Speakers": [{
                    "VoiceType": "zh_female_cancan",
                    "Name": "灿灿 2.0",
                    "ResourceID": "seed-tts-2.0",
                    "Gender": "female",
                    "Languages": ["zh-CN"],
                }],
                "Total": 2,
            },
        ])
        factory_calls = []

        def factory(access_key_id, secret_access_key):
            factory_calls.append((access_key_id, secret_access_key))
            return client, FakeRequest

        voices = fetch_volcengine_voices(
            "AKLT-test",
            "cloud-secret",
            "seed-tts-2.0",
            timeout=7.5,
            client_factory=factory,
        )

        self.assertEqual(factory_calls, [("AKLT-test", "cloud-secret")])
        self.assertEqual(len(client.calls), 2)
        first_request, first_timeout = client.calls[0]
        second_request, _ = client.calls[1]
        self.assertEqual(first_request.page, 1)
        self.assertEqual(second_request.page, 2)
        self.assertIsNone(getattr(first_request, "limit", None))
        self.assertEqual(first_request.resource_ids, ["seed-tts-2.0"])
        self.assertEqual(first_timeout, 7.5)
        self.assertEqual(
            voices[0],
            {
                "voiceId": "zh_male_sunshine_junior",
                "displayName": "阳光青年 2.0",
                "resourceId": "seed-tts-2.0",
                "gender": "male",
                "languages": ["zh-CN"],
                "emotions": ["happy"],
                "styles": ["通用场景", "清晰", "大模型"],
                "previewUrl": "https://example.invalid/full.mp3",
                "shortPreviewUrl": "https://example.invalid/short.mp3",
                "metadata": {
                    "age": "青年",
                    "avatar": "https://example.invalid/avatar.png",
                    "description": "明亮自然",
                    "categories": ["通用场景"],
                    "normalLabels": ["清晰"],
                    "specialLabels": ["大模型"],
                },
            },
        )
        self.assertEqual(voices[1]["voiceId"], "zh_female_cancan")

    def test_official_request_model_never_sets_a_nonempty_limit(self):
        import volcenginesdkspeechsaasprod as speech

        client = FakeClient([
            speech.ListSpeakersResponse(speakers=[], total=0),
        ])

        fetch_volcengine_voices(
            "AKLT-test",
            "cloud-secret",
            "seed-tts-2.0",
            client_factory=lambda *_: (client, speech.ListSpeakersRequest),
        )

        request, _ = client.calls[0]
        self.assertIsNone(request.limit)
        self.assertEqual(request.page, 1)
        self.assertEqual(request.resource_ids, ["seed-tts-2.0"])

    def test_list_speakers_stops_on_empty_page_before_reported_total(self):
        client = FakeClient([
            {
                "Speakers": [{
                    "VoiceType": "voice-a",
                    "Name": "音色 A",
                    "ResourceID": "seed-tts-2.0",
                }],
                "Total": 99,
            },
            {"Speakers": [], "Total": 99},
        ])

        voices = fetch_volcengine_voices(
            "AKLT-test",
            "cloud-secret",
            "seed-tts-2.0",
            client_factory=lambda *_: (client, FakeRequest),
        )

        self.assertEqual([voice["voiceId"] for voice in voices], ["voice-a"])
        self.assertEqual([call[0].page for call in client.calls], [1, 2])

    def test_list_speakers_deduplicates_voice_type_across_pages(self):
        client = FakeClient([
            {
                "Speakers": [{
                    "VoiceType": "voice-a",
                    "Name": "音色 A",
                    "ResourceID": "seed-tts-2.0",
                }],
                "Total": 3,
            },
            {
                "Speakers": [
                    {
                        "VoiceType": "voice-a",
                        "Name": "音色 A 重复项",
                        "ResourceID": "seed-tts-2.0",
                    },
                    {
                        "VoiceType": "voice-b",
                        "Name": "音色 B",
                        "ResourceID": "seed-tts-2.0",
                    },
                ],
                "Total": 3,
            },
        ])

        voices = fetch_volcengine_voices(
            "AKLT-test",
            "cloud-secret",
            "seed-tts-2.0",
            client_factory=lambda *_: (client, FakeRequest),
        )

        self.assertEqual(
            [voice["voiceId"] for voice in voices],
            ["voice-a", "voice-b"],
        )
        self.assertEqual(len(client.calls), 2)

    def test_list_speakers_has_a_bounded_page_count(self):
        pages = [
            {
                "Speakers": [{
                    "VoiceType": f"voice-{page}",
                    "Name": f"音色 {page}",
                    "ResourceID": "seed-tts-2.0",
                }],
                "Total": 1000,
            }
            for page in range(1, 101)
        ]
        client = FakeClient(pages)

        with self.assertRaises(VoiceCatalogError) as raised:
            fetch_volcengine_voices(
                "AKLT-test",
                "cloud-secret",
                "seed-tts-2.0",
                client_factory=lambda *_: (client, FakeRequest),
            )

        self.assertEqual(raised.exception.code, "VOICE_CATALOG_INVALID_RESPONSE")
        self.assertEqual(len(client.calls), 100)

    def test_upstream_exception_is_replaced_with_safe_error(self):
        class FailingClient:
            def list_speakers(self, request, _request_timeout):
                raise RuntimeError(
                    "Authorization AKLT-secret cloud-secret signed-header"
                )

        with self.assertRaises(VoiceCatalogError) as raised:
            fetch_volcengine_voices(
                "AKLT-secret",
                "cloud-secret",
                "seed-tts-2.0",
                client_factory=lambda *_: (FailingClient(), FakeRequest),
            )

        self.assertEqual(raised.exception.code, "VOICE_CATALOG_UNAVAILABLE")
        rendered = str(raised.exception)
        self.assertNotIn("AKLT-secret", rendered)
        self.assertNotIn("cloud-secret", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_invalid_parameter_is_replaced_with_actionable_safe_error(self):
        class InvalidParameterError(RuntimeError):
            status = 400
            body = json.dumps({
                "ResponseMetadata": {
                    "Error": {
                        "Code": "InvalidParameter",
                        "Message": (
                            "Invalid limit for AKLT-secret and cloud-secret"
                        ),
                    },
                },
            })

        class FailingClient:
            def list_speakers(self, request, _request_timeout):
                raise InvalidParameterError("unsafe upstream response")

        with self.assertRaises(VoiceCatalogError) as raised:
            fetch_volcengine_voices(
                "AKLT-secret",
                "cloud-secret",
                "seed-tts-2.0",
                client_factory=lambda *_: (FailingClient(), FakeRequest),
            )

        self.assertEqual(
            raised.exception.code,
            "VOICE_CATALOG_INVALID_PARAMETER",
        )
        self.assertEqual(
            str(raised.exception),
            "音色目录请求参数不兼容，请更新 剪辑工作台 后重试。",
        )
        self.assertNotIn("AKLT-secret", str(raised.exception))
        self.assertNotIn("cloud-secret", str(raised.exception))

    def test_official_nested_metadata_models_keep_readable_labels(self):
        import volcenginesdkspeechsaasprod as speech

        speaker = speech.SpeakerForListSpeakersOutput(
            voice_type="nested-voice",
            name="嵌套元数据音色",
            resource_id="seed-tts-2.0",
            languages=[speech.LanguageForListSpeakersOutput(
                language="zh",
                text="中文",
            )],
            emotions=[speech.EmotionForListSpeakersOutput(
                label="开心",
                value="happy",
            )],
            categories=[speech.CategoryForListSpeakersOutput(
                categories=["通用", "新闻"],
            )],
        )
        client = FakeClient([
            speech.ListSpeakersResponse(speakers=[speaker], total=1)
        ])

        voices = fetch_volcengine_voices(
            "AKLT-test",
            "cloud-secret",
            "seed-tts-2.0",
            client_factory=lambda *_: (client, speech.ListSpeakersRequest),
        )

        self.assertEqual(voices[0]["languages"], ["中文"])
        self.assertEqual(voices[0]["emotions"], ["开心"])
        self.assertEqual(voices[0]["styles"], ["通用", "新闻"])


if __name__ == "__main__":
    unittest.main()
