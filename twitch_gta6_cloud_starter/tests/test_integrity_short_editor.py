"""Real ffmpeg smoke tests and five-plus edge cases for isolated editor jobs."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from app import video_pipeline, video_routes, media_integrity, highlight_signals


FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def create_source(folder, *, seconds=23, audio=True):
    source = Path(folder) / "source.mp4"
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=480x270:rate=24:duration={seconds}",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=600:duration={seconds}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "2"]
    command += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    command += [str(source)]
    subprocess.run(command, check=True, capture_output=True, timeout=90)
    return source


class IntegrityAndEditorTests(unittest.TestCase):
    def test_malformed_chat_values_are_discarded(self):
        with patch("app.highlight_signals._sample", return_value=b""):
            start, analysis = highlight_signals.select_window(
                "not-opened", 60, 20, False,
                chat_seconds=[1.0, 1.3, 1.5, None, {}, "bad", -1, float("nan"), 99],
            )
        self.assertEqual(analysis["chat_messages"], 3)
        self.assertTrue(0 <= start <= 40)

    def test_outside_job_source_is_never_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "other-video.mp4"
            source.write_bytes(b"must never load files outside job")
            folder = Path(tmp) / "job"
            folder.mkdir()
            with self.assertRaises(video_pipeline.VideoPipelineError):
                video_pipeline.render_mp4(
                    source, folder / "vertical.mp4", clip_seconds=20,
                )

    def test_input_bigger_than_300_megabytes_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "source.mp4"
            with file.open("wb") as stream:
                stream.truncate(301 * 1024 * 1024)
            with self.assertRaises(media_integrity.IntegrityError):
                media_integrity.validate_source(file)

    def test_short_source_cannot_fake_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "source.mp4"
            file.write_bytes(b"not read because probe is mocked")
            with patch.object(video_pipeline, "probe", return_value={
                "duration": 9, "width": 480, "height": 270, "has_audio": True
            }):
                with self.assertRaises(video_pipeline.VideoPipelineError):
                    video_pipeline.render_mp4(
                        file, Path(tmp) / "vertical.mp4", clip_seconds=20,
                    )

    def test_invalid_media_detected_before_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "source.mp4"
            file.write_bytes(b"corrupt media")
            with self.assertRaises(media_integrity.IntegrityError):
                media_integrity.validate_source(file)

    def test_wrong_job_sha_is_never_downloaded(self):
        job = "05995e36-14dd-46b4-913f-0c2d1a0a8e62"
        with tempfile.TemporaryDirectory() as root, patch.object(
            video_routes, "VIDEO_DIR", Path(root)
        ), patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "example-secret"}):
            folder = video_routes._job_dir(job)
            video_routes._write_status(folder, {
                "job_id": job, "state": "done",
                "result": {"output_validation": {"output_sha256": "0" * 64}},
            })
            (folder / "vertical.mp4").write_bytes(b"file tampered after validation")
            with self.assertRaises(HTTPException) as err:
                video_routes.download_video_job(job, x_admin_token="example-secret")
            self.assertEqual(err.exception.status_code, 409)

    @unittest.skipUnless(FFMPEG, "Requires ffmpeg")
    def test_nominal_source_only_rhythm_webcam_20s(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = create_source(tmp, seconds=23, audio=True)
            output = Path(tmp) / "vertical.mp4"
            result = video_pipeline.render_mp4(
                source, output, clip_seconds=20, width=360, height=640,
                rhythmic=True, face_bottom_left=True, dynamic_zoom=False,
                auto_subtitles=False, music_enabled=True, hook_text="GTA VI REACTION",
            )
            self.assertTrue(output.is_file())
            self.assertEqual(result["selection_mode"], "rhythmic_shorts")
            self.assertEqual(result["output_validation"]["video_codec"], "h264")
            self.assertEqual(result["output_validation"]["audio_codec"], "aac")
            self.assertEqual(result["output_validation"]["output_width"], 360)
            self.assertGreater(len(result["source_shots"]), 3)

    @unittest.skipUnless(FFMPEG, "Requires ffmpeg")
    def test_no_audio_source_still_has_aac(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = create_source(tmp, seconds=4, audio=False)
            output = Path(tmp) / "vertical.mp4"
            data = video_pipeline.render_mp4(
                source, output, clip_seconds=3, width=360, height=640,
                dynamic_zoom=False, music_enabled=False,
            )
            self.assertEqual(data["output_validation"]["audio_codec"], "aac")
            self.assertTrue(video_pipeline.probe(output)["has_audio"])

    @unittest.skipUnless(FFMPEG, "Requires ffmpeg")
    def test_optional_full_hd_1080_1920_is_real(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = create_source(tmp, seconds=4, audio=True)
            output = Path(tmp) / "vertical.mp4"
            data = video_pipeline.render_mp4(
                source, output, clip_seconds=3,
                width=1080, height=1920, dynamic_zoom=False,
            )
            self.assertEqual(
                (data["output_validation"]["output_width"],
                 data["output_validation"]["output_height"]), (1080, 1920)
            )

    @unittest.skipUnless(FFMPEG, "Requires ffmpeg")
    def test_modified_output_fails_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = create_source(tmp, seconds=4)
            output = Path(tmp) / "vertical.mp4"
            video_pipeline.render_mp4(
                source, output, clip_seconds=3,
                width=360, height=640, dynamic_zoom=False,
            )
            output.write_bytes(b"not a video anymore")
            with self.assertRaises(media_integrity.IntegrityError):
                media_integrity.validate_mp4(
                    output, width=360, height=640, seconds=3,
                )


if __name__ == "__main__":
    unittest.main()
