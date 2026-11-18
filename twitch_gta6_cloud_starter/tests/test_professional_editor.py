"""Professional GTA VI editor contract: real ffmpeg, metadata, and edge cases."""
import io
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from app import media_integrity, rhythm, highlight_signals, video_pipeline, video_routes


FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def source_mp4(folder, *, seconds=7, audio=True, name="source.mp4"):
    target = Path(folder) / name
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=480x270:rate=24:duration={seconds}",
    ]
    if audio:
        command += [
            "-f", "lavfi", "-i", f"sine=frequency=620:sample_rate=44100:duration={seconds}"
        ]
    command += ["-c:v", "libx264", "-threads", "2", "-preset", "ultrafast"]
    if audio:
        command += ["-c:a", "aac", "-shortest"]
    else:
        command += ["-an"]
    command += [str(target)]
    subprocess.run(command, check=True, timeout=75, capture_output=True)
    return target


class ProfessionalEditorTests(unittest.TestCase):
    def test_01_timeline_has_actual_peak_hook_and_two_second_shots(self):
        shots = rhythm.plan_shots(start=100, duration=30, event=116)
        self.assertAlmostEqual(sum(length for _, length, _ in shots), 30, places=3)
        self.assertEqual(shots[0][2], "hook")
        self.assertEqual(shots[0][0], 116)
        self.assertLessEqual(shots[0][1], 1.5)
        self.assertGreaterEqual(len(shots), 12)
        self.assertTrue(all(0 < length <= 2.5 for _, length, _ in shots))
        self.assertTrue(all(100 <= start < 130 for start, _, _ in shots))

    def test_02_malformed_chat_is_ignored_and_never_crashes(self):
        with patch("app.highlight_signals._sample", return_value=b""):
            start, signal = highlight_signals.select_window(
                "not-opened.mp4", 60, 20, has_audio=False,
                chat_seconds=[None, {}, "oops", float("nan"), -5, 99, 2.0, 2.1, 2.4]
            )
        self.assertTrue(0 <= start <= 40)
        self.assertEqual(signal["chat_messages"], 3)

    def test_03_invalid_dimensions_and_short_source_raise(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(b"garbage")
            with patch.object(video_pipeline, "probe", return_value={
                "duration": 4.0, "width": 480, "height": 270,
                "has_audio": True,
            }):
                with self.assertRaises(video_pipeline.VideoPipelineError):
                    video_pipeline.render_mp4(
                        source, Path(folder) / "out.mp4", clip_seconds=30,
                    )
                with self.assertRaises(video_pipeline.VideoPipelineError):
                    video_pipeline.render_mp4(
                        source, Path(folder) / "out.mp4",
                        clip_seconds=3, width=1281, height=1920,
                    )

    def test_04_source_outside_job_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            raw = Path(root) / "unrelated.mp4"
            raw.write_bytes(b"an actual job must never read this")
            private = Path(root) / "my-job"
            private.mkdir()
            with self.assertRaises(video_pipeline.VideoPipelineError):
                video_pipeline.render_mp4(
                    raw, private / "vertical.mp4", clip_seconds=3
                )

    def test_05_larger_than_300mb_is_rejected_before_decoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oversized.mp4"
            with path.open("wb") as f:
                f.truncate(301 * 1024 * 1024)
            with self.assertRaises(media_integrity.IntegrityError):
                media_integrity.validate_source(path)

    def test_06_hook_respects_safe_zone_and_seven_words(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "caption.ass"
            self.assertTrue(video_pipeline.make_ass(
                "", 30, target, width=1080, height=1920,
                hook_text="THIS ONE MOMENT LEFT EVERYONE COMPLETELY STUNNED TODAY"
            ))
            content = target.read_text()
            self.assertIn("0:00:01.50", content)
            self.assertIn("576", content)  # 30% bottom-safe margin of 1920
            self.assertNotIn("TODAY", content)

    @unittest.skipUnless(FFMPEG, "ffmpeg unavailable")
    def test_07_real_rhythmic_edit_has_audio_and_no_other_media(self):
        with tempfile.TemporaryDirectory() as folder:
            src = source_mp4(folder, seconds=7)
            dest = Path(folder) / "vertical.mp4"
            detail = video_pipeline.render_mp4(
                src, dest, clip_seconds=5,
                width=360, height=640,
                dynamic_zoom=False, auto_subtitles=False, music_enabled=True,
                rhythmic=True, hook_text="GTA VI REACTION"
            )
            self.assertEqual(detail["selection_mode"], "rhythmic_source_cuts")
            self.assertEqual(detail["output_validation"]["video_codec"], "h264")
            self.assertEqual(detail["output_validation"]["audio_codec"], "aac")
            self.assertEqual(detail["output_validation"]["output_width"], 360)
            self.assertGreater(dest.stat().st_size, 4096)
            self.assertFalse(list(Path(folder).glob("shot-*.mp4")))

    @unittest.skipUnless(FFMPEG, "ffmpeg unavailable")
    def test_08_source_without_audio_has_valid_silent_aac(self):
        with tempfile.TemporaryDirectory() as folder:
            src = source_mp4(folder, seconds=4, audio=False)
            out = Path(folder) / "vertical.mp4"
            result = video_pipeline.render_mp4(
                src, out, clip_seconds=3, width=360, height=640,
                dynamic_zoom=False, music_enabled=False, auto_subtitles=False,
            )
            self.assertTrue(video_pipeline.probe(out)["has_audio"])
            self.assertTrue(result["output_validation"]["audio_codec"] == "aac")

    @unittest.skipUnless(FFMPEG, "ffmpeg unavailable")
    def test_09_optional_full_hd_is_real_1080x1920(self):
        with tempfile.TemporaryDirectory() as folder:
            src = source_mp4(folder, seconds=4)
            out = Path(folder) / "vertical.mp4"
            result = video_pipeline.render_mp4(
                src, out, clip_seconds=3,
                width=1080, height=1920, dynamic_zoom=False,
                auto_subtitles=False, music_enabled=False,
            )
            self.assertEqual(
                (result["output_validation"]["output_width"],
                 result["output_validation"]["output_height"]), (1080, 1920)
            )

    @unittest.skipUnless(FFMPEG, "ffmpeg unavailable")
    def test_10_corrupted_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            src = source_mp4(folder, seconds=4)
            out = Path(folder) / "vertical.mp4"
            video_pipeline.render_mp4(
                src, out, clip_seconds=3, width=360, height=640,
                dynamic_zoom=False,
            )
            out.write_bytes(b"corrupted")
            with self.assertRaises(media_integrity.IntegrityError):
                media_integrity.validate_mp4(
                    out, width=360, height=640, seconds=3
                )

    def test_11_wrong_job_output_is_not_downloaded(self):
        job_id = "05995e36-14dd-46b4-913f-0c2d1a0a8e62"
        with tempfile.TemporaryDirectory() as folder, patch.object(
            video_routes, "VIDEO_DIR", Path(folder)
        ), patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "test-token"}):
            job_folder = video_routes._job_dir(job_id)
            video_routes._write_status(job_folder, {
                "job_id": job_id, "state": "done",
                "result": {"output_validation": {"output_sha256": "f" * 64}},
            })
            (job_folder / "vertical.mp4").write_bytes(b"not the verified output")
            with self.assertRaises(HTTPException) as error:
                video_routes.download_video_job(job_id, x_admin_token="test-token")
            self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
