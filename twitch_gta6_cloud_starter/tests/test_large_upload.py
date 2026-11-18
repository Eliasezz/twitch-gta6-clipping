"""Regression tests for large-video uploads that avoid 413 requests."""
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, UploadFile

from app import large_upload
from app import video_routes


class ChunkUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.patches = [
            patch.object(video_routes, "VIDEO_DIR", self.folder),
            patch.dict(os.environ, {"POLL_ADMIN_TOKEN": "secret-for-test",
                                    "OPENAI_API_KEY": ""}),
        ]
        for context in self.patches:
            context.start()

    def tearDown(self):
        for context in reversed(self.patches):
            context.stop()
        self.tmp.cleanup()

    def create(self, size=4200, duration=65):
        data=large_upload.StartUpload(
            filename="my-authorized-video.mov",
            size_bytes=size,
            authorized=True,
            clip_seconds=duration,
            auto_subtitles=False,
        )
        return large_upload.start_upload(data, x_admin_token="secret-for-test")

    def send(self, job, index, data):
        return large_upload.upload_chunk(
            job_id=job, index=index,
            file=UploadFile(filename="part", file=io.BytesIO(data)),
            x_admin_token="secret-for-test",
        )

    def test_chunking_creates_job_and_finalizes_without_large_request(self):
        result=self.create(size=4200)
        self.assertEqual(result["chunk_size_bytes"],6*1024*1024)
        job=result["job_id"]
        self.send(job,0,b"1"*2400)
        with self.assertRaises(HTTPException) as missing:
            large_upload.finish_upload(job,BackgroundTasks(),x_admin_token="secret-for-test")
        self.assertEqual(missing.exception.status_code,409)
        next_result=self.send(job,1,b"2"*1800)
        self.assertEqual(next_result["upload_progress"],100)
        with patch.object(large_upload,"probe",return_value={
            "duration":70,"width":1920,"height":1080,"has_audio":True,
        }):
            tasks=BackgroundTasks()
            status=large_upload.finish_upload(job,tasks,x_admin_token="secret-for-test")
        self.assertEqual(status["state"],"queued")
        self.assertEqual(status["job_id"],job)
        self.assertEqual(len(tasks.tasks),1)
        self.assertEqual(video_routes._read_status(job)["state"],"queued")
        self.assertTrue((self.folder/job/"source.mp4").is_file())
        self.assertFalse((self.folder/job/"source.part").exists())

    def test_duplicate_chunk_is_safe(self):
        job=self.create(size=20)["job_id"]
        self.send(job,0,b"a"*10)
        duplicate=self.send(job,0,b"b"*10)
        self.assertTrue(duplicate["already_received"])
        self.assertEqual((self.folder/job/"source.part").read_bytes(),b"a"*10)
        self.assertEqual(duplicate["received_bytes"],10)

    def test_wrong_chunk_order_rejected(self):
        job=self.create(size=20)["job_id"]
        with self.assertRaises(HTTPException) as mismatch:
            self.send(job,2,b"a"*10)
        self.assertEqual(mismatch.exception.status_code,409)

    def test_oversized_chunk_rolls_back(self):
        job=self.create(size=7*1024*1024)["job_id"]
        with self.assertRaises(HTTPException) as large:
            self.send(job,0,b"x"*(large_upload._CHUNK_BYTES+1))
        self.assertEqual(large.exception.status_code,413)
        self.assertEqual(video_routes._read_status(job)["received_bytes"],0)
        self.assertEqual((self.folder/job/"source.part").stat().st_size,0)

    def test_unlicensed_or_invalid_token_rejected(self):
        with self.assertRaises(HTTPException) as denied:
            large_upload.start_upload(large_upload.StartUpload(
                filename="my-video.mp4",size_bytes=5000,authorized=False,
                auto_subtitles=False
            ),x_admin_token="secret-for-test")
        self.assertEqual(denied.exception.status_code,403)
        with self.assertRaises(HTTPException) as auth:
            large_upload.start_upload(large_upload.StartUpload(
                filename="my-video.mp4",size_bytes=5000,authorized=True,
                auto_subtitles=False
            ),x_admin_token="wrong")
        self.assertEqual(auth.exception.status_code,401)

    def test_larger_than_old_75mb_supported_in_chunks(self):
        self.assertGreater(large_upload._UPLOAD_LIMIT,75*1024*1024)
        large_request=self.create(size=85*1024*1024)
        self.assertEqual(large_request["state"],"uploading")
        self.assertEqual(large_request["max_upload_mb"],300)

    def test_existing_web_page_has_download_flow(self):
        html=large_upload.uploader_page()
        self.assertIn('id="video"',html)
        self.assertIn("export-link",html)
        self.assertIn("/video/uploads/",html)
        self.assertNotIn("localStorage",html)


if __name__=="__main__":
    unittest.main()
