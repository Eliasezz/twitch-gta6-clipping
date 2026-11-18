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
from .publishing import publication_package, sign_share_link, verify_share_link
from .video_exports import export_bundle, ExportError
import hashlib
from .captions import openai_auth_status

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


def _worker(job_id, caption_text, clip_seconds, width=720, height=1280, auto_subtitles=False,
            music_enabled=False, hook_text=""):
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
                                clip_seconds=clip_seconds, width=width, height=height,
                                auto_subtitles=auto_subtitles, music_enabled=music_enabled,
                                hook_text=hook_text)
            status.update({"state": "done", "result": result,
                           "publication_status": "review_required",
                           "transcription_warning": result.get("transcription_warning"),
                           "download_endpoint": f"/video/jobs/{job_id}/download"})
        except Exception as error:
            # Mark all worker failures, including subprocess timeouts, as failed.
            status.update({"state": "failed", "error": type(error).__name__})
            print("Video job failed:", job_id, type(error).__name__, str(error)[-500:])
        finally:
            # Preserve the uploaded source when a render fails, so the user can
            # retry with corrected settings without uploading a large VOD again.
            if status.get("state") == "done":
                (folder / "source.mp4").unlink(missing_ok=True)
                status["source_retained_for_retry"] = False
                print("Video job finished:", job_id,
                      (status.get("result") or {}).get("duration_seconds"))
            else:
                status["source_retained_for_retry"] = (folder / "source.mp4").is_file()
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


def recover_failed_video_jobs_on_startup():
    """Retry only the user-requested failed jobs, once, with their retained files.

    VIDEO_RECOVER_FAILED_JOB_IDS is a comma-separated, non-secret list of
    previously failed job IDs. Never creates a duplicate and cannot publish.
    """
    ids = [
        token.strip() for token in
        os.getenv("VIDEO_RECOVER_FAILED_JOB_IDS", "").split(",")
        if token.strip()
    ][:3]
    recovered = []
    for job_id in ids:
        try:
            folder = _job_dir(job_id)
            source = folder / "source.mp4"
            status = _read_status(job_id)
            if not (
                status.get("state") == "failed"
                and status.get("authorized") is True
                and status.get("error") == "CaptionError"
                and not status.get("auto_recovery_attempted_at")
                and source.is_file()
            ):
                continue
            status["state"] = "queued"
            status["auto_recovery_attempted_at"] = datetime.now(timezone.utc).isoformat()
            status["previous_error"] = status.pop("error", None)
            status["publication_status"] = "not_ready"
            _write_status(folder, status)
            thread = threading.Thread(
                target=_worker,
                args=(job_id, "", int(status.get("requested_duration_seconds", 65))),
                kwargs={
                    "auto_subtitles": bool(status.get("auto_subtitles_requested", False)),
                    "music_enabled": bool(status.get("music_original_generated", True)),
                    "hook_text": str(status.get("hook_text") or "GTA VI | BIG REACTION"),
                },
                daemon=True,
                name="recover-caption-job-" + job_id[:8],
            )
            thread.start()
            recovered.append(job_id)
            print("Automatically resumed failed caption job:", job_id)
        except (OSError, ValueError, HTTPException) as error:
            print("Caption recovery skipped:", job_id, type(error).__name__)
    return recovered


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


def start_internal_selftest():
    """Optional one-time production smoke test; never downloads third-party media."""
    if os.getenv("VIDEO_SELFTEST_ONCE", "").strip() != "1":
        return
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    manifest = VIDEO_DIR / "selftest.json"
    duration = 65
    job_id = None
    if manifest.is_file():
        try:
            entry = json.loads(manifest.read_text(encoding="utf-8"))
            job_id = entry["job_id"]
            previous = _read_status(job_id)
            if previous.get("state") == "done":
                print("Video production self-test already passed:", job_id)
                return
        except (OSError, KeyError, ValueError, HTTPException):
            job_id = None
    if not job_id:
        job_id = str(uuid.uuid4())
        folder = _job_dir(job_id)
        folder.mkdir(parents=True, exist_ok=False)
        _write_status(folder, {
            "job_id": job_id, "state": "queued", "demo": True,
            "source_type": "original_synthetic",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "requested_duration_seconds": duration,
        })
        temp = manifest.with_suffix(".tmp")
        temp.write_text(json.dumps({"job_id": job_id}), encoding="utf-8")
        temp.replace(manifest)
    print("Video production self-test started:", job_id)
    threading.Thread(
        target=_demo_worker, args=(job_id, duration),
        daemon=True, name="mp4-production-smoke"
    ).start()


def _selftest_summary():
    manifest = VIDEO_DIR / "selftest.json"
    if not manifest.is_file():
        return {"state": "not_run"}
    try:
        job_id = json.loads(manifest.read_text(encoding="utf-8"))["job_id"]
        status = _read_status(job_id)
        return {
            "state": status.get("state", "unknown"),
            "duration_seconds": (status.get("result") or {}).get("duration_seconds"),
            "job_id": job_id,
            "download_endpoint": status.get("download_endpoint"),
        }
    except (OSError, ValueError, KeyError, HTTPException):
        return {"state": "unavailable"}


@router.get("/ready")
def video_ready():
    return {
        "available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "configured_for_private_jobs": bool(os.getenv("POLL_ADMIN_TOKEN", "").strip()),
        "workflow": "authorized-file-upload -> audio-activity highlight -> vertical 9:16 -> colored ASS subtitles -> MP4",
        "auto_speech_transcription": openai_auth_status()["configured"],
        "transcription_authentication_method": openai_auth_status()["method"],
        "transcription_oidc_provider_configured": openai_auth_status()["identity_provider_id_present"],
        "transcription_oidc_service_account_configured": openai_auth_status()["service_account_id_present"],
        "transcription_oidc_token_file_available": openai_auth_status()["identity_token_file_available"],
        "transcription_provider": "openai_whisper_1_optional",
        "transcription_requires_credentials": True,
        "automatic_social_publishing": __import__(
            "app.autopilot", fromlist=["readiness"]
        ).readiness()["ready_for_automatic_publishing"],
        "original_instrumental_music_supported": True,
        "default_music_for_uploaded_clips": True,
        "recommended_tiktok_duration_seconds": 65,
        "max_clip_duration_seconds": 90,
        "demo_without_upload": "/video/demo",
        "large_video_uploader": "/video/uploader",
        "max_chunked_video_upload_mb": min(350, max(32, int(os.getenv("VIDEO_CHUNKED_MAX_MB", "300")))),
        "media_handoff": "review_required_before_signed_metricool_link",
        "manual_export": "GET /video/jobs/{job_id}/export and POST /video/jobs/{job_id}/export-link",
        "production_selftest": _selftest_summary(),
    }


@router.post("/jobs", status_code=202)
def create_video_job(background_tasks: BackgroundTasks,
                     video: UploadFile = File(...),
                     authorized: bool = Form(False),
                     caption_text: str = Form(""),
                     auto_subtitles: bool | None = Form(None),
                     music_enabled: bool = Form(True),
                     hook_text: str = Form("GTA VI | BIG REACTION"),
                     clip_seconds: int = Form(65),
                     x_admin_token: str | None = Header(default=None)):
    _authorize(x_admin_token)
    if not authorized:
        raise HTTPException(403, "Confirm you own this media or have explicit reuse permission")
    if clip_seconds < 3 or clip_seconds > 90:
        raise HTTPException(422, "clip_seconds must be 3 to 90")
    # If omitted from Swagger, auto-select transcription whenever a platform
    # key is configured. Explicit false still avoids any paid transcription.
    speech_auth_ready = openai_auth_status()["configured"]
    if auto_subtitles is None:
        auto_subtitles = speech_auth_ready
    if auto_subtitles and not speech_auth_ready:
        raise HTTPException(
            503, "Configure OPENAI_API_KEY or OpenAI OIDC workload identity in Render"
        )
    if len(hook_text) > 80:
        raise HTTPException(422, "hook_text must contain 80 characters or fewer")
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
              "original_filename": (video.filename or "source.mp4")[:128],
              "requested_duration_seconds": clip_seconds,
              "auto_subtitles_requested": auto_subtitles,
              "music_original_generated": music_enabled,
              "hook_text": hook_text,
              "publication_status": "not_ready"}
    _write_status(folder, status)
    background_tasks.add_task(_worker, job_id, caption_text, clip_seconds,
                              auto_subtitles=auto_subtitles,
                              music_enabled=music_enabled, hook_text=hook_text)
    return status


@router.get("/jobs/{job_id}")
def get_video_job(job_id: str, x_admin_token: str | None = Header(default=None)):
    _authorize(x_admin_token)
    return _read_status(job_id)


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_failed_video_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    auto_subtitles: bool | None = Form(None),
    x_admin_token: str | None = Header(default=None),
):
    """Retry a previously failed job using the retained original source file."""
    _authorize(x_admin_token)
    with _render_lock:
        status = _read_status(job_id)
        source = _job_dir(job_id) / "source.mp4"
        if status.get("state") != "failed" or not source.is_file():
            raise HTTPException(409, "Failed video source is not available for retry")
        if auto_subtitles is None:
            auto_subtitles = bool(status.get("auto_subtitles_requested", False))
        if auto_subtitles and not openai_auth_status()["configured"]:
            raise HTTPException(503, "OpenAI transcription is not configured")
        status["state"] = "queued"
        status.pop("error", None)
        status["auto_subtitles_requested"] = auto_subtitles
        status["publication_status"] = "not_ready"
        _write_status(_job_dir(job_id), status)
    background_tasks.add_task(
        _worker,
        job_id,
        "",
        int(status.get("requested_duration_seconds", 65)),
        auto_subtitles=auto_subtitles,
        music_enabled=bool(status.get("music_original_generated", True)),
        hook_text=str(status.get("hook_text") or "GTA VI | BIG REACTION"),
    )
    return {"job_id": job_id, "state": "queued",
            "message": "Retry started without re-uploading the source file"}


@router.get("/jobs/{job_id}/download")
def download_video_job(job_id: str, x_admin_token: str | None = Header(default=None)):
    _authorize(x_admin_token)
    status = _read_status(job_id)
    output = _job_dir(job_id) / "vertical.mp4"
    if status.get("state") != "done" or not output.is_file():
        raise HTTPException(409, "MP4 is not available yet")
    return FileResponse(output, media_type="video/mp4", filename=f"gta6-highlight-{job_id[:8]}.mp4")


@router.get("/jobs/{job_id}/publication", tags=["publishing-review"])
def publication_preview(job_id: str, x_admin_token: str | None = Header(default=None)):
    """Preview a private Metricool-ready draft; does not schedule anything."""
    _authorize(x_admin_token)
    status = _read_status(job_id)
    if status.get("demo"):
        raise HTTPException(403, "Synthetic tests are not for publication")
    return publication_package(job_id, status)


@router.post("/jobs/{job_id}/approve", tags=["publishing-review"])
def approve_publication(
    job_id: str,
    rights_confirmed: bool = Form(False),
    editorial_reviewed: bool = Form(False),
    x_admin_token: str | None = Header(default=None),
):
    """Approve a real licensed edit and create a 1-hour URL for Metricool ingest.

    Requires explicit rights and editorial confirmation. Never auto-publishes.
    """
    _authorize(x_admin_token)
    status = _read_status(job_id)
    if status.get("demo") or status.get("authorized") is not True:
        raise HTTPException(403, "Only explicitly authorized real clips can be shared")
    if status.get("state") != "done":
        raise HTTPException(409, "MP4 has not finished rendering")
    if not (rights_confirmed and editorial_reviewed):
        raise HTTPException(403, "Rights and editorial review must both be confirmed")
    output = _job_dir(job_id) / "vertical.mp4"
    if not output.exists():
        raise HTTPException(404, "Video output missing")
    status["publication_status"] = "approved_for_scheduling"
    status["editorial_reviewed_at"] = datetime.now(timezone.utc).isoformat()
    _write_status(_job_dir(job_id), status)
    signed = sign_share_link(job_id, os.environ["POLL_ADMIN_TOKEN"])
    package = publication_package(job_id, status)
    package.update({"signed_media_url": signed["url"],
                    "signed_url_expires_unix": signed["expires_unix"],
                    "note": "Submit this temporary link to Metricool before it expires. "
                            "This approval does not publish or schedule the video."})
    return package


@router.get("/shared/{job_id}", tags=["publishing-review"])
def temporarily_shared_video(job_id: str, expires: int, signature: str):
    """Time-limited media URL for Metricool ingest; no long-lived public link."""
    if not verify_share_link(
        job_id, os.getenv("POLL_ADMIN_TOKEN", ""), expires, signature
    ):
        raise HTTPException(403, "Invalid or expired media link")
    status = _read_status(job_id)
    if status.get("state") != "done" or status.get("publication_status") != "approved_for_scheduling":
        raise HTTPException(403, "Video has not been approved for distribution")
    if status.get("demo") or status.get("authorized") is not True:
        raise HTTPException(403, "Synthetic or unauthorized video cannot be shared")
    output = _job_dir(job_id) / "vertical.mp4"
    if not output.is_file():
        raise HTTPException(404, "Video output unavailable")
    return FileResponse(
        output, media_type="video/mp4",
        filename=f"gta6-editorial-{job_id[:8]}.mp4",
        headers={"Cache-Control": "private, no-store",
                 "X-Robots-Tag": "noindex, nofollow"},
    )


@router.get("/media/{job_id}/{token}/vertical.mp4", tags=["automatic-workflow"])
def metricool_temporary_media_transfer(job_id: str, token: str):
    """Unlisted, high-entropy temporary transfer link for Metricool ingest.

    It is immediately revoked after normalization. It never grants access
    to a synthetic or unauthorized video's output.
    """
    status = _read_status(job_id)
    valid = status.get("transfer_token")
    if (not valid or not hmac.compare_digest(str(valid), token)
            or not status.get("autogenerated")
            or status.get("state") != "done"
            or status.get("demo")
            or status.get("authorized") is not True
            or status.get("publication_status") != "media_transfer_pending"):
        raise HTTPException(403, "Media transfer unavailable")
    path = _job_dir(job_id) / "vertical.mp4"
    if not path.is_file():
        raise HTTPException(404, "Edited MP4 is unavailable")
    return FileResponse(
        path, media_type="video/mp4", filename="authorized-gta6.mp4",
        headers={"Cache-Control": "private, no-store",
                 "X-Robots-Tag": "noindex, nofollow"},
    )


def _sign_export(job_id, expires):
    """A signed download URL is independent of the admin token and expires."""
    token = os.getenv("POLL_ADMIN_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Package downloads require a configured admin token")
    data = f"video-export:{job_id}:{int(expires)}".encode("utf-8")
    return hmac.new(token.encode("utf-8"), data, hashlib.sha256).hexdigest()


@router.get("/jobs/{job_id}/export", tags=["download-to-computer"])
def export_private(job_id: str, x_admin_token: str | None = Header(default=None)):
    """Download complete ZIP directly via Swagger with X-Admin-Token."""
    _authorize(x_admin_token)
    status = _read_status(job_id)
    try:
        archive = export_bundle(job_id, _job_dir(job_id), status)
    except ExportError as exc:
        raise HTTPException(409, str(exc)) from exc
    return FileResponse(
        archive, filename=f"GTA_VI_{job_id[:8]}_pret_a_publier.zip",
        media_type="application/zip",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/jobs/{job_id}/export-link", tags=["download-to-computer"])
def create_export_link(job_id: str, x_admin_token: str | None = Header(default=None)):
    """Produce a direct browser-download URL valid for 30 minutes.

    For a Mac, open the returned URL in Safari then unzip in Downloads.
    Anyone who has the temporary link can access the package until expiry.
    """
    _authorize(x_admin_token)
    status = _read_status(job_id)
    try:
        export_bundle(job_id, _job_dir(job_id), status)
    except ExportError as exc:
        raise HTTPException(409, str(exc)) from exc
    from datetime import timedelta
    expires = int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp())
    signature = _sign_export(job_id, expires)
    base = os.getenv(
        "RENDER_EXTERNAL_URL", "https://twitch-gta6-clipping.onrender.com"
    ).rstrip("/")
    return {
        "job_id": job_id,
        "url": f"{base}/video/export-download/{job_id}?expires={expires}&signature={signature}",
        "expires_unix": expires,
        "contains": ["montage_9x16.mp4", "couverture.jpg", "tiktok_description.txt",
                     "youtube_titre.txt", "youtube_description.txt",
                     "publication.json", "INSTRUCTIONS_FR.txt"],
        "publication_status": status.get("publication_status", "review_required"),
        "demo": bool(status.get("demo")),
        "instructions": "Open the temporary link in Safari; save the ZIP and extract it on your Mac.",
    }


@router.get("/export-download/{job_id}", tags=["download-to-computer"])
def download_export_link(job_id: str, expires: int, signature: str):
    """Anonymous access is restricted to a cryptographic 30-minute link."""
    now = int(datetime.now(timezone.utc).timestamp())
    if not (
        len(signature) == 64
        and now < expires <= now + 1800
        and hmac.compare_digest(signature, _sign_export(job_id, expires))
    ):
        raise HTTPException(403, "Expired or invalid package link")
    status = _read_status(job_id)
    try:
        archive = export_bundle(job_id, _job_dir(job_id), status)
    except ExportError as exc:
        raise HTTPException(409, str(exc)) from exc
    return FileResponse(
        archive, filename=f"GTA_VI_{job_id[:8]}_pret_a_publier.zip",
        media_type="application/zip",
        headers={"Cache-Control": "private, no-store",
                 "X-Robots-Tag": "noindex, nofollow"},
    )
