"""OpenAI quota fallback: render MP4 without fabricated speech captions."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import large_upload, video_routes, publishing
from app.captions import CaptionError


class OpenAIQuotaFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.patcher = patch.object(video_routes, "VIDEO_DIR", self.folder)
        self.patcher.start()
        self.id = "00000000-0000-4000-8000-000000000099"
        self.job = self.folder / self.id
        self.job.mkdir()
        self.source = self.job / "source.mp4"
        self.source.write_bytes(b"video" * 2000)

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_rate_limit_falls_back_to_real_mp4_but_keeps_source(self):
        video_routes._write_status(self.job, {
            "job_id": self.id, "state": "queued", "authorized": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "auto_subtitles_requested": True,
            "requested_duration_seconds": 65,
        })
        calls = []
        def renderer(source, output, **kwargs):
            calls.append(kwargs)
            if kwargs["auto_subtitles"]:
                raise CaptionError("OpenAI quota or transcription rate limit exceeded")
            Path(output).write_bytes(b"made-video" * 2000)
            return {"duration_seconds": 65, "caption_mode": "none",
                    "original_music_mixed": True}
        with patch.object(video_routes, "probe", return_value={
            "duration": 1300, "width": 1280, "height": 720, "has_audio": True
        }), patch.object(video_routes, "render_mp4", side_effect=renderer):
            video_routes._worker(
                self.id, "", 65, auto_subtitles=True, music_enabled=True,
                hook_text="GTA VI | HIGHLIGHT",
            )
        status = video_routes._read_status(self.id)
        self.assertEqual(status["state"], "done")
        self.assertTrue(status["subtitles_pending"])
        self.assertTrue(status["source_retained_for_retry"])
        self.assertTrue(self.source.is_file())
        self.assertTrue((self.job / "vertical.mp4").is_file())
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["auto_subtitles"])
        self.assertFalse(calls[1]["auto_subtitles"])
        self.assertFalse(publishing.publication_package(self.id, status)["ready"])

    def test_previous_quota_error_recovers_without_upload(self):
        video_routes._write_status(self.job, {
            "job_id": self.id, "state": "failed", "error": "CaptionError",
            "authorized": True, "created_at": datetime.now(timezone.utc).isoformat(),
            "auto_subtitles_requested": True, "source_retained_for_retry": True,
            "requested_duration_seconds": 65,
            "music_original_generated": True,
            "hook_text": "GTA VI | REACTION",
        })
        with patch.object(large_upload.threading, "Thread") as thread:
            recovered = large_upload.recover_failed_caption_uploads()
        self.assertEqual(recovered, [self.id])
        thread.return_value.start.assert_called_once()
        status = video_routes._read_status(self.id)
        self.assertEqual(status["state"], "queued")
        self.assertTrue(status["subtitles_pending"])
        self.assertTrue(status["caption_fallback_attempted"])
        self.assertFalse(status["auto_subtitles_requested"])
        self.assertEqual(large_upload.recover_failed_caption_uploads(), [])

    def test_caption_retry_can_upgrade_completed_clip_without_upload(self):
        video_routes._write_status(self.job, {
            "job_id": self.id, "state": "done", "authorized": True,
            "subtitles_pending": True, "source_retained_for_retry": True,
            "requested_duration_seconds": 65,
            "music_original_generated": True,
        })
        with patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "test-admin",
                                      "OPENAI_API_KEY": "configured-test-value"}):
            from fastapi import BackgroundTasks
            pending = BackgroundTasks()
            result = video_routes.retry_failed_video_job(
                self.id, pending, auto_subtitles=True,
                x_admin_token="test-admin",
            )
        self.assertEqual(result["state"], "queued")
        self.assertEqual(len(pending.tasks), 1)
        status = video_routes._read_status(self.id)
        self.assertFalse(status["subtitles_pending"])
        self.assertTrue(status["auto_subtitles_requested"])


if __name__ == "__main__":
    unittest.main()
