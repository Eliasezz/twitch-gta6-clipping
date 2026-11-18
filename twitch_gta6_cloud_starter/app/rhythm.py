"""Build an edit decision list in source time, then render one shot at a time."""
import math
import re
import subprocess
from pathlib import Path


def active_ranges(source, start, length, audio):
    if not audio:
        return [(start, start + length)]
    proc = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-ss", str(start), "-i", str(source),
        "-t", str(length), "-map", "0:a:0", "-af", "silencedetect=n=-38dB:d=0.45",
        "-f", "null", "-"
    ], capture_output=True, text=True, check=True, timeout=90)
    cursor, ranges = 0., []
    for kind, timestamp in re.findall(r"silence_(start|end):\s*([-\d.]+)", proc.stderr):
        t = min(length, max(0., float(timestamp)))
        if kind == "start":
            if t - cursor >= .12:
                ranges.append((start + cursor, start + t))
            cursor = length
        else:
            cursor = t
    if length - cursor >= .12:
        ranges.append((start + cursor, start + length))
    return ranges


def plan(ranges, event, duration):
    from .video_pipeline import VideoPipelineError
    if not ranges:
        raise VideoPipelineError("Aucun passage audible dans la fenêtre sélectionnée.")
    peak = min(ranges, key=lambda r: max(r[0]-event, event-r[1], 0))
    hook = max(peak[0], min(event-.3, peak[1]-1.25))
    end = min(peak[1], hook+1.25)
    ordered = [(hook, end)]
    for a,b in ranges:
        if (a,b) == peak:
            ordered.extend([(a,hook),(end,b)])
        else:
            ordered.append((a,b))
    shots = []
    remaining = duration
    for a,b in ordered:
        length = min(b-a, remaining)
        if length < .10:
            continue
        pieces = max(1, math.ceil(length/2.9))
        shots.extend((a+i*length/pieces,length/pieces) for i in range(pieces))
        remaining -= length
        if remaining < .10:
            break
    if sum(t for _,t in shots) < 19.96:
        raise VideoPipelineError("Moins de 20 secondes exploitables après suppression des silences.")
    return shots


def assemble(source, folder, shots, face, width, height):
    files = []
    for index, (offset, length) in enumerate(shots):
        zoom = (1.12, 1., 1.05, 1.10)[index % 4]
        if face:
            game_height, cam_height = height//2, height*3//8
            filters = (
                "split=3[bg][g][c];"
                f"[bg]scale=108:192:force_original_aspect_ratio=increase,crop=108:192,"
                f"boxblur=8:2,scale={width}:{height},setsar=1[base];"
                f"[g]crop=iw/{zoom}:ih/{zoom},scale={width}:{game_height}:force_original_aspect_ratio=decrease,"
                f"setsar=1[game];[base][game]overlay=(W-w)/2:({game_height}-h)/2[layer];"
                f"[c]crop=iw*.33:ih*.46:0:ih-oh,crop=iw/{zoom}:ih/{zoom},"
                f"scale={width}:{cam_height}:force_original_aspect_ratio=decrease,setsar=1[cam];"
                f"[layer][cam]overlay=(W-w)/2:{game_height}+({cam_height}-h)/2"
            )
        else:
            filters = (f"crop=iw/{zoom}:ih/{zoom},scale={width}:{height}:force_original_aspect_ratio=increase,"
                       f"crop={width}:{height}")
        out = Path(folder) / f"shot-{index}.mp4"
        # Split/overlay requires filter_complex; -vf does not support
        # multi-chain graphs. Inputs remain exclusively source job media.
        from .video_pipeline import probe
        has_audio = probe(source)["has_audio"]
        command = [
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "1",
            "-ss", str(offset), "-i", str(source),
        ]
        if not has_audio:
            # An actual audio-less source gets silence, never random media.
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        command += ["-t", str(length), "-filter_threads", "1"]
        if face:
            command += ["-filter_complex",
                        filters + ",setsar=1,fps=24[outv]",
                        "-map", "[outv]"]
        else:
            command += ["-vf", filters + ",setsar=1,fps=24",
                        "-map", "0:v:0"]
        command += [
            "-map", "0:a:0" if has_audio else "1:a:0",
            "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
            "-r", "24", "-fps_mode", "cfr", "-preset", "ultrafast", "-crf", "22",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
            str(out),
        ]
        subprocess.run(command, check=True, capture_output=True, timeout=180)
        files.append(out)
    listing = Path(folder) / "shots.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in files))
    result = Path(folder) / "sequence.mp4"
    subprocess.run(["ffmpeg","-v","error","-nostdin","-y","-f","concat","-safe","0",
                    "-i",str(listing),"-c","copy",str(result)],check=True,capture_output=True,timeout=90)
    from .video_pipeline import probe
    rendered = probe(result)
    if not rendered["has_audio"] or rendered["width"] != width or rendered["height"] != height:
        raise RuntimeError("The source-only montage has invalid picture or missing audio")
    return result
