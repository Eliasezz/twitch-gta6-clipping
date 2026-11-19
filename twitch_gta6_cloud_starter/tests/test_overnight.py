"""Overnight Twitch editor smoke tests: consent gate, OAuth, private MP4 ZIP.

No actual Twitch clips are downloaded or Metricool posts created by these tests.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from app import main, overnight, twitch_connection, twitch_media, video_routes


class OvernightEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "AUTO_PIPELINE_ENABLED": "1",
            "ALLOWED_BROADCASTER_IDS": "",
            "TWITCH_EDITOR_ID": "",
            "TWITCH_EDITOR_ACCESS_TOKEN": "",
            "POLL_ADMIN_TOKEN": "private-test-token",
            "OPENAI_API_KEY": "",
            "METRICOOL_API_TOKEN": "",
            "METRICOOL_AUTO_PUBLISH": "0",
            "RENDER_EXTERNAL_URL": "https://twitch-gta6-clipping.onrender.com",
        })
        self.env.start()
        self.patch_db = patch.object(main, "DB_PATH", str(self.root / "clips.sqlite3"))
        self.patch_client = patch.object(main, "TWITCH_CLIENT_ID", "mock-client-id")
        self.patch_secret = patch.object(main, "TWITCH_CLIENT_SECRET", "mock-client-secret")
        self.patch_dir = patch.object(video_routes, "VIDEO_DIR", self.root / "media")
        for f in (self.patch_db, self.patch_client, self.patch_secret, self.patch_dir):
            f.start()
        main.init_database()
        twitch_connection.init_connection_db()
        overnight.init_overnight_db()

    def tearDown(self):
        for f in (self.patch_dir, self.patch_secret, self.patch_client, self.patch_db):
            f.stop()
        self.env.stop()
        self.temp.cleanup()

    def _add_clip(self):
        db = main.get_connection()
        try:
            db.execute("""
                INSERT INTO clips (id, broadcaster_id,broadcaster_name,title,
                                   url,view_count,created_at,eligible_for_processing)
                VALUES ('abc-123','456','Test Creator','Wow new GTA!',
                        'https://clips.twitch.tv/test-clip',1000,
                        '2026-11-19T11:00:00+00:00',0)
            """)
            db.commit()
        finally:
            db.close()

    def _connect(self):
        db = main.get_connection()
        try:
            db.execute("""
                INSERT INTO twitch_connected_editors (id,login,granted_at)
                VALUES ('789','test_editor','2026-11-19T12:00:00+00:00')
            """)
            db.execute("""
                INSERT INTO twitch_authorized_creators
                (broadcaster_id,login,attested_at)
                VALUES ('456','testcreator','2026-11-19T12:00:00+00:00')
            """)
            db.commit()
        finally:
            db.close()

    def test_01_no_consent_or_oauth_means_no_download(self):
        status = overnight.readiness()
        self.assertFalse(status["ready_for_automatic_editing"])
        self.assertFalse(status["checks"]["reuse_authorizations_present"])
        self.assertFalse(status["checks"]["twitch_editor_oauth_grant_present"])
        with patch("app.overnight.twitch_media.download_clip") as download:
            result = overnight.run_once()
        self.assertEqual(result["status"], "blocked")
        download.assert_not_called()

    def test_02_metricool_not_required_for_overview(self):
        self._connect()
        state = overnight.readiness()
        self.assertTrue(state["ready_for_automatic_editing"])
        self.assertTrue(state["metricool_not_required_for_editing"])
        self.assertFalse(bool(os.getenv("METRICOOL_API_TOKEN")))

    def test_03_existing_radar_clips_are_eligible_after_creator_attestation(self):
        self._add_clip()
        self._connect()
        clips = overnight._candidates()
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["id"], "abc-123")

    def test_04_real_overnight_workflow_creates_private_zip_without_metricool(self):
        self._add_clip()
        self._connect()

        def fake_download(broadcaster, clip_id, destination, **kwargs):
            self.assertEqual((broadcaster, clip_id), ("456", "abc-123"))
            Path(destination).write_bytes(b"licensed clip" * 2000)
            return destination

        def fake_worker(job_id, text, seconds, **kwargs):
            folder = video_routes._job_dir(job_id)
            (folder / "vertical.mp4").write_bytes(b"rendered source" * 2000)
            status = video_routes._read_status(job_id)
            status.update({"state": "done", "result": {
                "job_id": job_id, "file": "vertical.mp4",
                "duration_seconds": float(seconds), "caption_mode": "not_available",
            }})
            video_routes._write_status(folder, status)

        with patch("app.overnight.twitch_media.download_clip",
                   side_effect=fake_download), patch(
            "app.overnight.probe", return_value={"duration": 39.0}
        ), patch("app.video_routes._worker", side_effect=fake_worker), patch(
            "app.video_exports.shutil.which", return_value=None
        ), patch("app.overnight.shutil.disk_usage") as disk:
            disk.return_value.free = 700 * 1024 * 1024
            output = overnight.run_once()

        self.assertEqual(output["status"], "ready_to_download")
        self.assertEqual(output["duration_seconds"], 30)
        job_id = output["job_id"]
        folder = video_routes._job_dir(job_id)
        self.assertTrue((folder / "ready-to-publish.zip").is_file())
        self.assertTrue((folder / "vertical.mp4").is_file())
        jobs = overnight.overnight_jobs(x_admin_token="private-test-token")
        self.assertEqual(jobs["jobs"][0]["job_id"], job_id)
        self.assertTrue(jobs["jobs"][0]["export_link_endpoint"].endswith("/export-link"))
        self.assertEqual(overnight.run_once()["status"], "no_new_permissioned_clips")

    def test_05_creator_attestation_is_required_and_revocable(self):
        data = twitch_connection.CreatorConsent(
            login="testcreator", reuse_permission_confirmed=False
        )
        with self.assertRaises(HTTPException) as denied:
            twitch_connection.register_creator(data, x_admin_token="private-test-token")
        self.assertEqual(denied.exception.status_code, 403)

        response = Mock(ok=True)
        response.json.return_value = {"data": [{"id": "456", "login": "testcreator"}]}
        with patch("app.main.twitch_get", return_value=response.json.return_value):
            result = twitch_connection.register_creator(
                twitch_connection.CreatorConsent(
                    login="testcreator", reuse_permission_confirmed=True
                ), x_admin_token="private-test-token"
            )
        self.assertEqual(result["broadcaster_id"], "456")
        self.assertTrue(twitch_media.permitted_broadcaster("456"))
        twitch_connection.revoke_creator("456", x_admin_token="private-test-token")
        self.assertFalse(twitch_media.permitted_broadcaster("456"))

    def test_06_twitch_oauth_url_requires_private_admin_token(self):
        with self.assertRaises(HTTPException):
            twitch_connection.initiate_twitch_oauth(x_admin_token="wrong")
        with patch.dict(os.environ, {
            "TWITCH_CLIENT_ID": "cid",
            "TWITCH_CLIENT_SECRET": "csecret",
        }):
            result = twitch_connection.initiate_twitch_oauth(
                x_admin_token="private-test-token"
            )
        self.assertTrue(result["authorize_url"].startswith("https://id.twitch.tv/oauth2/authorize?"))
        self.assertIn("editor%3Amanage%3Aclips", result["authorize_url"])
        self.assertIn("/automation/twitch/callback", result["required_redirect_uri"])
        self.assertNotIn("csecret", json.dumps(result))

    def test_07_existing_env_credentials_still_take_priority(self):
        with patch.dict(os.environ, {
            "TWITCH_EDITOR_ID": "100",
            "TWITCH_EDITOR_ACCESS_TOKEN": "token",
            "TWITCH_CLIENT_ID": "app",
        }):
            self.assertEqual(twitch_media._editor_settings(),
                             ("100", "token", "app"))

    def test_08_twitch_app_access_after_oauth_grant(self):
        self._connect()
        with patch.dict(os.environ, {
            "TWITCH_CLIENT_ID": "some-app-id",
        }), patch("app.main.get_twitch_token", return_value="app-token"):
            self.assertEqual(twitch_media._editor_settings(),
                             ("789", "app-token", "some-app-id"))


if __name__ == "__main__":
    unittest.main()
