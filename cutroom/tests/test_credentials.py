import unittest

from media_catalog.credentials import (
    MemoryCredentialBackend,
    configured_capabilities,
    decode_provider_credentials,
    delete_provider_secret,
    delete_secret,
    encode_provider_credentials,
    read_provider_secret,
    read_secret,
    save_provider_secret,
    save_secret,
)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryCredentialBackend()

    def test_secret_round_trip_uses_stable_reference_without_exposing_value(self):
        reference = save_secret("general_text", "sk-private-value", self.backend)
        self.assertEqual(reference, "cutroom/general_text")
        self.assertEqual(
            read_secret("general_text", self.backend),
            "sk-private-value",
        )
        self.assertNotIn("sk-private-value", repr(self.backend))

        delete_secret("general_text", self.backend)
        self.assertIsNone(read_secret("general_text", self.backend))

    def test_env_is_read_only_compatibility_fallback(self):
        env = {
            "ARK_API_KEY": "legacy-text-key",
            "SEEDANCE_API_KEY": "legacy-video-key",
            "TTS_ACCESS_TOKEN": "legacy-tts-key",
        }
        state = configured_capabilities(self.backend, env=env)
        self.assertTrue(state["general_text"]["configured"])
        self.assertTrue(state["video_generation"]["configured"])
        self.assertTrue(state["tts"]["configured"])
        self.assertTrue(state["visual_matching"]["configured"])
        self.assertEqual(
            read_secret("general_text", self.backend, env=env),
            "legacy-text-key",
        )
        self.assertEqual(self.backend.references(), [])

    def test_blank_or_unknown_capability_is_rejected_without_echoing_secret(self):
        with self.assertRaisesRegex(ValueError, "密钥不能为空"):
            save_secret("general_text", "   ", self.backend)
        with self.assertRaisesRegex(ValueError, "未知能力"):
            save_secret("not-a-capability", "do-not-repeat-me", self.backend)

    def test_provider_account_secret_uses_scoped_reference(self):
        reference = save_provider_secret(
            "account-ark-01", "gateway-secret", self.backend
        )
        self.assertEqual(reference, "cutroom/provider/account-ark-01")
        self.assertEqual(
            read_provider_secret(reference, self.backend),
            "gateway-secret",
        )
        delete_provider_secret(reference, self.backend)
        self.assertIsNone(read_provider_secret(reference, self.backend))

        with self.assertRaisesRegex(ValueError, "账号 ID"):
            save_provider_secret("../escape", "secret", self.backend)

    def test_provider_credential_bundle_round_trip_hides_both_secrets(self):
        raw = encode_provider_credentials("tts-token", "cloud-sk", "speech-api-key")
        reference = save_provider_secret("doubao-local", raw, self.backend)

        self.assertEqual(
            decode_provider_credentials(read_provider_secret(reference, self.backend)),
            {
                "access_token": "tts-token",
                "secret_access_key": "cloud-sk",
                "api_key": "speech-api-key",
            },
        )
        self.assertNotIn("tts-token", repr(self.backend))
        self.assertNotIn("cloud-sk", repr(self.backend))
        self.assertNotIn("speech-api-key", repr(self.backend))

    def test_legacy_provider_secret_decodes_as_tts_access_token(self):
        self.assertEqual(
            decode_provider_credentials("legacy-token"),
            {
                "access_token": "legacy-token",
                "secret_access_key": None,
                "api_key": None,
            },
        )

    def test_schema_one_provider_bundle_remains_readable(self):
        self.assertEqual(
            decode_provider_credentials(
                '{"schema":1,"access_token":"old-token","secret_access_key":"old-sk"}'
            ),
            {
                "access_token": "old-token",
                "secret_access_key": "old-sk",
                "api_key": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
