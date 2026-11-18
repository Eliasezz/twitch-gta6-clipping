"""Offline smoke test: actual FFmpeg output and authorization safeguards."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, UploadFile

from app import video_pipeline as pipeline
from app import video_routes as routes


class VideoWorkflowTests(unittest.TestCase):
    def test_render_synthetic_vertical_mp4(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg unavailable in test environment")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source, output = folder / "source.mp4", folder / "final.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=24:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=500:duration=4",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest", str(source)
            ], check=True, timeout=45)
            result = pipeline.render_mp4(
                source, output, caption_text="THIS IS A SYNTHETIC VIDEO WORKFLOW TEST",
                clip_seconds=3, width=360, height=640, dynamic_zoom=True
            )
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1024)
            self.assertEqual((result["width"], result["height"]), (360, 640))
            self.assertEqual(result["fps"], 24)
            self.assertEqual(result["caption_mode"], "approximate_supplied_text")
            self.assertFalse(output.with_suffix(".ass").exists())
            self.assertGreater(pipeline.probe(output)["duration"], 2)

    def test_minute_plus_requires_sufficient_original_video(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.mp4"
            source.write_bytes(b"x" * 2000)
            with patch.object(pipeline, "probe", return_value={
                "duration": 45.0, "width": 1280, "height": 720
            }):
                with self.assertRaises(pipeline.VideoPipelineError):
                    pipeline.render_mp4(source, Path(directory) / "out.mp4", clip_seconds=65)

    def test_demo_submission_needs_no_upload(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            routes, "VIDEO_DIR", Path(directory)
        ), patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "test-secret"}):
            tasks = BackgroundTasks()
            created = routes.create_demo_job(
                background_tasks=tasks, clip_seconds=65,
                x_admin_token="test-secret",
            )
            self.assertEqual(created["state"], "queued")
            self.assertEqual(created["requested_duration_seconds"], 65)
            self.assertTrue(created["demo"])
            self.assertEqual(len(tasks.tasks), 1)
            job = routes._read_status(created["job_id"])
            self.assertEqual(job["source_type"], "original_synthetic")

    def test_demo_rejects_missing_token(self):
        with patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "test-secret"}):
            with self.assertRaises(HTTPException) as error:
                routes.create_demo_job(
                    background_tasks=BackgroundTasks(),
                    clip_seconds=65, x_admin_token="wrong"
                )
            self.assertEqual(error.exception.status_code, 401)

    def test_admin_required(self):
        with patch.dict(os.environ, {"POLL_ADMIN_TOKEN": ""}):
            with self.assertRaises(HTTPException) as error:
                routes._authorize(None)
            self.assertEqual(error.exception.status_code, 503)
        with patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "test-secret"}):
            with self.assertRaises(HTTPException) as error:
                routes._authorize("incorrect")
            self.assertEqual(error.exception.status_code, 401)
            routes._authorize("test-secret")

    def test_upload_requires_explicit_permission(self):
        with patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "test-secret"}):
            file = UploadFile(filename="video.mp4", file=io.BytesIO(b"x" * 2000))
            with self.assertRaises(HTTPException) as error:
                routes.create_video_job(
                    background_tasks=BackgroundTasks(),
                    video=file, authorized=False, caption_text="", clip_seconds=20,
                    x_admin_token="test-secret"
                )
            self.assertEqual(error.exception.status_code, 403)

    def test_job_id_path_validation(self):
        with self.assertRaises(HTTPException) as error:
            routes._job_dir("../../some-file")
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
