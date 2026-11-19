import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from app import rhythm, video_pipeline, video_routes


class ShortEditTests(unittest.TestCase):
    def test_peak_first_and_silence_excluded(self):
        shots = rhythm.plan([(0, 8), (14, 32)], 18, 22)
        self.assertLessEqual(shots[0][1], 1.5)
        self.assertAlmostEqual(sum(d for _, d in shots), 22)
        self.assertAlmostEqual(sum(shots[-1]), shots[0][0])
        for start, length in shots:
            self.assertLessEqual(length, 3)
            self.assertTrue(start + length <= 8 or start >= 14)
        with self.assertRaises(video_pipeline.VideoPipelineError):
            rhythm.plan([], 0, 22)

    def test_job_cannot_use_neighbor_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(video_routes, "VIDEO_DIR", Path(tmp)):
            neighbor = Path(tmp) / str(uuid.uuid4())
            neighbor.mkdir()
            (neighbor / "source.mp4").write_bytes(b"other job")
            job = str(uuid.uuid4())
            folder = Path(tmp) / job
            video_routes._write_status(folder, {"state": "queued", "shorts": True})
            video_routes._worker(job, "", 22)
            state = video_routes._read_status(job)
            self.assertEqual(state["state"], "failed")
            self.assertIn("introuvable", state["error_detail"])
            self.assertFalse((folder / "vertical.mp4").exists())
