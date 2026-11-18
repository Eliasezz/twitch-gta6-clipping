"""Fail-closed provenance and actual MP4 output checks for a single job."""
import hashlib
import json
import math
import subprocess
from pathlib import Path


class IntegrityError(RuntimeError):
    pass


def _ffprobe(path):
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,start_time,avg_frame_rate",
            "-of", "json", str(path),
        ], capture_output=True, text=True, timeout=35, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise IntegrityError("File is not a playable audio/video stream") from exc


def sha256_file(path):
    hash_ = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hash_.update(chunk)
    return hash_.hexdigest()


def validate_source(source, *, max_bytes=300 * 1024 * 1024, max_duration=12 * 3600):
    path = Path(source)
    if not path.is_file() or path.is_symlink():
        raise IntegrityError("Uploaded source missing or invalid")
    if path.stat().st_size > max_bytes:
        raise IntegrityError("Uploaded source is bigger than 300 MB")
    data = _ffprobe(path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise IntegrityError("Uploaded file has no video track")
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration < 0.5 or duration > max_duration:
        raise IntegrityError("Uploaded source duration is invalid or exceeds 12 hours")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width < 16 or height < 16:
        raise IntegrityError("Uploaded source has no valid picture dimensions")
    return {
        "source_sha256": sha256_file(path), "source_bytes": path.stat().st_size,
        "source_codec": video.get("codec_name"), "source_seconds": round(duration, 3),
        "source_width": width, "source_height": height,
        "source_has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def validate_mp4(output, *, width, height, seconds, check_boundary=True):
    path = Path(output)
    if not path.is_file() or path.is_symlink():
        raise IntegrityError("Rendered MP4 was not created")
    data = _ffprobe(path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or video.get("codec_name") != "h264":
        raise IntegrityError("Output video must use H.264")
    if audio is None or audio.get("codec_name") != "aac":
        raise IntegrityError("Output is missing AAC sound")
    if (int(video.get("width", 0)), int(video.get("height", 0))) != (width, height):
        raise IntegrityError(f"Output must measure {width}x{height}")
    length = float(data["format"]["duration"])
    if not math.isfinite(length) or abs(length - seconds) > 0.75:
        raise IntegrityError(f"Output should be {seconds:.1f}s, found {length:.2f}s")
    # A small codec priming difference is normal. Larger start offsets are not.
    try:
        offset = abs(float(video.get("start_time") or 0) -
                     float(audio.get("start_time") or 0))
    except (TypeError, ValueError):
        offset = 0
    if offset > 0.30:
        raise IntegrityError(f"Audio/video start times differ by {offset:.2f}s")
    warnings = []
    if check_boundary:
        # Inspect the first and last second. No synthetic replacement media.
        for part_start in (0.0, max(0.0, length - 1.0)):
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-nostdin", "-ss", str(part_start),
                "-i", str(path), "-t", "0.95",
                "-vf", "blackdetect=d=0.35:pix_th=0.04:pic_th=0.98,"
                       "freezedetect=n=-65dB:d=0.70",
                "-an", "-f", "null", "-",
            ], capture_output=True, text=True, timeout=55, check=False)
            if result.returncode:
                raise IntegrityError("Could not inspect start/end video frames")
            text = result.stderr.lower()
            if "black_start:" in text:
                warnings.append("Near-black interval detected at video boundary")
            if "freeze_start:" in text:
                warnings.append("Possibly frozen frames at video boundary")
        # Do not reject legitimate dark gameplay or deliberate static shots.
        # The warnings make this case visible for editorial review.
    return {
        "output_sha256": sha256_file(path),
        "output_bytes": path.stat().st_size,
        "output_width": width, "output_height": height,
        "output_seconds": round(length, 3),
        "audio_video_offset_seconds": round(offset, 3),
        "boundary_warnings": sorted(set(warnings)),
        "video_codec": "h264", "audio_codec": "aac",
    }
