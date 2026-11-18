"""Cut a short cold open from the selected event, then return to its lead-in."""
import subprocess
from pathlib import Path


def assemble(source, folder, start, duration, event, face, width, height):
    # Partition the same window into four shots; every source frame belongs to one shot.
    peak = min(duration * .78, max(duration * .22, event - start))
    hook = min(2.2, duration * .12)
    mid = peak * .55
    shots = [(peak, hook, "close"), (0, mid, "wide"), (mid, peak-mid, "push"),
             (peak+hook, duration-peak-hook, "wide")]
    files = []
    for index, (offset, length, framing) in enumerate(shots):
        if face and framing == "close":
            filters = f"crop=iw*.33:ih*.46:0:ih-oh,scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        elif face and framing == "wide":
            half = height // 2
            filters = (f"split[g][c];[g]scale={width}:{half}:force_original_aspect_ratio=increase,"
                       f"crop={width}:{half},setsar=1[top];[c]crop=iw*.33:ih*.46:0:ih-oh,"
                       f"scale={width}:{half},setsar=1[bottom];[top][bottom]vstack")
        else:
            zoom = .86 if framing in ("push", "close") else 1
            filters = (f"crop=iw*{zoom}:ih*{zoom},scale={width}:{height}:force_original_aspect_ratio=increase,"
                       f"crop={width}:{height}")
        out = Path(folder) / f"shot-{index}.mp4"
        cmd = ["ffmpeg","-loglevel","error","-nostdin","-y","-threads","1","-ss",str(start+offset),
               "-i",str(source),"-t",str(length),"-filter_threads","1","-vf",filters+",setsar=1,fps=24",
               "-map","0:v:0","-map","0:a:0?","-c:v","libx264","-threads","2","-pix_fmt","yuv420p",
               "-preset","ultrafast","-crf","20","-c:a","aac",str(out)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        files.append(out)
    listing = Path(folder) / "shots.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in files))
    result = Path(folder) / "sequence.mp4"
    subprocess.run(["ffmpeg","-loglevel","error","-nostdin","-y","-f","concat","-safe","1",
                    "-i",str(listing),"-c","copy",str(result)], check=True, capture_output=True, timeout=90)
    return result
