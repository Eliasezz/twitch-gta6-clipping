"""No extra transcription or CapCut API is required for the edit handoff."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from app.capcut_pack import ass_to_srt, editor_manifest
from app.video_exports import export_bundle


class CapCutPackTests(unittest.TestCase):
    def test_ass_to_srt_excludes_hook_and_keeps_actual_word_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            ass, srt = Path(directory) / "clip.ass", Path(directory) / "clip.srt"
            ass.write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.50,Hook,,0,0,0,,BIG REACTION\n"
                r"Dialogue: 0,0:00:01.50,0:00:02.00,Hype,,0,0,0,,{\c&H0000EFFF&}OH" "\n"
                r"Dialogue: 0,0:00:02.00,0:00:02.60,Hype,,0,0,0,,{\c&H00FFFFFF&}WOW" "\n",
                encoding="utf-8",
            )
            self.assertTrue(ass_to_srt(ass, srt))
            text = srt.read_text(encoding="utf-8")
            self.assertIn("00:00:01,500 --> 00:00:02,000", text)
            self.assertIn("OH", text)
            self.assertIn("WOW", text)
            self.assertNotIn("BIG REACTION", text)
            self.assertNotIn(r"{\c", text)

    def test_hook_only_has_no_invented_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            ass, srt = Path(directory) / "clip.ass", Path(directory) / "clip.srt"
            ass.write_text("Dialogue: 0,0:00:00.00,0:00:01.50,Hook,,0,0,0,,TITLE\n", encoding="utf-8")
            self.assertFalse(ass_to_srt(ass, srt))
            self.assertFalse(srt.exists())

    def test_manifest_and_zip_have_both_edited_clips(self):
        job_id = "00000000-0000-4000-8000-000000000002"
        status = {
            "state": "done", "authorized": True, "original_filename": "owned.mp4",
            "result": {"clips": [
                {"file": "vertical.mp4", "duration_seconds": 25,
                 "caption_mode": "english_word_synced", "editable_srt": True,
                 "source_shots": [{"start": 45.0, "duration": 1.2}]},
                {"file": "vertical-2.mp4", "duration_seconds": 25,
                 "caption_mode": "english_word_synced", "editable_srt": True,
                 "source_shots": [{"start": 95.0, "duration": 1.2}]},
            ]},
        }
        data = editor_manifest(job_id, status)
        self.assertEqual(len(data["clips"]), 2)
        self.assertEqual(data["clips"][1]["source_shots"][0]["start"], 95.0)
        with tempfile.TemporaryDirectory() as directory, patch(
                "app.video_exports.shutil.which", return_value=None):
            folder = Path(directory)
            (folder / "vertical.mp4").write_bytes(b"video" * 350)
            (folder / "vertical-2.mp4").write_bytes(b"video2" * 350)
            (folder / "vertical.srt").write_text("1\n00:00:00,000 --> 00:00:00,100\nWOW\n", encoding="utf-8")
            (folder / "vertical-2.srt").write_text("1\n00:00:00,000 --> 00:00:00,100\nNO WAY\n", encoding="utf-8")
            bundle = export_bundle(job_id, folder, status)
            with ZipFile(bundle) as archive:
                paths = archive.namelist()
                for name in ("montage_9x16.mp4", "montage_2_9x16.mp4",
                             "CapCut/clip_01.srt", "CapCut/clip_02.srt",
                             "CapCut/plan_montage.json"):
                    self.assertTrue(any(x.endswith("/" + name) for x in paths), name)


if __name__ == "__main__":
    unittest.main()
