"""CapCut handoff metadata and editable SRT captions, with no external calls."""
import re
from pathlib import Path

_ASS_STAMP = re.compile(r"^(\\d+):(\\d{1,2}):(\\d{2})\\.(\\d{2})$")
_ASS_OVERRIDE = re.compile(r"\\{[^}]*\\}")


def _ass_to_ms(value):
    match = _ASS_STAMP.fullmatch(value.strip())
    if not match:
        raise ValueError("Invalid ASS timestamp")
    hours, minutes, seconds, centiseconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Invalid ASS timestamp")
    return (((hours * 60 + minutes) * 60 + seconds) * 1000 + centiseconds * 10)


def _srt_time(milliseconds):
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def ass_to_srt(ass_path, srt_path):
    """Convert Hype caption events to UTF-8 SRT, excluding hook/title graphics."""
    ass_path, srt_path = Path(ass_path), Path(srt_path)
    if not ass_path.is_file() or ass_path.is_symlink():
        return False
    entries = []
    for line in ass_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(":", 1)[1].lstrip().split(",", 9)
        if len(fields) != 10 or fields[3].strip() != "Hype":
            continue
        try:
            start, end = _ass_to_ms(fields[1]), _ass_to_ms(fields[2])
        except ValueError:
            continue
        content = _ASS_OVERRIDE.sub("", fields[9])
        content = content.replace(r"\\N", " ").replace(r"\\h", " ").strip()
        if end <= start or not content:
            continue
        entries.append((start, end, content))
    if not entries:
        srt_path.unlink(missing_ok=True)
        return False
    srt_path.write_text("\n".join(
        f"{n}\n{_srt_time(begin)} --> {_srt_time(end)}\n{caption}\n"
        for n, (begin, end, caption) in enumerate(entries, 1)
    ), encoding="utf-8")
    return True


def editor_manifest(job_id, status):
    """Factual handoff; metadata never claims that CapCut has edited a video."""
    result = status.get("result") or {}
    rendered = result.get("clips") or [result]
    clips = []
    for index, data in enumerate(rendered, 1):
        filename = "vertical.mp4" if index == 1 else f"vertical-{index}.mp4"
        srtname = "vertical.srt" if index == 1 else f"vertical-{index}.srt"
        clips.append({
            "number": index,
            "rendered_video": f"montage_{index}_9x16.mp4" if index > 1 else "montage_9x16.mp4",
            "capcut_srt": f"clip_{index:02}.srt" if data.get("editable_srt") else None,
            "source_start_seconds": data.get("start_seconds"),
            "duration_seconds": data.get("duration_seconds"),
            "edit_mode": data.get("selection_mode"),
            "source_shots": data.get("source_shots", []),
            "subtitle_mode": data.get("caption_mode"),
            "captions_are_burned_into_video": data.get("caption_mode") not in ("none", "not_available"),
            "source_video_filename": status.get("original_filename"),
            "rendered_filename": filename,
            "subtitle_sidecar_filename": srtname if data.get("editable_srt") else None,
        })
    return {
        "job_id": job_id,
        "type": "CapCut-ready export package (not a native CapCut project)",
        "version": 1,
        "editing_roles": [
            "Scout: analyses sampled source audio, frames and optional chat.",
            "Hook: places detected events first when rhythmic editing is enabled.",
            "Rhythm: chooses short cuts sourced only from the uploaded video.",
            "Captions: transcribes only if available; never invents spoken words.",
            "Quality: validates output geometry, duration and integrity.",
        ],
        "clips": clips,
        "note": (
            "MP4s already include rendered captions when present. Import SRT "
            "for corrections/reference but do not overlay it on that MP4, "
            "as duplicate captions would appear."
        ),
    }


CAPCUT_README = """PACK CAPCUT - GTA VI

Les MP4 proviennent de FFmpeg, pas de CapCut. Ce ZIP ne contient
pas de projet CapCut natif.

1. Sur CapCut Desktop ou Web, creer un projet 9:16.
2. Importer montage_9x16.mp4 et les autres MP4 disponibles.
3. Importer clip_XX.srt via Sous-titres si fourni.
   Attention : les MP4 peuvent avoir deja des sous-titres incrustes.
   Ne pas superposer les deux couches.
4. Ouvrir plan_montage.json pour retrouver les timecodes et les coupes.
5. Regler manuellement les transitions, les effets, le cadrage et
   les sons autorises, puis verifier chaque clip avant publication.

Le moteur actuel accepte jusqu'a 3 clips par upload et les traite
sequentiellement pour respecter les ressources Render.
La collaboration en temps reel dans CapCut n'est pas integree.
"""
