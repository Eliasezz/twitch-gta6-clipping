"""Regression checks for original music, official Twitch OAuth, and Metricool autopilot."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from app import autopilot, metricool_api, music, twitch_media, video_routes, video_pipeline


class MusicTests(unittest.TestCase):
    def test_original_track_is_generated_and_valid_wave(self):
        import wave
        with tempfile.TemporaryDirectory() as tmp:
            f = music.make_original_beat(Path(tmp) / "original.wav")
            with wave.open(str(f), "rb") as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnchannels(), 1)
                self.assertGreater(audio.getnframes(), 110000)

    def test_music_and_hook_appear_in_real_render(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=24:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=530:duration=4",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "-shortest", str(source)
            ], check=True, timeout=40)
            result = video_pipeline.render_mp4(
                source, root / "with_music.mp4", clip_seconds=3,
                width=360, height=640, music_enabled=True,
                hook_text="GTA VI | REACTION"
            )
            self.assertTrue(result["original_music_mixed"])
            self.assertEqual(result["hook_text"], "GTA VI | REACTION")
            self.assertTrue((root / "with_music.mp4").is_file())
            self.assertFalse((root / "with_music.original-bed.wav").exists())


class TwitchTests(unittest.TestCase):
    def test_official_clip_endpoint_scoped_and_not_scraped(self):
        sample = {
            "data": [{
                "clip_id": "gta-test-id",
                "landscape_download_url":
                    "https://production.assets.clips.twitchcdn.net/approvedclip.mp4",
            }]
        }
        response = Mock(status_code=200, ok=True)
        response.json.return_value = sample
        with patch.dict(os.environ, {
            "ALLOWED_BROADCASTER_IDS": "1234",
            "TWITCH_EDITOR_ID": "viewer_editor",
            "TWITCH_EDITOR_ACCESS_TOKEN": "user_oauth_token",
            "TWITCH_CLIENT_ID": "app_id",
        }), patch("app.twitch_media.requests.get", return_value=response) as http:
            self.assertEqual(
                twitch_media.official_clip_url("1234", "gta-test-id"),
                sample["data"][0]["landscape_download_url"]
            )
            self.assertEqual(http.call_args.kwargs["params"]["editor_id"], "viewer_editor")
            self.assertEqual(http.call_args.kwargs["headers"]["Client-Id"], "app_id")
            self.assertEqual(http.call_args.kwargs["headers"]["Authorization"],
                             "Bearer user_oauth_token")

    def test_creator_not_allowlisted(self):
        with patch.dict(os.environ, {"ALLOWED_BROADCASTER_IDS": "1234"}):
            with self.assertRaises(twitch_media.TwitchMediaError):
                twitch_media.official_clip_url("random", "test")

    def test_official_host_required(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {
            "data": [{"clip_id": "bad", "landscape_download_url":
                      "https://insecure.example/clip.mp4"}]
        }
        with patch.dict(os.environ, {
            "ALLOWED_BROADCASTER_IDS": "1234",
            "TWITCH_EDITOR_ID": "1234",
            "TWITCH_EDITOR_ACCESS_TOKEN": "token",
            "TWITCH_CLIENT_ID": "id"
        }), patch("app.twitch_media.requests.get", return_value=response):
            with self.assertRaises(twitch_media.TwitchMediaError):
                twitch_media.official_clip_url("1234", "bad")


class MetricoolTests(unittest.TestCase):
    def test_schedule_payload_includes_both_networks_with_media(self):
        with patch.dict(os.environ, {
            "METRICOOL_API_TOKEN": "private",
            "METRICOOL_USER_ID": "6375809",
            "METRICOOL_BLOG_ID": "8109476",
            "METRICOOL_AUTO_PUBLISH": "1",
        }), patch("app.metricool_api.requests.post") as call:
            response = Mock(status_code=200, ok=True)
            response.json.return_value = {"id": 4711}
            call.return_value = response
            result = metricool_api.schedule_both_networks(
                "https://cdn.metricool.com/approved-video.mp4",
                "GTA VI licensed reactions #GTA6 #Gaming",
                "GTA VI | Best reaction"
            )
            payload = call.call_args.kwargs["json"]
            self.assertEqual([p["network"] for p in payload["providers"]],
                             ["tiktok", "youtube"])
            self.assertTrue(payload["autoPublish"])
            self.assertEqual(payload["media"], ["https://cdn.metricool.com/approved-video.mp4"])
            self.assertFalse(payload["youtubeData"]["madeForKids"])
            self.assertEqual(result["response_id"], 4711)

    def test_metricool_no_api_key_means_no_post(self):
        with patch.dict(os.environ, {
            "METRICOOL_API_TOKEN": "",
            "METRICOOL_AUTO_PUBLISH": "1",
        }):
            with self.assertRaises(metricool_api.MetricoolPublishingError):
                metricool_api.schedule_both_networks(
                    "https://cdn.metricool.com/video.mp4", "desc", "title"
                )

    def test_media_transfer_needs_unlisted_token_and_genuine_video(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            video_routes, "VIDEO_DIR", Path(tmp)
        ):
            job = "00000000-0000-4000-8000-000000000001"
            folder = video_routes._job_dir(job)
            video_routes._write_status(folder, {
                "state": "done", "authorized": True, "autogenerated": True,
                "publication_status": "media_transfer_pending",
                "transfer_token": "unguessable_token",
            })
            (folder / "vertical.mp4").write_bytes(b"x"*4096)
            video_routes.metricool_temporary_media_transfer(job, "unguessable_token")
            with self.assertRaises(HTTPException):
                video_routes.metricool_temporary_media_transfer(job, "wrong")
            status = video_routes._read_status(job)
            status["transfer_token"] = None
            video_routes._write_status(folder, status)
            with self.assertRaises(HTTPException):
                video_routes.metricool_temporary_media_transfer(job, "unguessable_token")


class AutopilotTests(unittest.TestCase):
    def test_autopilot_disabled_without_editor_oauth_and_metricool_token(self):
        with patch.dict(os.environ, {
            "AUTO_PIPELINE_ENABLED": "1",
            "ALLOWED_BROADCASTER_IDS": "1234",
            "TWITCH_EDITOR_ID": "",
            "TWITCH_EDITOR_ACCESS_TOKEN": "",
            "OPENAI_API_KEY": "somekey",
            "METRICOOL_API_TOKEN": "",
            "METRICOOL_AUTO_PUBLISH": "1",
        }):
            state = autopilot.readiness()
            self.assertFalse(state["ready_for_automatic_publishing"])
            self.assertFalse(state["checks"]["twitch_editor_oauth_present"])
            self.assertFalse(state["checks"]["metricool_api_token_present"])


if __name__ == "__main__":
    unittest.main()
