"""Overnight Twitch -> 30s vertical MP4 + private ZIP (never require Metricool).

Every 15-minute radar poll may process one NEW official, permissioned Twitch
clip. No direct Twitch CDN scraping. No unlicensed VOD downloads. No media
from other jobs. Publication is deliberately separate and manual by default.
"""
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from . import twitch_media
from .twitch_connection import allowed_creators, editor_connection, allowed_creator_details
from .captions import openai_auth_status
from .video_pipeline import probe

router = APIRouter(prefix="/automation", tags=["overnight-licensed-editor"])
_run_lock = threading.Lock()


def _db():
    from .main import get_connection
    return get_connection()


def init_overnight_db():
    db = _db()
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS overnight_clip_jobs (
            clip_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            broadcaster_id TEXT NOT NULL,
            title TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS overnight_created ON overnight_clip_jobs(created_at)")
        db.commit()
    finally:
        db.close()


def _limit():
    return min(8, max(1, int(os.getenv("AUTO_EDITS_PER_DAY", "6"))))


def readiness():
    authorized = allowed_creators()
    user_editor_id = os.getenv("TWITCH_EDITOR_ID", "").strip()
    user_oauth = os.getenv("TWITCH_EDITOR_ACCESS_TOKEN", "").strip()
    connected = editor_connection()
    has_oauth = bool(user_editor_id and user_oauth) or connected is not None
    from .main import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, POLL_INTERVAL
    checks = {
        "nightly_editor_enabled": os.getenv("AUTO_PIPELINE_ENABLED", "0") == "1",
        "twitch_application_configured": bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET),
        "reuse_authorizations_present": bool(authorized),
        "twitch_editor_oauth_grant_present": has_oauth,
    }
    return {
        "ready_for_automatic_editing": all(checks.values()),
        "checks": checks,
        "creator_count": len(authorized),
        "authorized_creators": allowed_creator_details(),
        "twitch_connected_editor": connected["login"] if connected else None,
        "radar_interval_seconds": POLL_INTERVAL,
        "edits_per_day_limit": _limit(),
        "clip_target_seconds": 30,
        "clip_duration_range_seconds": [20, 45],
        "download": "GET /automation/overnight/jobs requires X-Admin-Token",
        "transcription_ready_to_attempt": openai_auth_status()["configured"],
        "metricool_not_required_for_editing": True,
        "publication": "manual_review_then_upload; separate Metricool API if configured",
        "vod_full_replay": "not_implemented; official Twitch Clip downloads only",
        "selection": "Twitch clip view count followed by actual audio/visual highlight analysis",
        "render_style": "short-form hook, 9:16 blurred background, colored real-word subtitles when ASR works",
    }


def _candidates(limit=20):
    from .main import MIN_VIEWS
    allowed = allowed_creators()
    if not allowed:
        return []
    placeholders = ",".join("?" for _ in allowed)
    db = _db()
    try:
        rows = db.execute(f"""
          SELECT c.id, c.title, c.url, c.broadcaster_name, c.broadcaster_id,
                 c.view_count, c.created_at
          FROM clips c
          LEFT JOIN overnight_clip_jobs used ON used.clip_id=c.id
          WHERE c.broadcaster_id IN ({placeholders})
            AND COALESCE(c.view_count,0) >= ?
            AND used.clip_id IS NULL
          ORDER BY c.view_count DESC, c.created_at DESC
          LIMIT ?
        """, (*sorted(allowed), MIN_VIEWS, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def _used_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    db = _db()
    try:
        count = db.execute("""
          SELECT COUNT(*) FROM overnight_clip_jobs WHERE created_at>=?
        """, (today,)).fetchone()[0]
        return int(count)
    finally:
        db.close()


def _record(clip, job_id, state, error=None):
    now = datetime.now(timezone.utc).isoformat()
    db = _db()
    try:
        db.execute("""
          INSERT INTO overnight_clip_jobs
          (clip_id,job_id,broadcaster_id,title,state,created_at,updated_at,error)
          VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(clip_id) DO UPDATE SET
            job_id=excluded.job_id,state=excluded.state,
            updated_at=excluded.updated_at,error=excluded.error
        """, (clip["id"], job_id, clip["broadcaster_id"],
              str(clip.get("title") or "")[:140], state, now, now, error))
        db.commit()
    finally:
        db.close()


def run_once():
    """Run one authorized clip edit per pass and persist a downloadable ZIP."""
    from .video_routes import VIDEO_DIR, _job_dir, _write_status, _read_status, _worker
    from .video_exports import export_bundle
    checks = readiness()
    if not checks["ready_for_automatic_editing"]:
        return {"status": "blocked", "checks": checks["checks"]}
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running"}
    job_id = None
    clip = None
    try:
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        if _used_today() >= _limit():
            return {"status": "daily_edit_limit_reached", "limit": _limit()}
        if shutil.disk_usage(VIDEO_DIR).free < 250 * 1024 * 1024:
            return {"status": "insufficient_disk_space", "minimum_free_mb": 250}
        candidates = _candidates()
        if not candidates:
            return {"status": "no_new_permissioned_clips"}
        clip = candidates[0]
        job_id = str(uuid.uuid4())
        _record(clip, job_id, "downloading")
        folder = _job_dir(job_id)
        folder.mkdir(parents=True, exist_ok=False)
        src = folder / "source.mp4"
        _write_status(folder, {
            "job_id": job_id, "state": "queued",
            "stage": "downloading_official_twitch_clip", "progress": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "authorized": True, "autogenerated": True,
            "source_type": "official_twitch_clip",
            "source_clip_ids": [clip["id"]],
            "broadcaster_id": clip["broadcaster_id"],
            "broadcaster_name": clip["broadcaster_name"],
            "publication_title": "GTA VI | " + str(clip.get("title") or "Reaction")[:70],
            "clip_count": 1, "rhythmic": True,
            "face_bottom_left": False,
            "shorts": False,
            "auto_subtitles_requested": openai_auth_status()["configured"],
            "music_original_generated": True,
            "requested_duration_seconds": 30,
            "publication_status": "review_required",
        })
        twitch_media.download_clip(clip["broadcaster_id"], clip["id"], src,
                                   max_bytes=90 * 1024 * 1024)
        length = probe(src)["duration"]
        if length < 20.1:
            raise ValueError("Twitch clip shorter than 20 seconds")
        seconds = min(30, int(length - 0.2))
        status = _read_status(job_id)
        status["requested_duration_seconds"] = seconds
        _write_status(folder, status)
        _record(clip, job_id, "editing")
        # Keep 540x960 on the 512MB Render service; do not claim 1080p detail.
        _worker(job_id, "", seconds, width=540, height=960,
                auto_subtitles=openai_auth_status()["configured"],
                music_enabled=True, hook_text="GTA VI | BIG REACTION")
        state = _read_status(job_id)
        if state.get("state") != "done":
            _record(clip, job_id, "failed", str(state.get("error") or "render failed"))
            return {"status": "editing_failed", "job_id": job_id,
                    "error": state.get("error_detail") or state.get("error")}
        archive = export_bundle(job_id, folder, state)
        _record(clip, job_id, "done")
        print("Overnight Twitch clip ready:", job_id, clip["id"], seconds, archive.stat().st_size)
        return {"status": "ready_to_download", "job_id": job_id,
                "duration_seconds": seconds,
                "file": "ready-to-publish.zip"}
    except Exception as exc:
        if clip is not None and job_id is not None:
            _record(clip, job_id, "failed", type(exc).__name__)
        print("Overnight licensed clip failed:", type(exc).__name__)
        return {"status": "failed", "job_id": job_id, "error": type(exc).__name__}
    finally:
        _run_lock.release()


@router.get("/overnight/status")
def overnight_status():
    status = readiness()
    status["edits_today"] = _used_today()
    status["potential_next_clips"] = len(_candidates(limit=10))
    return status


@router.get("/overnight/shortlist")
def clip_shortlist(x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize
    _authorize(x_admin_token)
    return {
        "description": "Authorized Twitch clips ranked by view count (not actual emotion measurements)",
        "candidates": _candidates(20),
    }


@router.get("/overnight/jobs")
def overnight_jobs(x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize, _read_status
    _authorize(x_admin_token)
    db = _db()
    try:
        rows = db.execute("""
          SELECT clip_id,job_id,broadcaster_id,title,state,created_at,error
          FROM overnight_clip_jobs ORDER BY created_at DESC LIMIT 50
        """).fetchall()
        data = [dict(row) for row in rows]
    finally:
        db.close()
    for item in data:
        try:
            saved = _read_status(item["job_id"])
            item["state"] = saved.get("state", item["state"])
            item["progress"] = saved.get("progress", 0)
            item["transcription_warning"] = saved.get("transcription_warning")
            if saved.get("state") == "done":
                item["export_link_endpoint"] = (
                    f"/video/jobs/{item['job_id']}/export-link"
                )
        except HTTPException:
            item["source_expired"] = True
    return {"jobs": data, "count": len(data)}


@router.post("/overnight/run", status_code=202)
def overnight_run(background_tasks: BackgroundTasks,
                  x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize
    _authorize(x_admin_token)
    if not readiness()["ready_for_automatic_editing"]:
        raise HTTPException(409, "Configure authorized creators and Twitch editor OAuth")
    background_tasks.add_task(run_once)
    return {"status": "queued", "action": "edit_one_authorized_twitch_clip"}


def run_overnight_after_poll():
    if os.getenv("AUTO_PIPELINE_ENABLED", "0") == "1":
        return run_once()
    return {"status": "disabled"}
