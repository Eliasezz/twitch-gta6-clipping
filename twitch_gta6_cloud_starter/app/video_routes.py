"""Authenticated video upload -> background MP4 render -> private download API."""
import hmac
import json
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .video_pipeline import VideoPipelineError, render_mp4, probe

router = APIRouter(prefix="/video", tags=["video-workflow"])
VIDEO_DIR = Path(os.getenv("VIDEO_STORAGE_DIR", "/data/videos"))
MAX_UPLOAD_BYTES = min(120, max(1, int(os.getenv("VIDEO_MAX_UPLOAD_MB", "75")))) * 1024 * 1024
_render_lock = threading.Lock()
VIDEO_KEEP_HOURS = max(24, int(os.getenv("VIDEO_KEEP_HOURS", "48")))


def _cleanup_finished_jobs():
    """Free storage used by already-finished jobs older than the retention period."""
    if not VIDEO_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=VIDEO_KEEP_HOURS)
    for entry in VIDEO_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            if str(uuid.UUID(entry.name)) != entry.name:
                continue
            status_file = entry / "status.json"
            if not status_file.is_file():
                continue
            item = json.loads(status_file.read_text(encoding="utf-8"))
            if item.get("state") not in {"done", "failed"}:
                continue
            created = datetime.fromisoformat(item["created_at"])
            if created.tzinfo and created < cutoff:
                shutil.rmtree(entry)
        except (OSError, ValueError, KeyError, TypeError):
            continue


def _authorize(token):
    secret = os.getenv("POLL_ADMIN_TOKEN", "").strip()
    if not secret:
        raise HTTPException(503, "Configure POLL_ADMIN_TOKEN in Render to enable video jobs")
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(401, "Invalid administrator token")


def _job_dir(job_id):
    try:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
    except ValueError as exc:
        raise HTTPException(404, "Unknown video job") from exc
    return VIDEO_DIR / job_id


def _write_status(job_dir, payload):
    job_dir.mkdir(parents=True, exist_ok=True)
    temp_file = job_dir / "status.tmp"
    temp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_file.replace(job_dir / "status.json")


def _read_status(job_id):
    status_file = _job_dir(job_id) / "status.json"
    if not status_file.exists():
        raise HTTPException(404, "Unknown video job")
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "Video job metadata unavailable") from exc


def _worker(job_id, caption_text, clip_seconds, width=720, height=1280):
    folder = _job_dir(job_id)
    with _render_lock:
        status = _read_status(job_id)
        status["state"] = "processing"
        _write_status(folder, status)
        try:
            source = folder / "source.mp4"
            output = folder / "vertical.mp4"
            probe(source)
            result = render_mp4(source, output, caption_text=caption_text,
                                clip_seconds=clip_seconds, width=width, height=height)
            status.update({"state": "done", "result": result,
                           "download_endpoint": f"/video/jobs/{job_id}/download"})
        except Exception as error:
            # Mark all worker failures, including subprocess timeouts, as failed.
            status.update({"state": "failed", "error": type(error).__name__})
            print("Video job failed:", job_id, type(error).__name__, str(error)[-500:])
        finally:
            (folder / "source.mp4").unlink(missing_ok=True)
            _write_status(folder, status)


def _demo_worker(job_id: str, clip_seconds: int):
    """Generate an original synthetic source video, then test the regular editor."""
    folder = _job_dir(job_id)
    source = folder / "source.mp4"
    # 12 fps keeps synthetic source generation cheap on Render's small instance.
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=480x270:rate=12:duration={clip_seconds + 1}",
        "-f", "lavfi", "-i",
        f"sine=frequency=530:sample_rate=22050:duration={clip_seconds + 1}",
        "-c:v", "mpeg4", "-q:v", "14",
        "-c:a", "aac", "-b:a", "40k",
        "-shortest", str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False, timeout=160)
        if result.returncode != 0:
            raise VideoPipelineError("Synthetic source generation failed")
        # Render a smaller preview to avoid exhausting a 512-MB instance.
        _worker(
            job_id,
            "SYNTHETIC TEST ONLY THIS IS A VIDEO WORKFLOW DEMONSTRATION",
            clip_seconds, width=360, height=640,
        )
    except Exception as error:
        (folder / "source.mp4").unlink(missing_ok=True)
        status = _read_status(job_id)
        status.update({"state": "failed", "error": type(error).__name__})
        _write_status(folder, status)
        print("Synthetic video demo failed:", job_id, type(error).__name__)


@router.post("/demo", status_code=202)
def create_demo_job(background_tasks: BackgroundTasks,
                    clip_seconds: int = 65,
                    x_admin_token: str | None = Header(default=None)):
    """No upload needed: create an ORIGINAL test pattern, then render an MP4."""
    _authorize(x_admin_token)
    if clip_seconds < 3 or clip_seconds > 90:
        raise HTTPException(422, "clip_seconds must be between 3 and 90")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(503, "FFmpeg is unavailable")
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_finished_jobs()
    if shutil.disk_usage(VIDEO_DIR).free < 150 * 1024 * 1024:
        raise HTTPException(507, "Not enough disk space for the demo")
    job_id = str(uuid.uuid4())
    folder = _job_dir(job_id)
    folder.mkdir(parents=True, exist_ok=False)
    status = {
        "job_id": job_id, "state": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "demo": True, "source_type": "original_synthetic",
        "requested_duration_seconds": clip_seconds,
        "note": "This is a synthetic technical test, not monetizable content.",
    }
    _write_status(folder, status)
    background_tasks.add_task(_demo_worker, job_id, clip_seconds)
    return status


@router.get("/ready")
def video_ready():
    return {
        "available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "configured_for_private_jobs": bool(os.getenv("POLL_ADMIN_TOKEN", "").strip()),
        "workflow": "authorized-file-upload -> audio-activity highlight -> vertical 9:16 -> colored ASS subtitles -> MP4",
        "auto_speech_transcription": False,
        "automatic_social_publishing": False,
        "recommended_tiktok_duration_seconds": 65,
        "max_clip_duration_seconds": 90,
        "demo_without_upload": "/video/demo",
    }


@router.post("/jobs", status_code=202)
def create_video_job(background_tasks: BackgroundTasks,
                     video: UploadFile = File(...),
                     authorized: bool = Form(False),
                     caption_text: str = Form(""),
                     clip_seconds: int = Form(65),
                     x_admin_token: str | None = Header(default=None)):
    _authorize(x_admin_token)
    if not authorized:
        raise HTTPException(403, "Confirm you own this media or have explicit reuse permission")
    if clip_seconds < 3 or clip_seconds > 90:
        raise HTTPException(422, "clip_seconds must be 3 to 90")
    if len(caption_text) > 5000:
        raise HTTPException(413, "Caption text too long")
    if not (video.filename or "").lower().endswith((".mp4", ".mov", ".mkv")):
        raise HTTPException(415, "Upload an MP4, MOV, or MKV video")
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_finished_jobs()
    if shutil.disk_usage(VIDEO_DIR).free < MAX_UPLOAD_BYTES * 2:
        raise HTTPException(507, "Insufficient disk space for this upload")

    job_id = str(uuid.uuid4())
    folder = _job_dir(job_id)
    folder.mkdir(parents=True, exist_ok=False)
    source = folder / "source.mp4"
    amount = 0
    try:
        with source.open("wb") as destination:
            while True:
                block = video.file.read(1024 * 1024)
                if not block:
                    break
                amount += len(block)
                if amount > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Video exceeds configured upload limit")
                destination.write(block)
        if amount < 1024:
            raise HTTPException(422, "Video file is empty or too small")
        try:
            source_info = probe(source)
        except VideoPipelineError as exc:
            raise HTTPException(422, "Input is not a valid video") from exc
        if clip_seconds >= 61 and source_info["duration"] < clip_seconds - 0.1:
            raise HTTPException(
                422,
                f"Video must be at least {clip_seconds} seconds long for the requested clip",
            )
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        video.file.close()

    status = {"job_id": job_id, "state": "queued", "created_at": datetime.now(timezone.utc).isoformat(),
              "input_bytes": amount, "authorized": True,
              "requested_duration_seconds": clip_seconds}
    _write_status(folder, status)
    background_tasks.add_task(_worker, job_id, caption_text, clip_seconds)
    return status


@router.get("/jobs/{job_id}")
def get_video_job(job_id: str, x_admin_token: str | None = Header(default=None)):
    _authorize(x_admin_token)
    return _read_status(job_id)


@router.get("/jobs/{job_id}/download")
def download_video_job(job_id: str, x_admin_token: str | None = Header(default=None)):
    _authorize(x_admin_token)
    status = _read_status(job_id)
    output = _job_dir(job_id) / "vertical.mp4"
    if status.get("state") != "done" or not output.is_file():
        raise HTTPException(409, "MP4 is not available yet")
    return FileResponse(output, media_type="video/mp4", filename=f"gta6-highlight-{job_id[:8]}.mp4")
