"""Tests for Mac-downloadable edited-video publishing packages."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from fastapi import HTTPException

from app import video_exports, video_routes


class ExportTests(unittest.TestCase):
    def test_package_has_real_mp4_and_both_social_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "job"
            folder.mkdir()
            (folder / "vertical.mp4").write_bytes(b"safe-video" * 350)
            state = {
                "state": "done", "authorized": True, "demo": False,
                "original_filename": "authorized-stream.mp4",
            }
            with patch("app.video_exports.shutil.which", return_value=None):
                bundle = video_exports.export_bundle(
                    "00000000-0000-4000-8000-000000000001", folder, state
                )
            self.assertTrue(bundle.is_file())
            with ZipFile(bundle) as z:
                names = z.namelist()
                self.assertEqual(len(names), 6)
                self.assertTrue(any(n.endswith("/montage_9x16.mp4") for n in names))
                self.assertTrue(any(n.endswith("/tiktok_description.txt") for n in names))
                self.assertTrue(any(n.endswith("/youtube_description.txt") for n in names))
                self.assertTrue(any(n.endswith("/INSTRUCTIONS_FR.txt") for n in names))
                meta = json.loads(z.read(next(n for n in names if n.endswith("/publication.json"))))
                self.assertTrue(meta["ready"])

    def test_demo_package_is_explicitly_non_publishable(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "demo"
            folder.mkdir()
            (folder / "vertical.mp4").write_bytes(b"video" * 300)
            with patch("app.video_exports.shutil.which", return_value=None):
                result = video_exports.export_bundle("demo", folder, {
                    "state": "done", "demo": True,
                    "source_type": "original_synthetic",
                })
            with ZipFile(result) as z:
                meta = json.loads(z.read(next(n for n in z.namelist()
                                              if n.endswith("/publication.json"))))
                self.assertFalse(meta["ready"])
                self.assertIn("DO NOT PUBLISH", meta["title"])

    def test_private_link_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            video_routes, "VIDEO_DIR", Path(tmp)
        ), patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "test-token"}), patch(
            "app.video_exports.shutil.which", return_value=None
        ):
            job_id = "00000000-0000-4000-8000-000000000001"
            folder = video_routes._job_dir(job_id)
            video_routes._write_status(folder, {
                "job_id": job_id, "state": "done", "demo": False, "authorized": True
            })
            (folder / "vertical.mp4").write_bytes(b"file" * 350)
            link = video_routes.create_export_link(job_id, x_admin_token="test-token")
            from urllib.parse import urlsplit, parse_qs
            p = urlsplit(link["url"])
            params = parse_qs(p.query)
            valid = video_routes.download_export_link(
                job_id, expires=int(params["expires"][0]),
                signature=params["signature"][0]
            )
            self.assertTrue(valid.path.name.endswith(".zip"))
            with self.assertRaises(HTTPException) as failure:
                video_routes.download_export_link(
                    job_id, expires=int(params["expires"][0]),
                    signature="f" * 64
                )
            self.assertEqual(failure.exception.status_code, 403)
            with self.assertRaises(HTTPException) as failure:
                video_routes.create_export_link(job_id, x_admin_token="wrong")
            self.assertEqual(failure.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
