"""Regression tests for the Twitch GTA VI radar (no network access required)."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from app import main as radar


class RadarTests(unittest.TestCase):
    def test_paginated_clip_collection(self):
        responses = [
            {"data": [{"id": "a"}, {"id": "b"}], "pagination": {"cursor": "next"}},
            {"data": [{"id": "c"}], "pagination": {}},
        ]
        with patch.object(radar, "twitch_get", side_effect=responses) as fetch:
            clips = radar.collect_clips_paginated({"game_id": "123"}, 3)
        self.assertEqual([c["id"] for c in clips], ["a", "b", "c"])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[0].args[1]["first"], 3)
        self.assertEqual(fetch.call_args_list[1].args[1]["first"], 1)
        self.assertEqual(fetch.call_args_list[1].args[1]["after"], "next")

    def test_no_pagination_when_cursor_missing(self):
        with patch.object(radar, "twitch_get", return_value={
            "data": [{"id": "a"}], "pagination": {}
        }) as fetch:
            self.assertEqual(len(radar.collect_clips_paginated({"game_id": "123"}, 100)), 1)
        self.assertEqual(fetch.call_count, 1)

    def test_eligibility_requires_creator_permission_and_views(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            radar, "DB_PATH", str(Path(directory) / "clips.sqlite3")
        ), patch.object(radar, "ALLOWED_BROADCASTER_IDS", {"allowed"}):
            radar.init_database()
            created_at = datetime.now(timezone.utc).isoformat()
            clips = [
                {"id": "allowed", "broadcaster_id": "allowed",
                 "created_at": created_at, "view_count": radar.MIN_VIEWS + 1},
                {"id": "not-allowed", "broadcaster_id": "other",
                 "created_at": created_at, "view_count": radar.MIN_VIEWS + 1},
                {"id": "low-views", "broadcaster_id": "allowed",
                 "created_at": created_at, "view_count": max(0, radar.MIN_VIEWS - 1)},
            ]
            saved, eligible = radar.save_clips(clips, {"allowed"})
            self.assertEqual(saved, 3)
            if radar.MIN_VIEWS > 0:
                self.assertEqual(eligible, 1)
            con = radar.get_connection()
            rows = {
                row["id"]: row["eligible_for_processing"]
                for row in con.execute("SELECT id, eligible_for_processing FROM clips")
            }
            con.close()
            self.assertEqual(rows["allowed"], 1)
            self.assertEqual(rows["not-allowed"], 0)
            if radar.MIN_VIEWS > 0:
                self.assertEqual(rows["low-views"], 0)
            # Legacy flags must not survive a restart before verification.
            radar.init_database()
            con = radar.get_connection()
            count = con.execute(
                "SELECT COUNT(*) FROM clips WHERE eligible_for_processing = 1"
            ).fetchone()[0]
            con.close()
            self.assertEqual(count, 0)

    def test_oauth_secret_is_never_sent_in_query_string(self):
        response = Mock()
        response.json.return_value = {"access_token": "stub", "expires_in": 3600}
        with patch.object(radar, "TWITCH_CLIENT_ID", "test_id"), patch.object(
            radar, "TWITCH_CLIENT_SECRET", "test_secret"
        ), patch.dict(radar.token_cache, {"access_token": None, "expires_at": 0}), patch(
            "app.main.requests.post", return_value=response
        ) as request:
            self.assertEqual(radar.get_twitch_token(), "stub")
        self.assertIn("data", request.call_args.kwargs)
        self.assertNotIn("params", request.call_args.kwargs)
        self.assertEqual(request.call_args.kwargs["data"]["client_secret"], "test_secret")

    def test_manual_poll_requires_admin_token(self):
        with patch.object(radar, "POLL_ADMIN_TOKEN", ""):
            with self.assertRaises(HTTPException) as error:
                radar.manual_poll(x_admin_token=None)
            self.assertEqual(error.exception.status_code, 403)
        with patch.object(radar, "POLL_ADMIN_TOKEN", "sample-token"):
            with self.assertRaises(HTTPException) as error:
                radar.manual_poll(x_admin_token="wrong")
            self.assertEqual(error.exception.status_code, 401)
            with patch.object(radar, "poll_once", return_value={"status": "success"}):
                self.assertEqual(
                    radar.manual_poll(x_admin_token="sample-token"),
                    {"status": "success"}
                )


if __name__ == "__main__":
    unittest.main()
