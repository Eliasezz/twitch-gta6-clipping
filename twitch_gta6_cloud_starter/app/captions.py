"""English captions using an OpenAI key OR Render OIDC workload identity.

Whisper-1 word timestamps support English-language synchronization until
its scheduled February 2027 retirement. Non-English translation currently
uses approximate segment timing. Calls require explicitly configured auth.
"""
import math
import os
import re
import subprocess
from pathlib import Path



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


def openai_auth_status():
    """Return non-secret setup readiness, not proof that OpenAI accepted it."""
    key_present = bool(os.getenv("OPENAI_API_KEY", "").strip())
    provider = os.getenv("OPENAI_IDENTITY_PROVIDER_ID", "").strip()
    account = os.getenv("OPENAI_SERVICE_ACCOUNT_ID", "").strip()
    token_path = os.getenv("OPENAI_IDENTITY_TOKEN_FILE", "").strip()
    # The token path is created automatically by Render; never set it manually.
    token_available = bool(token_path and Path(token_path).is_file())
    oidc_ready = bool(provider and account and token_available)
    method = (
        "workload_identity" if oidc_ready
        else "api_key" if key_present
        else "unconfigured"
    )
    return {
        "configured": bool(oidc_ready or key_present),
        "method": method,
        "api_key_present": key_present,
        "identity_provider_id_present": bool(provider),
        "service_account_id_present": bool(account),
        "identity_token_file_available": token_available,
        "oidc_ready": oidc_ready,
    }


def _openai_client():
    """Use OIDC when ready, otherwise the existing environment API key."""
    status = openai_auth_status()
    if not status["configured"]:
        raise CaptionError(
            "Configure OPENAI_API_KEY or complete OpenAI Render workload identity"
        )
    from openai import OpenAI
    if status["oidc_ready"]:
        # Read the file for each token exchange; never cache a short-lived JWT.
        return OpenAI(
            workload_identity={
                "identity_provider_id": os.environ["OPENAI_IDENTITY_PROVIDER_ID"],
                "service_account_id": os.environ["OPENAI_SERVICE_ACCOUNT_ID"],
                "provider": {
                    "token_type": "jwt",
                    "get_token": lambda: Path(
                        os.environ["OPENAI_IDENTITY_TOKEN_FILE"]
                    ).read_text(encoding="utf-8").strip(),
                },
            },
            timeout=180.0,
            max_retries=1,
        )
    return OpenAI(
        api_key=os.environ["OPENAI_API_KEY"].strip(),
        timeout=180.0,
        max_retries=1,
    )


def _request_whisper(audio_file: Path, *, translate=False):
    # The official Python SDK manages both OIDC token exchange and API keys.
    from openai import APIConnectionError, APIStatusError, OpenAIError
    try:
        client = _openai_client()
        with audio_file.open("rb") as stream:
            if translate:
                response = client.audio.translations.create(
                    model="whisper-1",
                    file=stream,
                    response_format="verbose_json",
                )
            else:
                response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=stream,
                    response_format="verbose_json",
                    timestamp_granularities=["word"],
                )
        payload = response.model_dump(exclude_none=True)
    except APIStatusError as exc:
        if exc.status_code in (401, 403):
            raise CaptionError("OpenAI authentication rejected; verify identity mapping or API key") from exc
        if exc.status_code == 429:
            raise CaptionError("OpenAI quota or transcription rate limit exceeded") from exc
        raise CaptionError(f"OpenAI audio API failed with HTTP {exc.status_code}") from exc
    except APIConnectionError as exc:
        raise CaptionError("Could not connect to the OpenAI audio API") from exc
    except OpenAIError as exc:
        raise CaptionError("OpenAI audio transcription request failed") from exc
    if not isinstance(payload, dict):
        raise CaptionError("OpenAI transcription response was not an object")
    return payload


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
    except subprocess.TimeoutExpired as exc:
        raise CaptionError("Speech audio extraction timed out") from exc
    finally:
        audio.unlink(missing_ok=True)
