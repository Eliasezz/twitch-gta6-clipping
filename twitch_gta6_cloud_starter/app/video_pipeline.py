"""Offline, permission-first FFmpeg highlight renderer.

No unauthorised Twitch/YouTube downloads or automatic publishing.
Supplied plain-text captions have approximate timing, not speech recognition.
"""
import json
import math
import re
import shutil
import subprocess
from pathlib import Path


class VideoPipelineError(RuntimeError):
    pass


def _check_binary(name):
    binary = shutil.which(name)
    if not binary:
        raise VideoPipelineError(f"{name} is unavailable on this server")
    return binary


def _execute(command, timeout=240):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise VideoPipelineError("FFmpeg operation timed out") from exc
    if result.returncode:
        raise VideoPipelineError(f"Media operation failed (exit {result.returncode}): {result.stderr[-800:]}")
    return result


def probe(path):
    result = _execute([
        _check_binary("ffprobe"), "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_type,width,height",
        "-of", "json", str(path)
    ], timeout=30)
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        video = next(stream for stream in payload["streams"] if stream["codec_type"] == "video")
        width, height = int(video["width"]), int(video["height"])
    except (KeyError, StopIteration, ValueError, TypeError) as exc:
        raise VideoPipelineError("Input is not a playable video") from exc
    if not math.isfinite(duration) or duration <= 0 or width < 16 or height < 16:
        raise VideoPipelineError("Invalid video duration or dimensions")
    return {"duration": duration, "width": width, "height": height}


def pick_highlight_start(path, duration, clip_seconds):
    """Maximize non-silent audio; this does not claim semantic/viral ranking."""
    if duration <= clip_seconds:
        return 0.0, "entire_video"
    analysis_span = min(duration, 180.0)
    result = subprocess.run([
        _check_binary("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path),
        "-t", str(analysis_span), "-vn", "-af", "silencedetect=noise=-32dB:d=0.35",
        "-f", "null", "-"
    ], capture_output=True, text=True, timeout=80, check=False)
    if result.returncode:
        return 0.0, "first_window_fallback"
    intervals = []
    begin = None
    for line in result.stderr.splitlines():
        match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if match:
            begin = float(match.group(1))
        match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if match and begin is not None:
            intervals.append((begin, float(match.group(1))))
            begin = None
    if begin is not None:
        intervals.append((begin, analysis_span))
    max_start = max(0.0, analysis_span - clip_seconds)
    candidates = [min(float(i * 3), max_start) for i in range(int(max_start // 3) + 2)]
    best_start, best_score = 0.0, float("-inf")
    for candidate in candidates:
        silence = sum(max(0.0, min(candidate + clip_seconds, end) - max(candidate, start))
                      for start, end in intervals)
        score = clip_seconds - silence - candidate * 0.0001
        if score > best_score:
            best_start, best_score = candidate, score
    return round(best_start, 3), "audio_activity_heuristic"


def _ass_escape(value):
    value = re.sub(r"[\r\n]+", " ", value)
    return value.replace("\\", "\\\\").replace("{", "(").replace("}", ")").strip()


def _ass_time(seconds):
    centis = max(0, int(round(seconds * 100)))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def make_ass(caption_text, duration, destination, width=720, height=1280):
    """English-colored subtitles with approximate timing for supplied text."""
    words = re.findall(r"\S+", caption_text or "")[:160]
    if not words:
        return False
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hype,DejaVu Sans,54,&H00FFFFFF,&H0000FFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,3,1,2,35,35,220,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    allotted = max(0.4, duration - 0.2)
    max_cards = max(1, int(allotted / 0.45))
    chunks = chunks[:max_cards]
    card_length = allotted / len(chunks)
    lines = []
    for index, chunk in enumerate(chunks):
        start = index * card_length
        end = min(duration, (index + 1) * card_length)
        if end - start < 0.12:
            continue
        rendered = []
        for word_index, word in enumerate(chunk):
            clean = _ass_escape(word.upper())
            if word_index == 1 or (index + word_index) % 5 == 0:
                rendered.append(r"{\c&H0000EFFF&}" + clean + r"{\c&H00FFFFFF&}")
            else:
                rendered.append(clean)
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Hype,,0,0,0,,{' '.join(rendered)}")
    destination.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return bool(lines)


def render_mp4(source, destination, *, caption_text="", clip_seconds=65, width=720, height=1280,
               dynamic_zoom=True):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise VideoPipelineError("Source video not found")
    if clip_seconds < 3 or clip_seconds > 90:
        raise VideoPipelineError("clip_seconds must be between 3 and 90")
    if (width, height) not in {(360, 640), (540, 960), (720, 1280)}:
        raise VideoPipelineError("Unsupported output format")
    info = probe(source)
    if info["duration"] > 900:
        raise VideoPipelineError("Source exceeds 15-minute processing limit")
    # Never fake a minute-long eligible-looking clip by stretching or looping short footage.
    if clip_seconds >= 61 and info["duration"] < float(clip_seconds) - 0.1:
        raise VideoPipelineError(
            f"Source is only {info['duration']:.1f} seconds; upload at least {clip_seconds} seconds of footage"
        )
    duration = min(float(clip_seconds), info["duration"])
    start, selection = pick_highlight_start(source, info["duration"], duration)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ass_file = destination.with_suffix(".ass")
    has_captions = make_ass(caption_text, duration, ass_file, width, height)
    filters = [
        "fps=24",
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1"
    ]
    if dynamic_zoom:
        filters.append(
            f"zoompan=z='1.025+0.045*abs(sin(on/15))':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps=24"
        )
    if has_captions:
        filters.append(f"ass={ass_file.as_posix()}")
    command = [
        _check_binary("ffmpeg"), "-hide_banner", "-nostdin", "-y",
        "-ss", str(start), "-i", str(source), "-t", str(duration),
        "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(filters),
        "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-crf", "25",
        "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", "-shortest", str(destination)
    ]
    try:
        _execute(command, timeout=720 if duration >= 45 else 270)
        output_info = probe(destination)
        if output_info["width"] != width or output_info["height"] != height:
            raise VideoPipelineError("Output geometry failed validation")
        if not 0.5 < output_info["duration"] <= duration + 1.5:
            raise VideoPipelineError("Output duration failed validation")
        if destination.stat().st_size < 1024:
            raise VideoPipelineError("Output MP4 was unexpectedly small")
        return {
            "file": destination.name, "size_bytes": destination.stat().st_size,
            "duration_seconds": round(output_info["duration"], 2),
            "start_seconds": start, "selection_mode": selection,
            "width": width, "height": height, "fps": 24,
            "caption_mode": "approximate_supplied_text" if has_captions else "none",
            "dynamic_zoom": dynamic_zoom,
            "minute_plus": output_info["duration"] > 60.0,
        }
    finally:
        ass_file.unlink(missing_ok=True)
