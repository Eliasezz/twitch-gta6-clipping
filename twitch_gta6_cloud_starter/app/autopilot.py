"""Permission-gated Twitch -> FFmpeg -> ASR -> Metricool automation.

All irreversible publishing is disabled unless:
- the creator is in ALLOWED_BROADCASTER_IDS;
- an authorized Twitch editor granted an official OAuth token;
- OpenAI transcription is explicitly configured;
- the user's Metricool API token is configured and AUTO_PUBLISH is enabled.
A capped, SQLite-deduplicated job per day prevents duplicate uncontrolled posts.
"""
import os
import secrets
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from . import metricool_api, twitch_media
from .video_pipeline import probe

router = APIRouter(prefix="/automation", tags=["automatic-workflow"])
lock = threading.Lock()


def _bool(name):
    return os.getenv(name, "0").strip() == "1"


def readiness():
    allow = {x.strip() for x in os.getenv("ALLOWED_BROADCASTER_IDS", "").split(",") if x.strip()}
    settings = metricool_api.settings()
    needs = {
        "automation_enabled": _bool("AUTO_PIPELINE_ENABLED"),
        "authorized_creator_ids_present": bool(allow),
        "twitch_editor_id_present": bool(os.getenv("TWITCH_EDITOR_ID", "").strip()),
        "twitch_editor_oauth_present": bool(os.getenv("TWITCH_EDITOR_ACCESS_TOKEN", "").strip()),
        "speech_api_key_present": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "metricool_api_token_present": metricool_api.configured(),
        "metricool_autopublish_enabled": settings["enabled"],
    }
    return {
        "ready_for_automatic_publishing": all(needs.values()),
        "checks": needs,
        "creator_count": len(allow),
        "max_posts_per_day": max(1, min(3, int(os.getenv("AUTO_POSTS_PER_DAY", "1")))),
        "metricool_brand": "ACTU GTA VI",
        "networks": ["tiktok", "youtube"],
        "video_duration_seconds": 65,
        "music": "original_procedural_electronic",
        "source": "official_twitch_editor_oauth_only",
        "notes": "Technical OAuth authorization and a Metricool Advanced/Custom API token are mandatory.",
    }


def init_automation_db():
    from .main import get_connection
    connection = get_connection()
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS auto_used_clips (
                clip_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_auto_completed
            ON auto_used_clips(completed_at)
        """)
        connection.commit()
    finally:
        connection.close()


def _select_candidates():
    from .main import get_connection
    con = get_connection()
    try:
        rows = con.execute("""
            SELECT id, broadcaster_id, broadcaster_name, title, view_count
            FROM clips
            WHERE eligible_for_processing = 1
              AND id NOT IN (SELECT clip_id FROM auto_used_clips)
            ORDER BY view_count DESC LIMIT 35
        """).fetchall()
        counts = con.execute("""
            SELECT COUNT(DISTINCT job_id) FROM auto_used_clips
            WHERE completed_at >= ?
        """, (datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00"),)).fetchone()[0]
        return [dict(row) for row in rows], int(counts)
    finally:
        con.close()


def _mark_done(clips, job_id):
    from .main import get_connection
    con = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        con.executemany(
            "INSERT OR IGNORE INTO auto_used_clips(clip_id, job_id, completed_at) VALUES (?, ?, ?)",
            [(row["id"], job_id, now) for row in clips],
        )
        con.commit()
    finally:
        con.close()


def _run_once():
    from .video_routes import VIDEO_DIR, _job_dir, _write_status, _read_status, _worker
    from . import publishing
    checks = readiness()
    if not checks["ready_for_automatic_publishing"]:
        return {"status": "blocked", "checks": checks["checks"]}
    if not lock.acquire(blocking=False):
        return {"status": "already_running"}
    folder = None
    input_clips = []
    try:
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        candidates, processed_today = _select_candidates()
        if processed_today >= checks["max_posts_per_day"]:
            return {"status": "daily_limit_reached"}
        if not candidates:
            return {"status": "no_new_authorized_clips"}

        # Only combine clips from a single broadcaster who granted OAuth access.
        grouped = {}
        for item in candidates:
            grouped.setdefault(item["broadcaster_id"], []).append(item)
        selected_group = next(
            (rows for rows in grouped.values() if len(rows) >= 2), None
        )
        if not selected_group:
            return {"status": "not_enough_clips_from_same_authorized_creator"}
        selected_group = selected_group[:5]
        if shutil.disk_usage(VIDEO_DIR).free < 320 * 1024 * 1024:
            return {"status": "insufficient_disk_space"}
        job_id = str(uuid.uuid4())
        folder = _job_dir(job_id)
        folder.mkdir(parents=True, exist_ok=False)
        source_paths = []
        total = 0.0
        for index, entry in enumerate(selected_group):
            media_file = folder / f"twitch-clip-{index}.mp4"
            twitch_media.download_clip(
                entry["broadcaster_id"], entry["id"], media_file
            )
            duration = probe(media_file)["duration"]
            total += duration
            source_paths.append(media_file)
            input_clips.append(entry)
            if total >= 67:
                break
        if total < 65.5:
            return {"status": "insufficient_duration", "clips": len(input_clips)}
        source = folder / "source.mp4"
        twitch_media.assemble_authorized_clips(source_paths, source, seconds=65)
        for video in source_paths:
            video.unlink(missing_ok=True)

        title = str(input_clips[0].get("title") or "GTA VI reaction")
        status = {
            "job_id": job_id, "state": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_type": "official_twitch_editor_oauth",
            "source_clip_ids": [entry["id"] for entry in input_clips],
            "authorized": True, "autogenerated": True,
            "publication_title": "GTA VI | " + title[:68],
            "requested_duration_seconds": 65,
            "auto_subtitles_requested": True,
            "music_original_generated": True,
        }
        _write_status(folder, status)
        _worker(job_id, "", 65, auto_subtitles=True, music_enabled=True,
                hook_text="GTA VI | THE REACTION")
        status = _read_status(job_id)
        if status.get("state") != "done":
            return {"status": "editing_failed", "job_id": job_id,
                    "error": status.get("error")}
        # Persist clip IDs before any external scheduling call. Unknown network
        # outcomes must never cause duplicate TikTok/YouTube posts.
        _mark_done(input_clips, job_id)
        # Give Metricool an unlisted public source URL with a random nonce
        # during the synchronous media normalization request. Revoke it after.
        transfer_token = secrets.token_urlsafe(32)
        status["transfer_token"] = transfer_token
        status["publication_status"] = "media_transfer_pending"
        _write_status(folder, status)
        hostname = os.getenv(
            "RENDER_EXTERNAL_URL", "https://twitch-gta6-clipping.onrender.com"
        ).rstrip("/")
        transfer_url = f"{hostname}/video/media/{job_id}/{transfer_token}/vertical.mp4"
        try:
            normalized = metricool_api.normalize_media(transfer_url)
            # Revoke public token immediately after Metricool hosts its copy.
            status["transfer_token"] = None
            status["publication_status"] = "scheduling_in_progress"
            _write_status(folder, status)
            post = publishing.publication_package(job_id, status)
            scheduled = metricool_api.schedule_both_networks(
                normalized, post["tiktok_caption"], post["title"],
            )
            status["publication_status"] = "submitted_to_metricool"
            status["metricool_schedule"] = scheduled
            _write_status(folder, status)
            return {"status": "scheduled", "job_id": job_id, "networks": scheduled["networks"]}
        except Exception as exc:
            status["transfer_token"] = None
            status["publication_status"] = "manual_intervention_required"
            status["schedule_error"] = type(exc).__name__
            _write_status(folder, status)
            print("Metricool automatic scheduling failed", job_id, type(exc).__name__)
            return {"status": "needs_attention", "job_id": job_id}
    except Exception as exc:
        print("Autopilot failed:", type(exc).__name__)
        return {"status": "failed", "error": type(exc).__name__}
    finally:
        if folder is not None:
            for file in folder.glob("twitch-clip-*.mp4"):
                file.unlink(missing_ok=True)
            # Keep only successful MP4 outputs, cleanup incomplete non-job dirs.
            if not (folder / "status.json").exists():
                shutil.rmtree(folder, ignore_errors=True)
        lock.release()


@router.get("/status")
def automation_status():
    return readiness()


@router.post("/run")
def run_automation(background_tasks: BackgroundTasks,
                   x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize
    _authorize(x_admin_token)
    if not readiness()["ready_for_automatic_publishing"]:
        raise HTTPException(409, detail=readiness())
    background_tasks.add_task(_run_once)
    return {"status": "queued", "action": "licensed_autopilot_pass"}


def run_automatic_after_poll():
    """Called by the existing 15-minute radar thread; never duplicates work."""
    if _bool("AUTO_PIPELINE_ENABLED"):
        _run_once()
