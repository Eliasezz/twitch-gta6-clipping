"""Official Twitch clip-media access, requiring actual broadcaster/editor OAuth.

Written permission to reuse content is separate from the API-level
editor:manage:clips/channel:manage:clips OAuth grant.
Never scrape Twitch pages or derive CDN URLs from thumbnails.
"""
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests


class TwitchMediaError(RuntimeError):
    pass


def _editor_settings():
    editor_id = os.getenv("TWITCH_EDITOR_ID", "").strip()
    token = os.getenv("TWITCH_EDITOR_ACCESS_TOKEN", "").strip()
    client_id = os.getenv("TWITCH_CLIENT_ID", "").strip()
    if not (editor_id and token and client_id):
        raise TwitchMediaError("Twitch editor OAuth is not configured")
    return editor_id, token, client_id


def permitted_broadcaster(broadcaster_id):
    allowed = {v.strip() for v in os.getenv("ALLOWED_BROADCASTER_IDS", "").split(",") if v.strip()}
    return bool(str(broadcaster_id) in allowed)


def official_clip_url(broadcaster_id, clip_id):
    if not permitted_broadcaster(broadcaster_id):
        raise TwitchMediaError("Broadcaster is not allowlisted for reuse")
    editor_id, token, client_id = _editor_settings()
    try:
        response = requests.get(
            "https://api.twitch.tv/helix/clips/downloads",
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Id": client_id,
            },
            params={
                "editor_id": editor_id,
                "broadcaster_id": str(broadcaster_id),
                "clip_id": str(clip_id),
            },
            timeout=25,
        )
    except requests.RequestException as exc:
        raise TwitchMediaError("Twitch media API network error") from exc
    if response.status_code in {401, 403}:
        raise TwitchMediaError("Twitch editor OAuth authorization denied")
    if not response.ok:
        raise TwitchMediaError("Twitch media API request failed")
    rows = response.json().get("data") or []
    for item in rows:
        if item.get("clip_id") == clip_id:
            url = item.get("landscape_download_url") or item.get("portrait_download_url")
            if not url:
                continue
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not (
                host.endswith(".twitchcdn.net") or host.endswith(".twitch.tv")
            ):
                raise TwitchMediaError("Unexpected Twitch media host")
            return url
    raise TwitchMediaError("Twitch did not return a permitted clip download URL")


def download_clip(broadcaster_id, clip_id, destination, *, max_bytes=65 * 1024 * 1024):
    """Stream the official temporary clip URL to a local temporary file."""
    url = official_clip_url(broadcaster_id, clip_id)
    destination = Path(destination)
    count = 0
    try:
        with requests.get(url, stream=True, timeout=(15, 120), allow_redirects=False) as response:
            if not response.ok:
                raise TwitchMediaError("Twitch clip media could not be downloaded")
            with destination.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    count += len(chunk)
                    if count > max_bytes:
                        raise TwitchMediaError("Twitch clip media exceeds size limit")
                    f.write(chunk)
    except (OSError, requests.RequestException) as exc:
        destination.unlink(missing_ok=True)
        raise TwitchMediaError("Twitch clip media transfer failed") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if count < 1024:
        destination.unlink(missing_ok=True)
        raise TwitchMediaError("Twitch clip media is empty")
    return destination


def assemble_authorized_clips(clip_files, output, *, seconds=65):
    """Compose distinct permissioned clips into an original editorial montage.

    Normalize video/audio formats before concatenating; never loop short
    clips or fabricate a >1min source from less than 65s original footage.
    """
    from .video_pipeline import probe
    output = Path(output)
    if len(clip_files) < 2 or len(clip_files) > 5:
        raise TwitchMediaError("Provide 2 to 5 distinct authorized clip files")
    sources = [Path(p) for p in clip_files]
    lengths = [probe(p)["duration"] for p in sources]
    if sum(lengths) < seconds + 0.5:
        raise TwitchMediaError("Insufficient genuine footage to create a minute-plus montage")
    folder = output.parent
    normalized = []
    remaining = float(seconds + 0.6)
    concat_file = folder / "source-concat.txt"
    try:
        for i, (file, length) in enumerate(zip(sources, lengths)):
            if remaining <= 0:
                break
            take = min(length, remaining)
            part = folder / f"normalized-{i}.mp4"
            info = probe(file)
            command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-y", "-i", str(file)]
            if not info["has_audio"]:
                command.extend(["-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono"])
            command.extend([
                "-t", str(take), "-map", "0:v:0",
                "-map", "0:a:0" if info["has_audio"] else "1:a:0",
                "-vf", "fps=24,scale=480:270:force_original_aspect_ratio=decrease,"
                       "pad=480:270:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast",
                "-crf", "29", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "48k", "-ar", "22050", "-ac", "1",
                "-shortest", str(part),
            ])
            done = subprocess.run(command, capture_output=True, text=True, timeout=210)
            if done.returncode:
                raise TwitchMediaError("Unable to normalize authorized clip")
            normalized.append(part)
            remaining -= probe(part)["duration"]
        concat_file.write_text(
            "".join(f"file '{part.name}'\n" for part in normalized), encoding="utf-8"
        )
        render = subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", str(output)
        ], capture_output=True, text=True, timeout=150)
        if render.returncode or probe(output)["duration"] < seconds:
            raise TwitchMediaError("Authorized source assembly could not reach the requested length")
        return output
    finally:
        concat_file.unlink(missing_ok=True)
        for part in normalized:
            part.unlink(missing_ok=True)
