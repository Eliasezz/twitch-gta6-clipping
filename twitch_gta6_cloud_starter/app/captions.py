"""English caption extraction using a separately configured API key.

Whisper-1 word timestamps support English-language synchronization in 2026;
this API model is deprecated and must be replaced before its 2027 shutdown.
Non-English translation produces segment-timed English captions, not
individually aligned English words. No API calls without OPENAI_API_KEY.
"""
import math
import os
import re
import subprocess
from pathlib import Path

import requests


class CaptionError(RuntimeError):
    pass


def _run_ffmpeg_audio(source: Path, target: Path, start: float, duration: float):
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(max(0.0, start)), "-i", str(source), "-t", str(duration),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k",
        str(target),
    ], capture_output=True, text=True, timeout=120, check=False)
    if result.returncode or not target.exists() or target.stat().st_size < 100:
        raise CaptionError("Cannot extract speech audio from video")


def _request_whisper(audio_file: Path, *, translate=False):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise CaptionError("OPENAI_API_KEY is not configured")
    endpoint = "translations" if translate else "transcriptions"
    # Fixed API host; never send the key to user-supplied URLs or log its value.
    with audio_file.open("rb") as stream:
        data = {"model": "whisper-1", "response_format": "verbose_json"}
        if not translate:
            data["timestamp_granularities[]"] = "word"
        try:
            response = requests.post(
                "https://api.openai.com/v1/audio/" + endpoint,
                headers={"Authorization": "Bearer " + key},
                data=data,
                files={"file": (audio_file.name, stream, "audio/mpeg")},
                timeout=180,
            )
        except requests.RequestException as exc:
            raise CaptionError("Transcription network error") from exc
    if response.status_code in (401, 403):
        raise CaptionError("Transcription API key unauthorized")
    if response.status_code == 429:
        raise CaptionError("Transcription API rate limit or quota exceeded")
    if response.status_code >= 400:
        raise CaptionError("Transcription provider returned an error")
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Not JSON object")
        return payload
    except (ValueError, TypeError) as exc:
        raise CaptionError("Invalid transcription response") from exc


def _safe_word(word, start, end, duration):
    token = re.sub(r"\s+", " ", str(word)).strip()
    try:
        a, b = float(start), float(end)
    except (ValueError, TypeError) as exc:
        raise CaptionError("Bad caption timestamps") from exc
    if not (token and math.isfinite(a) and math.isfinite(b)):
        raise CaptionError("Bad caption text or timestamps")
    if b <= a or b < 0 or a >= duration:
        return None
    return {"word": token[:64], "start": max(0.0, a), "end": min(duration, b)}


def _segment_english_words(segments, duration):
    """Translate segments into approximate English timing, never claim word alignment."""
    words = []
    for segment in segments:
        try:
            start = float(segment["start"])
            end = float(segment["end"])
            tokens = str(segment.get("text") or "").split()
        except (KeyError, ValueError, TypeError):
            continue
        if not tokens or end <= start:
            continue
        for index, token in enumerate(tokens):
            a = start + (end - start) * index / len(tokens)
            b = start + (end - start) * (index + 1) / len(tokens)
            entry = _safe_word(token, a, b, duration)
            if entry:
                words.append(entry)
    return words[:600]


def transcribe_english(source, *, start=0.0, duration=65.0, scratch_dir=None):
    """Return (word timestamps relative to clip start, caption_mode, language).

    English original: word timestamps from Whisper transcription.
    Other languages: translated English subtitles timed by Whisper segments.
    """
    source = Path(source)
    scratch = Path(scratch_dir) if scratch_dir else source.parent
    scratch.mkdir(parents=True, exist_ok=True)
    audio = scratch / "speech-extract.mp3"
    try:
        _run_ffmpeg_audio(source, audio, start, duration)
        transcription = _request_whisper(audio, translate=False)
        language = str(transcription.get("language") or "unknown").strip().lower()
        words = []
        if language in {"english", "en"}:
            for item in transcription.get("words") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    entry = _safe_word(
                        item.get("word", ""), item.get("start"), item.get("end"), duration
                    )
                except CaptionError:
                    continue
                if entry:
                    words.append(entry)
            if not words:
                raise CaptionError("No English word timestamps returned")
            return words[:600], "english_word_synced", language

        # The translation endpoint returns timestamped segments (not word timestamps).
        # Translate ONLY when source language is not English.
        translation = _request_whisper(audio, translate=True)
        words = _segment_english_words(translation.get("segments") or [], duration)
        if not words:
            raise CaptionError("English translation has no timestamped segments")
        return words, "english_translated_segment_timed", language
    finally:
        audio.unlink(missing_ok=True)
