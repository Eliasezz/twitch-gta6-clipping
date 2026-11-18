"""Regression checks: transcription quota must never prevent a playable MP4."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import captions, video_pipeline, video_routes, large_upload


class QuotaFallbackTests(unittest.TestCase):
    def test_hook_only_has_no_division_by_zero_or_fake_words(self):
        with tempfile.TemporaryDirectory() as root:
            out=Path(root) / "hook-only.ass"
            created=video_pipeline.make_ass(
                "", 65, out, width=540, height=960,
                timed_words=None, hook_text="GTA VI | REACTION"
            )
            self.assertTrue(created)
            content=out.read_text(encoding="utf-8")
            self.assertIn("GTA VI | REACTION", content)
            self.assertEqual(content.count("Dialogue: 0,"),1)

    def test_quota_error_outputs_real_mp4_without_fabricated_subtitles(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg not installed on runner")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            src, dst = folder / "source.mp4", folder / "vertical.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=24:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=510:duration=4",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest", "-y", str(src),
            ], timeout=60, check=True)
            with patch.object(
                captions, "transcribe_english",
                side_effect=captions.CaptionError(
                    "OpenAI quota or transcription rate limit exceeded"
                )
            ) as mocked:
                details = video_pipeline.render_mp4(
                    src, dst, clip_seconds=3, width=360, height=640,
                    auto_subtitles=True, hook_text="GTA VI | TEST",
                    music_enabled=True,
                )
            self.assertEqual(mocked.call_count, 1)
            self.assertTrue(dst.is_file())
            self.assertGreater(dst.stat().st_size, 2048)
            self.assertEqual(details["caption_mode"], "not_available")
            self.assertEqual(details["transcription_warning"],
                             "openai_quota_or_rate_limit")
            self.assertFalse(details["speech_words_synchronized"])
            self.assertGreater(details["duration_seconds"], 2)
            self.assertTrue(details["original_music_mixed"])

    def test_recover_only_opted_in_failed_caption_jobs_once(self):
        job = "05995e36-14dd-46b4-913f-0c2d1a0a8e62"
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            video_routes, "VIDEO_DIR", Path(tmp)
        ), patch.dict(os.environ, {
            "VIDEO_RECOVER_FAILED_JOB_IDS": job,
        }), patch("app.video_routes.threading.Thread") as threading_mock:
            folder=video_routes._job_dir(job)
            video_routes._write_status(folder, {
                "job_id":job,
                "state":"failed",
                "authorized":True,
                "error":"CaptionError",
                "requested_duration_seconds":65,
                "auto_subtitles_requested":True,
                "music_original_generated":True,
            })
            (folder / "source.mp4").write_bytes(b"some-original-upload")
            recovered = video_routes.recover_failed_video_jobs_on_startup()
            self.assertEqual(recovered,[job])
            self.assertEqual(threading_mock.call_count,1)
            state = video_routes._read_status(job)
            self.assertEqual(state["state"],"queued")
            self.assertEqual(state["previous_error"],"CaptionError")
            self.assertTrue(state["auto_recovery_attempted_at"])
            second=video_routes.recover_failed_video_jobs_on_startup()
            self.assertEqual(second,[])

    def test_revision_two_can_retry_previous_zero_division_once(self):
        job="05995e36-14dd-46b4-913f-0c2d1a0a8e62"
        with tempfile.TemporaryDirectory() as root, patch.object(
            video_routes, "VIDEO_DIR", Path(root)
        ), patch.dict(os.environ, {
            "VIDEO_RECOVER_FAILED_JOB_IDS":job
        }), patch("app.video_routes.threading.Thread") as thread:
            folder=video_routes._job_dir(job)
            (folder / "source.mp4").parent.mkdir(parents=True, exist_ok=True)
            (folder / "source.mp4").write_bytes(b"retained original")
            video_routes._write_status(folder,{
                "job_id":job,"state":"failed","authorized":True,
                "error":"ZeroDivisionError",
                "auto_recovery_attempted_at":"2026-11-18T21:45:09+00:00",
                "requested_duration_seconds":65,
            })
            self.assertEqual(video_routes.recover_failed_video_jobs_on_startup(),[job])
            self.assertEqual(video_routes._read_status(job)["auto_recovery_revision"],"hook-only-v2")
            self.assertEqual(video_routes.recover_failed_video_jobs_on_startup(),[])
            self.assertEqual(thread.call_count,1)

    def test_one_click_page_can_resume_and_export_mp4(self):
        html=large_upload.uploader_page()
        self.assertIn("Tout exécuter : importer",html)
        self.assertIn("Réparation du montage précédent sans nouvel envoi",html)
        self.assertIn("Obtenir un lien MP4 seul",html)
        self.assertIn("transcription_warning",html)
        self.assertIn("export-link",html)


if __name__ == "__main__":
    unittest.main()
