import os
import time
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import FastAPI, HTTPException

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
POLL_INTERVAL = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "900")))
MAX_CLIPS = min(100, max(1, int(os.getenv("MAX_CLIPS_PER_POLL", "100"))))
DB_PATH = os.getenv("DATABASE_PATH", "/data/clips.sqlite3")
ALLOWED_BROADCASTERS = {
    x.strip() for x in os.getenv("ALLOWED_BROADCASTER_IDS", "").split(",") if x.strip()
}

TWITCH_API = "https://api.twitch.tv/helix"
OAUTH_URL = "https://id.twitch.tv/oauth2/token"

app = FastAPI(title="Twitch GTA VI Clips Radar", version="0.1.0")
_state: dict[str, Any] = {"last_poll": None, "last_error": None, "found": 0}
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                clip_id TEXT PRIMARY KEY,
                broadcaster_id TEXT NOT NULL,
                broadcaster_name TEXT,
                creator_name TEXT,
                title TEXT,
                url TEXT,
                thumbnail_url TEXT,
                view_count INTEGER DEFAULT 0,
                created_at TEXT,
                discovered_at TEXT NOT NULL,
                eligible_for_processing INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


def get_app_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Missing TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET")
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    response = requests.post(
        OAUTH_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    _token_cache["token"] = payload["access_token"]
    _token_cache["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    return _token_cache["token"]


def twitch_get(path: str, params: dict | None = None):
    token = get_app_token()
    response = requests.get(
        f"{TWITCH_API}/{path}",
        headers={"Client-Id": CLIENT_ID, "Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=25,
    )
    response.raise_for_status()
    return response.json()


def poll_once():
    # Resolve GTA VI by its current Twitch category name.
    games = twitch_get("games", {"name": "Grand Theft Auto VI"}).get("data", [])
    if not games:
        raise RuntimeError("Twitch did not return a game/category named Grand Theft Auto VI")
    game_id = games[0]["id"]

    payload = twitch_get("clips", {"game_id": game_id, "first": MAX_CLIPS})
    clips = payload.get("data", [])
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        for c in clips:
            broadcaster_id = str(c.get("broadcaster_id", ""))
            eligible = int(broadcaster_id in ALLOWED_BROADCASTERS)
            conn.execute("""
                INSERT INTO clips (
                    clip_id, broadcaster_id, broadcaster_name, creator_name, title,
                    url, thumbnail_url, view_count, created_at, discovered_at,
                    eligible_for_processing
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(clip_id) DO UPDATE SET
                    view_count=excluded.view_count,
                    eligible_for_processing=excluded.eligible_for_processing
            """, (
                c["id"], broadcaster_id, c.get("broadcaster_name"),
                c.get("creator_name"), c.get("title"), c.get("url"),
                c.get("thumbnail_url"), int(c.get("view_count", 0)),
                c.get("created_at"), now, eligible
            ))
        conn.commit()

    _state["last_poll"] = now
    _state["last_error"] = None
    _state["found"] = len(clips)
    return {"game_id": game_id, "clips_seen": len(clips),
            "eligible_by_allowlist": sum(
                1 for c in clips if str(c.get("broadcaster_id", "")) in ALLOWED_BROADCASTERS
            )}


def poll_loop():
    init_db()
    while True:
        try:
            poll_once()
        except Exception as exc:
            _state["last_error"] = str(exc)[:500]
        time.sleep(POLL_INTERVAL)


@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True, "service": "twitch-gta6-radar", **_state}


@app.get("/clips")
def list_clips(limit: int = 50, eligible_only: bool = False):
    limit = min(200, max(1, limit))
    with db() as conn:
        query = "SELECT * FROM clips"
        if eligible_only:
            query += " WHERE eligible_for_processing=1"
        query += " ORDER BY view_count DESC, created_at DESC LIMIT ?"
        rows = conn.execute(query, (limit,)).fetchall()
    return {"clips": [dict(row) for row in rows]}


@app.post("/poll")
def manual_poll():
    try:
        return poll_once()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
