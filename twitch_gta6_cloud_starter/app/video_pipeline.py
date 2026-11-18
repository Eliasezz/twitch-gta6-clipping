"""Offline, permission-first FFmpeg highlight renderer.

No unauthorised Twitch/YouTube downloads or automatic publishing.
Supplied plain-text captions have approximate timing, not speech recognition.
"""
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path


# Source files are capped at 300 MB by the browser upload endpoint.
# A long Twitch VOD can be processed without decoding the entire recording.
MAX_SOURCE_SECONDS = 12 * 3600


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
    has_audio = any(x.get("codec_type") == "audio" for x in payload["streams"])
    return {"duration": duration, "width": width, "height": height, "has_audio": has_audio}


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


def make_ass(caption_text, duration, destination, width=720, height=1280,
             timed_words=None, hook_text=""):
    """English-colored subtitles with approximate timing for supplied text."""
    words = re.findall(r"\S+", caption_text or "")[:160]
    if not words and not timed_words and not hook_text:
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
Style: Hook,DejaVu Sans,44,&H00FFFFFF,&H0000FFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,3,1,8,45,45,140,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    extras = []
    if hook_text:
        hook = _ass_escape(str(hook_text).upper())[:80]
        if hook:
            extras.append(
                f"Dialogue: 0,0:00:00.00,{_ass_time(min(duration, 3.2))},Hook,,0,0,0,,{hook}"
            )
    if timed_words:
        # Each event starts/ends at the ASR word boundaries. Show 3-word
        # context and highlight the currently spoken English word.
        validated = []
        for entry in timed_words[:600]:
            try:
                begin = max(0.0, float(entry["start"]))
                finish = min(float(duration), float(entry["end"]))
                token = _ass_escape(str(entry["word"]).upper())
            except (KeyError, ValueError, TypeError):
                continue
            if token and finish > begin + 0.04:
                validated.append((begin, finish, token))
        lines = []
        for i, (begin, finish, _) in enumerate(validated):
            group_start = (i // 3) * 3
            group = validated[group_start:group_start + 3]
            text_parts = []
            for j, (_, _, token) in enumerate(group):
                if j + group_start == i:
                    text_parts.append(r"{\c&H0000EFFF&}" + token + r"{\c&H00FFFFFF&}")
                else:
                    text_parts.append(token)
            lines.append(
                f"Dialogue: 0,{_ass_time(begin)},{_ass_time(finish)},Hype,,0,0,0,,{' '.join(text_parts)}"
            )
        destination.write_text(header + "\n".join(extras + lines) + "\n", encoding="utf-8")
        return bool(extras or lines)
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
    destination.write_text(header + "\n".join(extras + lines) + "\n", encoding="utf-8")
    return bool(extras or lines)


def render_mp4(source, destination, *, caption_text="", clip_seconds=65, width=720, height=1280,
               dynamic_zoom=True, auto_subtitles=False, music_enabled=False, hook_text=""):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise VideoPipelineError("Source video not found")
    if clip_seconds < 3 or clip_seconds > 90:
        raise VideoPipelineError("clip_seconds must be between 3 and 90")
    if (width, height) not in {(360, 640), (540, 960), (720, 1280)}:
        raise VideoPipelineError("Unsupported output format")
    info = probe(source)
    if info["duration"] > MAX_SOURCE_SECONDS:
        raise VideoPipelineError("Source exceeds 12-hour input limit")
    # Never fake a minute-long eligible-looking clip by stretching or looping short footage.
    if clip_seconds >= 61 and info["duration"] < float(clip_seconds) - 0.1:
        raise VideoPipelineError(
            f"Source is only {info['duration']:.1f} seconds; upload at least {clip_seconds} seconds of footage"
        )
    duration = min(float(clip_seconds), info["duration"])
    start, selection = pick_highlight_start(source, info["duration"], duration)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ass_file = destination.with_suffix(".ass")
    subtitle_mode, detected_language = "none", None
    reaction_score = None
    transcript_warning = None
    if auto_subtitles:
        # Subtitle API failures must not destroy a licensed edit. Render the
        # real source audio, original music and intro; never invent dialogue.
        from .captions import CaptionError, transcribe_english
        from .highlight import choose_speech_window, window_words
        try:
            analysis_duration = min(info["duration"], max(180.0, duration))
            transcript_words, subtitle_mode, detected_language = transcribe_english(
                source, start=0.0, duration=analysis_duration,
                scratch_dir=destination.parent,
            )
            new_start, speech_rank = choose_speech_window(
                transcript_words, analysis_duration, duration,
            )
            if new_start is not None:
                start = new_start
                selection = speech_rank["method"]
                reaction_score = speech_rank.get("reaction_score")
            aligned_words = window_words(transcript_words, start, duration)
            has_captions = make_ass(
                "", duration, ass_file, width, height,
                timed_words=aligned_words, hook_text=hook_text,
            )
            if not has_captions:
                raise CaptionError("No real word timestamps in the chosen highlight")
        except CaptionError as exc:
            # A 429 can mean insufficient API credits, not an expired key.
            # Honest fallback: no fabricated transcript or translation.
            detail = str(exc).lower()
            if "quota" in detail or "rate limit" in detail:
                transcript_warning = "openai_quota_or_rate_limit"
            elif "authentication" in detail or "unauthorized" in detail:
                transcript_warning = "openai_authentication"
            else:
                transcript_warning = "speech_transcription_unavailable"
            subtitle_mode = "not_available"
            detected_language = None
            selection = "audio_activity_fallback"
            start, _ = pick_highlight_start(source, info["duration"], duration)
            reaction_score = None
            has_captions = make_ass(
                "", duration, ass_file, width, height, hook_text=hook_text,
            )
    else:
        has_captions = make_ass(caption_text, duration, ass_file, width, height,
                                hook_text=hook_text)
        subtitle_mode = "approximate_supplied_text" if caption_text and has_captions else "none"
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
    bed_path = destination.with_suffix(".original-bed.wav")
    if music_enabled:
        from .music import make_original_beat
        make_original_beat(bed_path)
    command = [
        _check_binary("ffmpeg"), "-hide_banner", "-nostdin", "-y",
        "-ss", str(start), "-i", str(source),
    ]
    if music_enabled:
        command.extend(["-stream_loop", "-1", "-i", str(bed_path)])
    command.extend(["-t", str(duration), "-map", "0:v:0"])
    if music_enabled:
        if info["has_audio"]:
            # Keep the original voice foreground; softly mix original instrumental.
            command.extend([
                "-filter_complex",
                "[0:a:0]volume=1.0[voice];[1:a:0]volume=0.19[bed];"
                "[voice][bed]amix=inputs=2:duration=first:normalize=0[aout]",
                "-map", "[aout]",
            ])
        else:
            command.extend(["-map", "1:a:0"])
    else:
        command.extend(["-map", "0:a:0?"])
    command.extend([
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-crf", "25",
        "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", "-shortest", str(destination),
    ])
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
            "caption_mode": subtitle_mode,
            "detected_language": detected_language,
            "reaction_keyword_score": reaction_score,
            "transcription_warning": transcript_warning,
            "speech_words_synchronized": subtitle_mode == "english_word_synced",
            "original_music_mixed": bool(music_enabled),
            "hook_text": hook_text[:80],
            "dynamic_zoom": dynamic_zoom,
            "minute_plus": output_info["duration"] > 60.0,
        }
    finally:
        ass_file.unlink(missing_ok=True)
        bed_path.unlink(missing_ok=True)
