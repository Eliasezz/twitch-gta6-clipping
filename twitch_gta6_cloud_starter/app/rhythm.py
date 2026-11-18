"""Source-only rhythmic montage. Every shot is cut from this job's source.

A 1.2s cold-open reuses the actual selected peak, then 2.4s contextual
shots follow. No downloaded videos, demo assets, sample clips or caches.
Temporary normalized shots live under the caller's isolated job directory.
"""
import math
import subprocess
from pathlib import Path


class RhythmError(RuntimeError):
    pass


def _shot_filter(width, height, *, face=False, punch=False):
    """Return a complete FFmpeg video graph with no black pillarboxes."""
    if face:
        half = height // 2
        # User confirms that the webcam is bottom-left in source.
        return (
            "[0:v]split=2[game][cam];"
            f"[game]scale={width}:{half}:force_original_aspect_ratio=increase,"
            f"crop={width}:{half},setsar=1[g];"
            "[cam]crop=iw/3:ih/2:0:ih/2,"
            f"scale={width}:{half},setsar=1[c];"
            "[g][c]vstack=inputs=2,"
            "fps=24,format=yuv420p,setsar=1[outv]"
        )
    zoom = "crop=iw/1.09:ih/1.09," if punch else ""
    return (
        f"[0:v]{zoom}split=2[base][foreground];"
        f"[base]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=10:1[bg];"
        f"[foreground]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        "setsar=1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        "fps=24,format=yuv420p,setsar=1[outv]"
    )


def _run(command, *, timeout=360):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RhythmError("Shot creation unavailable or timed out") from exc
    if result.returncode:
        raise RhythmError("Shot creation failed: " + result.stderr[-350:])


def plan_shots(start, duration, event):
    """Plan peak hook plus contextual 1.5–2.5s source-only shots."""
    if duration < 5 or not all(math.isfinite(x) for x in (start, duration, event)):
        raise RhythmError("Need at least five seconds of valid source for rhythmic editing")
    peak = min(max(0.0, event - start), max(0.0, duration - 1.3))
    hook = min(1.2, duration * 0.10)
    plans = [(start + peak, hook, "hook")]
    left = duration - hook
    offset = 0.0
    index = 0
    while left > 0.005:
        step = min(left, 2.4)
        if 0 < left - step < 0.5:
            step = left
        plans.append((start + offset, step, "punch" if index % 3 == 1 else "wide"))
        offset += step
        left -= step
        index += 1
    if any(length <= 0 for _, length, _ in plans):
        raise RhythmError("Shot planner produced an invalid duration")
    return plans


def assemble(source, folder, start, duration, event, face, width, height):
    """Normalize source-only shots inside caller's private work directory."""
    from .video_pipeline import probe
    source = Path(source).resolve(strict=True)
    folder = Path(folder).resolve(strict=True)
    plan = plan_shots(start, duration, event)
    info = probe(source)
    parts = []
    for index, (shot_start, length, framing) in enumerate(plan):
        if shot_start < -0.01 or shot_start + length > info["duration"] + .15:
            raise RhythmError("Planned frame is outside the uploaded source")
        output = folder / f"shot-{index:02}.mp4"
        graph = _shot_filter(width, height, face=face, punch=framing in ("hook", "punch"))
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-threads", "2", "-filter_threads", "2", "-y",
            "-ss", str(shot_start), "-i", str(source),
        ]
        if not info["has_audio"]:
            # Silence is allowed for an audio-less video, not random music.
            command += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        command += [
            "-t", str(length), "-filter_complex", graph,
            "-map", "[outv]",
            "-map", "0:a:0" if info["has_audio"] else "1:a:0",
            "-c:v", "libx264", "-threads", "2",
            "-preset", "ultrafast", "-crf", "24",
            "-pix_fmt", "yuv420p", "-r", "24", "-fps_mode", "cfr",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "96k",
            "-movflags", "+faststart", "-shortest", str(output),
        ]
        _run(command)
        if not output.is_file() or output.stat().st_size < 1024:
            raise RhythmError("Expected shot was not created")
        parts.append(output)
    listing = folder / "shots.txt"
    listing.write_text(
        "".join(f"file '{part.name}'\n" for part in parts), encoding="utf-8",
    )
    output = folder / "sequence.mp4"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-y", "-f", "concat", "-safe", "0",
        "-i", str(listing), "-c", "copy",
        "-movflags", "+faststart", str(output),
    ], timeout=150)
    final = probe(output)
    if (final["duration"] < duration - 0.7
            or not final["has_audio"]
            or final["width"] != width or final["height"] != height):
        raise RhythmError("Normalized montage failed frame/audio integrity check")
    return output
