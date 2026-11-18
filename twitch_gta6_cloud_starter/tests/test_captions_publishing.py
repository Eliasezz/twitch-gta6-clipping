"""Offline checks for real caption timing and review-only publishing handoff."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, UploadFile

from app import captions
from app import highlight
from app import publishing
from app import video_pipeline
from app import video_routes


class CaptionTests(unittest.TestCase):
    def test_whisper_english_real_word_timestamps(self):
        transcription = {
            "language": "english", "words": [
                {"word": "That", "start": 0.10, "end": 0.60},
                {"word": "is", "start": 0.65, "end": 0.89},
                {"word": "wild!", "start": 0.95, "end": 1.38},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            captions, "_run_ffmpeg_audio"
        ), patch.object(captions, "_request_whisper", return_value=transcription) as request:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"not a real media file")
            words, mode, language = captions.transcribe_english(
                source, start=15, duration=3, scratch_dir=Path(tmp)
            )
        self.assertEqual(mode, "english_word_synced")
        self.assertEqual(language, "english")
        self.assertEqual(len(words), 3)
        self.assertAlmostEqual(words[0]["start"], .10)
        self.assertEqual(request.call_count, 1)

    def test_translation_timing_is_explicitly_segment_approximate(self):
        transcription = {"language": "french", "text": "Incroyable"}
        translation = {"segments": [
            {"start": .5, "end": 2.0, "text": "This is incredible"}
        ]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            captions, "_run_ffmpeg_audio"
        ), patch.object(captions, "_request_whisper",
                        side_effect=[transcription, translation]) as request:
            words, mode, language = captions.transcribe_english(
                Path(tmp) / "source.mp4", duration=3, scratch_dir=Path(tmp)
            )
        self.assertEqual(mode, "english_translated_segment_timed")
        self.assertEqual(language, "french")
        self.assertEqual([w["word"] for w in words], ["This", "is", "incredible"])
        self.assertEqual(request.call_count, 2)

    def test_ass_renders_word_highlight_at_real_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            ass = Path(tmp) / "words.ass"
            words = [
                {"word": "WOW", "start": .12, "end": .49},
                {"word": "YES", "start": .52, "end": .88},
            ]
            ok = video_pipeline.make_ass("", 3.0, ass, 360, 640, timed_words=words)
            data = ass.read_text(encoding="utf-8")
        self.assertTrue(ok)
        self.assertEqual(data.count("Dialogue: 0,"), 2)
        self.assertIn("0:00:00.12", data)
        self.assertIn("0:00:00.52", data)
        self.assertIn("H0000EFFF", data)

    def test_keyword_window_prefers_excited_reaction(self):
        words = [
            {"word": "hello", "start": 2, "end": 2.4},
            {"word": "everyone", "start": 5, "end": 5.4},
            {"word": "no", "start": 67, "end": 67.2},
            {"word": "way", "start": 67.3, "end": 67.6},
            {"word": "wow", "start": 69, "end": 69.5},
            {"word": "insane", "start": 71, "end": 71.5},
        ]
        start, score = highlight.choose_speech_window(
            words, source_seconds=140, clip_seconds=65
        )
        self.assertGreater(start, 0)
        self.assertLessEqual(start, 67)
        self.assertGreater(score["reaction_score"], 0)
        aligned = highlight.window_words(words, start, 65)
        self.assertTrue(all(0 <= w["start"] < 65 for w in aligned))

    def test_asr_requires_key_before_video_upload(self):
        with patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "admin", "OPENAI_API_KEY": ""}):
            file = UploadFile(filename="example.mp4", file=io.BytesIO(b"file"))
            with self.assertRaises(HTTPException) as e:
                video_routes.create_video_job(
                    background_tasks=BackgroundTasks(),
                    video=file, authorized=True, caption_text="",
                    auto_subtitles=True, clip_seconds=65,
                    x_admin_token="admin",
                )
            self.assertEqual(e.exception.status_code, 503)


class PublishTests(unittest.TestCase):
    def test_signed_link_expiry_and_tamper(self):
        with patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://example.org"}):
            link = publishing.sign_share_link(
                "00000000-0000-4000-8000-000000000001",
                "admin-secret", lifetime_seconds=3600, now=100
            )
        self.assertTrue(link["url"].startswith("https://example.org/video/shared/"))
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(link["url"]).query)
        signature = params["signature"][0]
        expires = int(params["expires"][0])
        id_ = "00000000-0000-4000-8000-000000000001"
        self.assertTrue(publishing.verify_share_link(id_, "admin-secret", expires, signature, now=100))
        self.assertFalse(publishing.verify_share_link(id_, "admin-secret", expires, signature, now=expires))
        self.assertFalse(publishing.verify_share_link(id_, "wrong", expires, signature, now=100))
        self.assertFalse(publishing.verify_share_link(id_, "admin-secret", expires, "X" * 64, now=100))

    def test_approval_requires_real_licensed_video_and_review(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            video_routes, "VIDEO_DIR", Path(tmp)
        ), patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "admin-secret"}):
            job_id = "00000000-0000-4000-8000-000000000001"
            folder = video_routes._job_dir(job_id)
            video_routes._write_status(folder, {
                "job_id": job_id, "state": "done", "authorized": True,
                "original_filename": "licensed.mp4",
            })
            (folder / "vertical.mp4").write_bytes(b"x" * 4096)
            with self.assertRaises(HTTPException) as e:
                video_routes.approve_publication(
                    job_id, rights_confirmed=True, editorial_reviewed=False,
                    x_admin_token="admin-secret"
                )
            self.assertEqual(e.exception.status_code, 403)
            result = video_routes.approve_publication(
                job_id, rights_confirmed=True, editorial_reviewed=True,
                x_admin_token="admin-secret"
            )
            self.assertTrue(result["ready"])
            self.assertFalse(result["automatic_publication"])
            self.assertIn("/video/shared/", result["signed_media_url"])
            self.assertEqual(video_routes._read_status(job_id)["publication_status"],
                             "approved_for_scheduling")

    def test_synthetic_demos_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            video_routes, "VIDEO_DIR", Path(tmp)
        ), patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "admin-secret"}):
            job_id = "00000000-0000-4000-8000-000000000001"
            video_routes._write_status(video_routes._job_dir(job_id), {
                "job_id": job_id, "state": "done", "authorized": True, "demo": True
            })
            with self.assertRaises(HTTPException) as error:
                video_routes.approve_publication(
                    job_id, rights_confirmed=True, editorial_reviewed=True,
                    x_admin_token="admin-secret"
                )
            self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
