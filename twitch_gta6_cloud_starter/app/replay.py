"""Sample public GTA VI replay windows from creators registered in the studio."""
import os
import re
import signal
import subprocess
import sys
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone

from .twitch_connection import allowed_creators


def candidates(db, now=None):
    from .main import twitch_get
    now = now or datetime.now(timezone.utc)
    items = []
    for creator in sorted(allowed_creators()):
        streams = twitch_get("streams", {"user_id": creator})["data"]
        for stream in streams:
            if (stream.get("user_id") == creator and stream.get("game_name") == "Grand Theft Auto VI"
                    and re.fullmatch(r"[a-zA-Z0-9_]+", stream.get("user_login", ""))
                    and stream.get("id", "").isdigit()):
                key = f"live-{stream['id']}-{int(now.timestamp()) // 900}"
                if not db.execute("SELECT 1 FROM overnight_clip_jobs WHERE clip_id=?", (key,)).fetchone():
                    items.append(dict(id=key, broadcaster_id=creator, broadcaster_name=stream["user_name"],
                                      title=stream["title"], stream_id=stream["id"],
                                      stream_started_at=stream["started_at"],
                                      url="https://www.twitch.tv/"+stream["user_login"]))
        videos = twitch_get("videos", {"user_id": creator, "type": "archive", "first": 3})["data"]
        for video in videos:
            if video["user_id"] != creator or not video["id"].isdigit():
                continue
            if not re.search(r"\b(?:GTA|Grand Theft Auto)\s*(?:6|VI)\b", video["title"], re.I):
                continue
            started = datetime.fromisoformat(video["created_at"].replace("Z", "+00:00"))
            duration = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", video["duration"])
            if not duration or not timedelta(0) <= now-started <= timedelta(days=2):
                continue
            h, m, s = (int(x or 0) for x in duration.groups())
            length = 3600*h + 60*m + s
            # Work backwards from indexed footage, at most one sample per quarter hour.
            for end in range(length - 10, 59, -900):
                bucket = end // 900
                key = f"vod-{video['id']}-{bucket}"
                if db.execute("SELECT 1 FROM overnight_clip_jobs WHERE clip_id=?", (key,)).fetchone():
                    continue
                items.append(dict(id=key, broadcaster_id=creator, broadcaster_name=video["user_name"],
                                  title=video["title"], video_id=video["id"], vod_offset=end-60,
                                  video_created_at=video["created_at"], url="https://www.twitch.tv/videos/"+video["id"]))
                break
    return items


def download(item, destination):
    if item["broadcaster_id"] not in allowed_creators():
        raise ValueError("Source Twitch non autorisée")
    if item.get("stream_id"):
        # Only accept the live identity that Helix just matched to the authorized channel.
        from .main import twitch_get
        streams = twitch_get("streams", {"user_id": item["broadcaster_id"]})["data"]
        if not any(st["id"] == item["stream_id"] and st["game_name"] == "Grand Theft Auto VI"
                   and item["url"] == "https://www.twitch.tv/"+st["user_login"] for st in streams):
            raise ValueError("Le live a changé ou s’est terminé")
        lookup = subprocess.run([sys.executable, "-m", "yt_dlp", "--ignore-config", "-g",
                                 "-f", "best[height<=720]", item["url"]],
                                capture_output=True, text=True, timeout=40)
        media = lookup.stdout.strip()
        if lookup.returncode or len(media.splitlines()) != 1 or urlsplit(media).scheme != "https":
            raise ValueError("Flux Twitch public indisponible")
        result = subprocess.run(["ffmpeg","-nostdin","-y","-loglevel","error","-rw_timeout","15000000",
                                 "-i",media,"-t","60","-map","0:v:0","-map","0:a:0?",
                                 "-c","copy","-movflags","+faststart",str(destination)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=130)
        if result.returncode:
            raise ValueError("La capture Twitch a été interrompue")
        return
    if not item["video_id"].isdigit():
        raise ValueError("Identifiant de replay invalide")
    start = int(item["vod_offset"])
    args = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
            "--no-progress", "--socket-timeout", "15", "--retries", "1",
            "--download-sections", f"*{start}-{start+60}", "--force-keyframes-at-cuts",
            "-f", "best[height<=720]", "--remux-video", "mp4", "-o", str(destination),
            "https://www.twitch.tv/videos/" + item["video_id"]]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        if process.wait(timeout=200) != 0:
            raise RuntimeError("Replay inaccessible ou extrait indisponible")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
