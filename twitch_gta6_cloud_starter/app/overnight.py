"""Overnight Twitch -> 30s vertical MP4 + private ZIP (never require Metricool).

Every 15-minute radar poll may process one NEW official, permissioned Twitch
clip. No direct Twitch CDN scraping. No unlicensed VOD downloads. No media
from other jobs. Publication is deliberately separate and manual by default.
"""
import json
import os
import re
from urllib.parse import urlsplit
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
        db.execute("""CREATE TABLE IF NOT EXISTS studio_inbox (
            id TEXT PRIMARY KEY, broadcaster_id TEXT, broadcaster_name TEXT,
            title TEXT, submitted_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        db.executemany("""INSERT OR IGNORE INTO studio_inbox
            (id, broadcaster_name, title) VALUES (?, 'Zerkaa', ?)""", [
            ("PrettyTangibleMacaroniKevinTurtle-gbjo8gLeDVDzpkia", "Zerkaa · vicecityvirals"),
            ("EmpathicSavoryRabbitOhMyDog-yrVM6FpD2zuyUAVK", "Zerkaa · deeksty"),
        ])
        db.execute("CREATE INDEX IF NOT EXISTS overnight_created ON overnight_clip_jobs(created_at)")
        db.commit()
    finally:
        db.close()


def _limit():
    return min(96, max(1, int(os.getenv("AUTO_EDITS_PER_DAY", "96"))))


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
        "ready_for_public_replays": all(v for k, v in checks.items() if k != "twitch_editor_oauth_grant_present"),
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
        "vod_full_replay": "private 60-second samples from recent public GTA VI archives of authorized creators",
        "live_capture": "60-second public live samples from authorized creators playing GTA VI",
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
                 c.view_count, c.created_at, c.video_id, c.vod_offset
          FROM clips c
          LEFT JOIN overnight_clip_jobs used ON used.clip_id=c.id
          WHERE c.broadcaster_id IN ({placeholders})
            AND COALESCE(c.view_count,0) >= ?
            AND used.clip_id IS NULL
          ORDER BY EXISTS(SELECT 1 FROM studio_top_clips s WHERE s.clip_id=c.id) DESC,
                   c.view_count DESC, c.created_at DESC
          LIMIT ?
        """, (*sorted(allowed), MIN_VIEWS, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def clip_slug(url):
    parts = urlsplit(url.strip())
    path = parts.path.strip("/")
    if parts.scheme == "https" and parts.netloc == "clips.twitch.tv":
        slug = path
    elif (parts.scheme == "https" and parts.netloc in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
          and re.fullmatch(r"[A-Za-z0-9_]+/clip/[A-Za-z0-9_-]+", path)):
        slug = path.rsplit("/", 1)[1]
    else:
        raise HTTPException(422, "Colle le lien individuel d’un clip Twitch.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", slug):
        raise HTTPException(422, "Identifiant de clip invalide.")
    return slug


@router.post("/inbox", status_code=202)
def import_clip(url: str, background_tasks: BackgroundTasks,
                x_admin_token: str | None = Header(default=None)):
    from .main import twitch_get
    from .video_routes import _authorize
    _authorize(x_admin_token)
    slug = clip_slug(url)
    try:
        rows = twitch_get("clips", {"id": slug})["data"]
    except Exception:
        raise HTTPException(502, "Twitch est momentanément inaccessible.")
    if not rows:
        raise HTTPException(404, "Twitch ne trouve plus ce clip.")
    clip = rows[0]
    db = _db()
    try:
        db.execute("""INSERT INTO studio_inbox (id,broadcaster_id,broadcaster_name,title)
            VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            broadcaster_id=excluded.broadcaster_id,broadcaster_name=excluded.broadcaster_name,
            title=excluded.title""", (slug, clip["broadcaster_id"], clip["broadcaster_name"], clip["title"]))
        db.execute("DELETE FROM overnight_clip_jobs WHERE clip_id=? AND state='failed'", (slug,))
        db.commit()
    finally:
        db.close()
    background_tasks.add_task(run_once)
    return {"id": slug, "title": clip["title"]}


@router.get("/inbox")
def list_inbox(x_admin_token: str | None = Header(default=None)):
    from .video_routes import _authorize
    _authorize(x_admin_token)
    db = _db()
    try:
        return {"clips": [dict(row) for row in db.execute("""
            SELECT i.id,i.title,i.broadcaster_name,j.job_id,j.state,j.error
            FROM studio_inbox i LEFT JOIN overnight_clip_jobs j ON j.clip_id=i.id
            ORDER BY i.submitted_at DESC,i.rowid DESC LIMIT 100""")]}
    finally:
        db.close()


def _requested_candidates():
    from .main import twitch_get
    allowed = allowed_creators()
    db = _db()
    try:
        missing = [r[0] for r in db.execute("""
            SELECT i.id FROM studio_inbox i JOIN twitch_authorized_creators a
            ON lower(i.broadcaster_name)=lower(a.login)
            WHERE i.broadcaster_id IS NULL LIMIT 100""")]
        if missing:
            for item in twitch_get("clips", {"id": missing}).get("data", []):
                db.execute("UPDATE studio_inbox SET broadcaster_id=? WHERE id=?",
                           (item["broadcaster_id"], item["id"]))
            db.commit()
        rows = db.execute("""SELECT i.* FROM studio_inbox i
            LEFT JOIN overnight_clip_jobs j ON j.clip_id=i.id
            WHERE j.clip_id IS NULL ORDER BY i.submitted_at,i.rowid""")
        return [dict(r) for r in rows if r["broadcaster_id"] in allowed]
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


def run_once(preferred_source="clip"):
    """Run one authorized clip edit per pass and persist a downloadable ZIP."""
    from .video_routes import VIDEO_DIR, _job_dir, _write_status, _read_status, _worker
    from .video_exports import export_bundle
    checks = readiness()
    if not checks["ready_for_public_replays"]:
        return {"status": "blocked", "checks": checks["checks"]}
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running"}
    job_id = None
    clip = None
    try:
        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        if _used_today() >= _limit():
            return {"status": "daily_edit_limit_reached", "limit": _limit()}
        from .video_routes import _cleanup_finished_jobs
        _cleanup_finished_jobs()
        if shutil.disk_usage(VIDEO_DIR).free < 250 * 1024 * 1024:
            return {"status": "insufficient_disk_space", "minimum_free_mb": 250}
        from . import replay
        candidates = _requested_candidates() if checks["ready_for_automatic_editing"] else []
        radar = _candidates() if checks["ready_for_automatic_editing"] else []
        if not candidates and (preferred_source != "clip" or not radar):
            db = _db()
            try:
                samples = replay.candidates(db)
                key = "video_id" if preferred_source == "replay" else "stream_id"
                candidates = [s for s in samples if s.get(key)] or samples
            finally:
                db.close()
        candidates = candidates or radar
        if not candidates:
            return {"status": "no_new_permissioned_clips"}
        clip = candidates[0]
        job_id = str(uuid.uuid4())
        folder = _job_dir(job_id)
        _record(clip, job_id, "downloading")
        folder.mkdir(parents=True, exist_ok=False)
        src = folder / "source.mp4"
        is_replay = bool(clip.get("video_created_at"))
        is_live = bool(clip.get("stream_id"))
        origin = {key: clip.get(key) for key in ("broadcaster_name","title","video_id","vod_offset","video_created_at","stream_id","stream_started_at")}
        if not is_replay and not clip.get("url"):
            from .main import twitch_get
            details = twitch_get("clips", {"id": clip["id"]}).get("data", [])
            if details:
                clip = dict(clip, **{k: details[0].get(k) for k in ("video_id","vod_offset","url","created_at")})
                origin.update(video_id=clip["video_id"], vod_offset=clip["vod_offset"])
        origin["clip_created_at"] = None if is_replay or is_live else clip.get("created_at")
        origin["url"] = clip.get("url") or "https://clips.twitch.tv/"+clip["id"]
        origin["collected_at"] = datetime.now(timezone.utc).isoformat()
        _write_status(folder, {
            "origin": origin, "vision_enabled": openai_auth_status()["configured"],
            "job_id": job_id, "state": "queued",
            "stage": "downloading_official_twitch_clip", "progress": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "authorized": True, "autogenerated": True,
            "source_type": "authorized_live" if is_live else ("public_authorized_replay" if is_replay else "official_twitch_clip"),
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
        if is_replay or is_live:
            replay.download(clip, src)
        else:
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
        if state.get("state") == "failed" and src.is_file() and state.get("error") in {"VideoPipelineError","CalledProcessError"}:
            state.update(rhythmic=False, vision_enabled=False, recovery="continuous_original_audio")
            _write_status(folder, state)
            _worker(job_id, "", seconds, width=540, height=960,
                    auto_subtitles=openai_auth_status()["configured"], music_enabled=False)
            state = _read_status(job_id)
        if state.get("state") != "done":
            _record(clip, job_id, "failed", str(state.get("error") or "render failed"))
            return {"status": "editing_failed", "job_id": job_id,
                    "error": state.get("error_detail") or state.get("error")}
        archive = export_bundle(job_id, folder, state)
        _record(clip, job_id, "done")
        from .publishing import publication_package
        report = publication_package(job_id, state)
        print("Twitch export report:", json.dumps({"job_id": job_id, "origin": origin,
              "description": report["tiktok_caption"], "shots": state["result"].get("source_shots"),
              "start_seconds": state["result"].get("start_seconds"),
              "caption_mode": state["result"].get("caption_mode"),
              "vision_used": bool((state["result"].get("highlight_analysis") or {}).get("vision")),
              "vision_warning": (state["result"].get("highlight_analysis") or {}).get("vision_warning"),
              "caption_warning": state["result"].get("transcription_warning"),
              "source_type": state["source_type"],
              "duration_seconds": state["result"].get("duration_seconds"),
              "recovery": state.get("recovery")}, ensure_ascii=True), flush=True)
        print("Overnight Twitch clip ready:", job_id, clip["id"], seconds, archive.stat().st_size)
        return {"status": "ready_to_download", "job_id": job_id,
                "duration_seconds": seconds,
                "file": "ready-to-publish.zip"}
    except Exception as exc:
        if clip is not None and job_id is not None:
            detail = str(exc) if isinstance(exc, (twitch_media.TwitchMediaError, ValueError)) else type(exc).__name__
            _record(clip, job_id, "failed", detail)
            if (folder / "status.json").exists():
                status = _read_status(job_id)
                status.update(state="failed", error_detail=detail)
                _write_status(folder, status)
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
    if not readiness()["ready_for_public_replays"]:
        raise HTTPException(409, "Configure authorized creators and Twitch editor OAuth")
    background_tasks.add_task(run_once)
    return {"status": "queued", "action": "edit_one_authorized_twitch_clip"}


def run_overnight_after_poll():
    if os.getenv("AUTO_PIPELINE_ENABLED", "0") == "1":
        slot = int(datetime.now(timezone.utc).timestamp()) // 900
        return run_once(("clip", "live", "replay")[slot % 3])
    return {"status": "disabled"}
