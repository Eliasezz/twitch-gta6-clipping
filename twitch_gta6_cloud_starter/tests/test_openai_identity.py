"""Render OpenAI OIDC wiring regression tests (never call the real API)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import captions
from app import autopilot
from app import video_routes


class OpenAIWorkloadIdentityTests(unittest.TestCase):
    def test_no_credentials_blocks_transcription(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "OPENAI_IDENTITY_PROVIDER_ID": "",
            "OPENAI_SERVICE_ACCOUNT_ID": "",
            "OPENAI_IDENTITY_TOKEN_FILE": "",
        }):
            status = captions.openai_auth_status()
            self.assertFalse(status["configured"])
            self.assertEqual(status["method"], "unconfigured")
            self.assertFalse(video_routes.video_ready()["auto_speech_transcription"])
            self.assertFalse(autopilot.readiness()["checks"]["speech_auth_configured"])
            with self.assertRaises(captions.CaptionError):
                captions._openai_client()

    def test_existing_key_stays_supported(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "example-fake-key",
            "OPENAI_IDENTITY_PROVIDER_ID": "",
            "OPENAI_SERVICE_ACCOUNT_ID": "",
            "OPENAI_IDENTITY_TOKEN_FILE": "",
        }), patch("openai.OpenAI") as make_client:
            client = captions._openai_client()
            make_client.assert_called_once()
            self.assertEqual(make_client.call_args.kwargs["api_key"], "example-fake-key")
            self.assertIs(client, make_client.return_value)
            self.assertEqual(captions.openai_auth_status()["method"], "api_key")

    def test_workload_oidc_file_is_read_fresh_at_every_exchange(self):
        with tempfile.TemporaryDirectory() as folder:
            token_file = Path(folder) / "identity-token"
            token_file.write_text("jwt-one\n", encoding="utf-8")
            env = {
                "OPENAI_API_KEY": "",
                "OPENAI_IDENTITY_PROVIDER_ID": "widp-example",
                "OPENAI_SERVICE_ACCOUNT_ID": "user-example",
                "OPENAI_IDENTITY_TOKEN_FILE": str(token_file),
            }
            with patch.dict(os.environ, env), patch("openai.OpenAI") as make_client:
                self.assertTrue(captions.openai_auth_status()["configured"])
                self.assertEqual(captions.openai_auth_status()["method"], "workload_identity")
                self.assertTrue(video_routes.video_ready()["auto_speech_transcription"])
                self.assertTrue(autopilot.readiness()["checks"]["speech_auth_configured"])
                captions._openai_client()
                identity = make_client.call_args.kwargs["workload_identity"]
                self.assertEqual(identity["identity_provider_id"], "widp-example")
                self.assertEqual(identity["service_account_id"], "user-example")
                callback = identity["provider"]["get_token"]
                self.assertEqual(callback(), "jwt-one")
                token_file.write_text("jwt-rotated\n", encoding="utf-8")
                self.assertEqual(callback(), "jwt-rotated")

    def test_partial_oidc_does_not_claim_ready(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "",
            "OPENAI_IDENTITY_PROVIDER_ID": "widp-example",
            "OPENAI_SERVICE_ACCOUNT_ID": "user-example",
            "OPENAI_IDENTITY_TOKEN_FILE": "/nonexistent/oidc-token",
        }):
            status = captions.openai_auth_status()
            self.assertFalse(status["configured"])
            self.assertFalse(status["identity_token_file_available"])
            self.assertFalse(status["oidc_ready"])

    def test_sdk_audio_calls_keep_timestamp_options(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "speech.mp3"
            source.write_bytes(b"fake-audio")
            mocked = Mock()
            transcript = Mock()
            transcript.model_dump.return_value = {
                "language": "english",
                "words": [{"word": "YES", "start": 0.1, "end": 0.5}],
            }
            mocked.audio.transcriptions.create.return_value = transcript
            with patch.object(captions, "_openai_client", return_value=mocked):
                data = captions._request_whisper(source, translate=False)
            self.assertEqual(data["language"], "english")
            arguments = mocked.audio.transcriptions.create.call_args.kwargs
            self.assertEqual(arguments["timestamp_granularities"], ["word"])
            self.assertEqual(arguments["response_format"], "verbose_json")


if __name__ == "__main__":
    unittest.main()
