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
import tempfile
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
        "-format_whitelist", "mov,matroska,webm", "-protocol_whitelist", "file",
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
Style: Hype,DejaVu Sans,{round(width*.072)},&H00FFFFFF,&H0000FFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,3,1,2,{round(width*.10)},{round(width*.17)},{round(height*.24)},1
Style: Hook,DejaVu Sans,{round(width*.06)},&H00FFFFFF,&H0000FFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,3,1,8,45,45,140,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    extras = []
    if hook_text:
        hook = _ass_escape(" ".join(str(hook_text).split()[:7]).upper())[:80]
        if hook:
            extras.append(
                f"Dialogue: 0,0:00:00.00,{_ass_time(min(duration, 1.5))},Hook,,0,0,0,,{hook}"
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
        for begin, finish, token in validated:
            lines.append(f"Dialogue: 0,{_ass_time(begin)},{_ass_time(finish)},Hype,,0,0,0,,"
                         + r"{\c&H0000EFFF&}" + token)
        destination.write_text(header + "\n".join(extras + lines) + "\n", encoding="utf-8")
        return bool(extras or lines)
    chunks = [words[i:i + 3] for i in range(0, len(words), 3)]
    if not chunks:
        # A hook/title alone is a legitimate overlay when voice ASR fails.
        # Do not divide by zero; never invent dialogue or word timings.
        destination.write_text(header + "\n".join(extras) + "\n", encoding="utf-8")
        return bool(extras)
    allotted = max(0.4, duration - 0.2)
    max_cards = max(1, int(allotted / 0.45))
    chunks = chunks[:max_cards]
    if not chunks:
        destination.write_text(header + "\n".join(extras) + "\n", encoding="utf-8")
        return bool(extras)
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
               dynamic_zoom=True, auto_subtitles=False, music_enabled=False, hook_text="",
               subtitles_required=False, chat_seconds=(), vision_enabled=False, voiceover=False, section=(0, 1), face_bottom_left=False, rhythmic=False, prepared=False):
    source, destination = Path(source), Path(destination)
    if not source.is_file() or source.is_symlink():
        raise VideoPipelineError("Source for this job is missing or unsafe")
    if not source.resolve().is_relative_to(destination.parent.resolve()):
        raise VideoPipelineError("Source file is outside its own job folder")
    if clip_seconds < 3 or clip_seconds > 90:
        raise VideoPipelineError("clip_seconds must be between 3 and 90")
    if (width, height) not in {(360, 640), (540, 960), (720, 1280), (1080, 1920)}:
        raise VideoPipelineError("Unsupported output format")
    info = probe(source)
    if info["duration"] > MAX_SOURCE_SECONDS:
        raise VideoPipelineError("Source exceeds 12-hour input limit")
    if not prepared and info["duration"] < float(clip_seconds) - 0.1:
        raise VideoPipelineError(
            f"Source is only {info['duration']:.1f} seconds; upload at least {clip_seconds} seconds of footage"
        )
    duration = min(float(clip_seconds), info["duration"])
    from .highlight_signals import select_window
    if prepared:
        start, analysis = 0.0, {}
    else:
        start, analysis = select_window(source, info["duration"], duration, info["has_audio"], chat_seconds, vision_enabled, section)
    if (rhythmic or face_bottom_left) and duration >= 5:
        from .rhythm import active_ranges, assemble, plan
        section_end = info["duration"] * (section[0]+1) / section[1]
        section_begin = info["duration"] * section[0] / section[1]
        window_start = max(section_begin, min(start, section_end-duration*2))
        ranges = active_ranges(source, window_start, min(section_end-window_start, duration*2), info["has_audio"])
        shots = plan(ranges, analysis["event_seconds"], duration)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
            sequence = assemble(source, folder, shots, face_bottom_left, width, height)
            output = render_mp4(sequence, destination, caption_text=caption_text, clip_seconds=duration,
                                width=width, height=height, dynamic_zoom=False, auto_subtitles=auto_subtitles,
                                music_enabled=music_enabled, hook_text=hook_text, subtitles_required=subtitles_required,
                                voiceover=False, prepared=True)
        output.update(start_seconds=start, highlight_analysis=analysis, selection_mode="rhythmic_shorts",
                      source_shots=[{"start": round(a,3), "duration": round(b,3)} for a,b in shots])
        return output
    selection = "sampled_audio_visual_chat" if start or duration < info["duration"] else "entire_video"
    destination.parent.mkdir(parents=True, exist_ok=True)
    ass_file = destination.with_suffix(".ass")
    subtitle_mode, detected_language = "none", None
    caption_warning = None
    transcription_warning = None
    has_captions = False
    if auto_subtitles and (not prepared or info["has_audio"]):
        from .captions import CaptionError, transcribe_english
        try:
            if not info["has_audio"]:
                raise CaptionError("Source video has no audio track")
            words, subtitle_mode, detected_language = transcribe_english(
                source, start=start, duration=duration, scratch_dir=destination.parent,
            )
            has_captions = make_ass(
                "", duration, ass_file, width, height, timed_words=words, hook_text=hook_text,
            )
        except CaptionError as error:
            if subtitles_required:
                raise
            caption_warning = str(error)
            detail = caption_warning.lower()
            if "quota" in detail or "rate limit" in detail:
                transcription_warning = "openai_quota_or_rate_limit"
            elif "authentication" in detail or "unauthorized" in detail:
                transcription_warning = "openai_authentication"
            else:
                transcription_warning = "speech_transcription_unavailable"
            detected_language = None
    if not has_captions:
        has_captions = make_ass(caption_text, duration, ass_file, width, height, hook_text=hook_text)
        if caption_text.strip():
            subtitle_mode = "approximate_supplied_text"
        else:
            subtitle_mode = "not_available" if transcription_warning else "none"
    filters = [
        "fps=24",
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1"
    ]
    if face_bottom_left:
        half = height // 2
        filters = [
            "fps=24,split[game][camera];"
            f"[game]scale={width}:{half}:force_original_aspect_ratio=increase,crop={width}:{half},setsar=1[g];"
            f"[camera]crop=iw*0.33:ih*0.46:0:ih-oh,scale={width}:{half},setsar=1[c];"
            "[g][c]vstack"
        ]
    if dynamic_zoom and not face_bottom_left:
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
    elif not info["has_audio"]:
        # Silence is a valid audio track; no unrelated music or footage.
        command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
    narration = destination.with_suffix(".voice.wav")
    voice_ready = False
    if voiceover and hook_text.strip():
        try:
            with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
                Path(directory, "intro.txt").write_text(hook_text[:80], encoding="utf-8")
                subprocess.run([
                    _check_binary("ffmpeg"), "-v", "error", "-y", "-f", "lavfi",
                    "-i", "flite=textfile=intro.txt:voice=slt", "-t", str(duration),
                    str(narration.resolve()),
                ], cwd=directory, capture_output=True, check=True, timeout=20)
            voice_ready = True
        except (OSError, subprocess.SubprocessError):
            narration.unlink(missing_ok=True)
    if voice_ready:
        command.extend(["-i", str(narration)])
    command.extend(["-t", str(duration), "-map", "0:v:0"])
    tracks = []
    if info["has_audio"]:
        tracks.append("[0:a:0]volume=0.85[src]")
    if music_enabled:
        tracks.append("[1:a:0]volume=0.19[bed]")
    elif not info["has_audio"]:
        tracks.append("[1:a:0]volume=1.0[silence]")
    if voice_ready:
        tracks.append(f"[{2 if music_enabled else 1}:a:0]volume=1.4[narr]")
    if prepared and not tracks:
        raise VideoPipelineError("Source sans audio. Active la musique pour exporter avec une piste sonore.")
    if tracks:
        if prepared and info["has_audio"] and music_enabled:
            tracks = ["[0:a:0]asplit=2[src][key]", "[1:a:0]volume=0.14[music]",
                      "[music][key]sidechaincompress=threshold=0.035:ratio=7:attack=15:release=275[bed]"]
            labels = "[src][bed]"
            inputs = 2
        else:
            inputs = len(tracks)
            labels = "".join(track[track.rindex("["):] for track in tracks)
        mix = f"{labels}amix=inputs={inputs}:duration=longest:normalize=0,atrim=duration={duration},alimiter=limit=0.95"
        if prepared:
            target = "loudnorm=I=-14:TP=-1.5:LRA=9"
            measure = _execute(command + ["-filter_complex", ";".join(tracks + [mix+","+target+":print_format=json[m]"]),
                                "-map","[m]","-vn","-f","null","-"], timeout=150)
            stats = json.loads(measure.stderr[measure.stderr.rfind("{"):measure.stderr.rfind("}")+1])
            if not math.isfinite(float(stats["input_i"])):
                # Silence is expected for a legitimate source without audio.
                # Do not fabricate speech or fail a playable silent clip.
                mix += ",aresample=48000"
            else:
                for key, field in (("I","i"),("TP","tp"),("LRA","lra"),("thresh","thresh")):
                    target += f":measured_{key}=" + stats["input_"+field]
                mix += ","+target+":offset="+stats["target_offset"]+":linear=true,aresample=48000"
        command.extend(["-filter_complex_threads","1","-filter_complex", ";".join(tracks + [mix+"[aout]"]), "-map", "[aout]"])
    command.extend([
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-crf", "25",
        "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", str(destination),
    ])
    try:
        _execute(command, timeout=720 if duration >= 45 else 270)
        output_info = probe(destination)
        if output_info["width"] != width or output_info["height"] != height:
            raise VideoPipelineError("Output geometry failed validation")
        if not 0.5 < output_info["duration"] <= duration + 1.5:
            raise VideoPipelineError("Output duration failed validation")
        if prepared and (not output_info["has_audio"] or abs(output_info["duration"]-duration) > .25
                         or output_info["duration"] < 19.94 or output_info["duration"] > 45.10):
            raise VideoPipelineError("Export invalide : durée ou piste audio incorrecte.")
        if destination.stat().st_size < 1024:
            raise VideoPipelineError("Output MP4 was unexpectedly small")
        from .media_integrity import validate_mp4, IntegrityError
        try:
            verified = validate_mp4(
                destination, width=width, height=height, seconds=duration,
            )
        except IntegrityError as exc:
            destination.unlink(missing_ok=True)
            raise VideoPipelineError("Export verification failed: " + str(exc)) from exc
        return {
            "file": destination.name, "size_bytes": destination.stat().st_size,
            "output_validation": verified,
            "duration_seconds": round(output_info["duration"], 2),
            "start_seconds": start, "selection_mode": selection,
            "width": width, "height": height, "fps": 24,
            "caption_mode": subtitle_mode,
            "detected_language": detected_language,
            "reaction_keyword_score": None,
            "highlight_analysis": analysis,
            "caption_warning": caption_warning,
            "transcription_warning": transcription_warning,
            "speech_words_synchronized": subtitle_mode == "english_word_synced",
            "voiceover_mixed": voice_ready,
            "voiceover_warning": "Voix off indisponible." if voiceover and not voice_ready else None,
            "original_music_mixed": bool(music_enabled),
            "hook_text": hook_text[:80],
            "dynamic_zoom": dynamic_zoom and not face_bottom_left,
            "minute_plus": output_info["duration"] > 60.0,
        }
    finally:
        ass_file.unlink(missing_ok=True)
        narration.unlink(missing_ok=True)
        bed_path.unlink(missing_ok=True)
