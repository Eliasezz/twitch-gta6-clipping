
import os
import time
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from fastapi import FastAPI, HTTPException

# ---------------- CONFIGURATION ----------------

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()

POLL_INTERVAL = max(
    60, int(os.getenv("POLL_INTERVAL_SECONDS", "900"))
)

MAX_CLIPS = min(
    100, max(1, int(os.getenv("MAX_CLIPS_PER_POLL", "100")))
)

KNOWN_CLIPS_LIMIT = min(
    100, max(1, int(os.getenv("MAX_CLIPS_PER_STREAMER", "20")))
)

LOOKBACK_HOURS = max(
    1, int(os.getenv("CLIP_LOOKBACK_HOURS", "48"))
)

MIN_VIEWS = max(
    0, int(os.getenv("MIN_CLIP_VIEWS", "10"))
)

DB_PATH = os.getenv("DATABASE_PATH", "/data/clips.sqlite3")

KNOWN_STREAMERS = {
    x.strip().lower()
    for x in os.getenv(
        "KNOWN_STREAMERS",
        "ishowspeed,kaicenat,xqc,jynxzi,rubius"
    ).split(",")
    if x.strip()
}

TWITCH_API = "https://api.twitch.tv/helix"
OAUTH_URL = "https://id.twitch.tv/oauth2/token"

app = FastAPI(
    title="Twitch GTA VI Clips Radar",
    version="0.2.0"
)

_state: dict[str, Any] = {
    "last_poll": None,
    "last_error": None,
    "found": 0,
    "eligible": 0,
    "known_streamer_clips": 0,
    "discovered_streamer_clips": 0
}

_token_cache = {"token": None, "expires_at": 0}
_poll_lock = threading.Lock()


# ---------------- DATABASE ----------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
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
                eligible_for_processing INTEGER NOT NULL DEFAULT 0,
                source_type TEXT DEFAULT 'discovery'
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(clips)")
        }

        if "source_type" not in columns:
            conn.execute("""
                ALTER TABLE clips
                ADD COLUMN source_type TEXT DEFAULT 'discovery'
            """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_clips_created
            ON clips(created_at)
        """)

        conn.commit()


# ---------------- TWITCH AUTHENTICATION ----------------

def get_app_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "Missing TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET"
        )

    if (
        _token_cache["token"]
        and time.time() < _token_cache["expires_at"] - 60
    ):
        return _token_cache["token"]

    response = requests.post(
        OAUTH_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials"
        },
        timeout=20
    )

    response.raise_for_status()

    payload = response.json()

    _token_cache["token"] = payload["access_token"]
    _token_cache["expires_at"] = (
        time.time() + int(payload.get("expires_in", 3600))
    )

    return _token_cache["token"]


def twitch_get(path: str, params=None):
    token = get_app_token()

    response = requests.get(
        f"{TWITCH_API}/{path}",
        headers={
            "Client-Id": CLIENT_ID,
            "Authorization": f"Bearer {token}"
        },
        params=params or {},
        timeout=25
    )

    response.raise_for_status()
    return response.json()


# ---------------- STREAMER DISCOVERY ----------------

def resolve_known_streamers():
    """Convertit les noms Twitch en identifiants."""

    if not KNOWN_STREAMERS:
        return {}, []

    params = [
        ("login", login)
        for login in sorted(KNOWN_STREAMERS)
    ]

    result = twitch_get("users", params)

    users = {
        user["login"].lower(): user["id"]
        for user in result.get("data", [])
    }

    missing = sorted(KNOWN_STREAMERS - set(users.keys()))

    return users, missing


# ---------------- CLIP COLLECTION ----------------

def collect_category_clips(game_id, started_at, ended_at):
    """Découvre des clips GTA VI de tous les créateurs."""

    result = twitch_get(
        "clips",
        {
            "game_id": game_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "first": MAX_CLIPS
        }
    )

    return result.get("data", [])


def collect_known_streamer_clips(
    broadcaster_id, started_at, ended_at
):
    """Récupère les clips récents d'un streamer connu."""

    result = twitch_get(
        "clips",
        {
            "broadcaster_id": broadcaster_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "first": KNOWN_CLIPS_LIMIT
        }
    )

    return result.get("data", [])


# ---------------- CLIP STORAGE ----------------

def save_clips(clips, known_ids):
    now = datetime.now(timezone.utc).isoformat()
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=LOOKBACK_HOURS
    )

    saved = 0
    eligible_count = 0
    known_count = 0
    discovery_count = 0

    with db() as conn:
        for clip in clips:
            broadcaster_id = str(
                clip.get("broadcaster_id", "")
            )

            created_at = clip.get("created_at")
            if not created_at:
                continue

            try:
                clip_date = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                )

                if clip_date < cutoff:
                    continue

            except (ValueError, TypeError):
                continue

            view_count = int(clip.get("view_count", 0))

            # Les clips connus doivent également être des clips GTA VI.
            source_type = (
                "known"
                if broadcaster_id in known_ids
                else "discovery"
            )

            if source_type == "known":
                known_count += 1
            else:
                discovery_count += 1

            eligible = int(view_count >= MIN_VIEWS)

            if eligible:
                eligible_count += 1

            conn.execute("""
                INSERT INTO clips (
                    clip_id,
                    broadcaster_id,
                    broadcaster_name,
                    creator_name,
                    title,
                    url,
                    thumbnail_url,
                    view_count,
                    created_at,
                    discovered_at,
                    eligible_for_processing,
                    source_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(clip_id) DO UPDATE SET
                    broadcaster_name=excluded.broadcaster_name,
                    title=excluded.title,
                    view_count=excluded.view_count,
                    eligible_for_processing=excluded.eligible_for_processing,
                    source_type=excluded.source_type
            """, (
                clip["id"],
                broadcaster_id,
                clip.get("broadcaster_name"),
                clip.get("creator_name"),
                clip.get("title"),
                clip.get("url"),
                clip.get("thumbnail_url"),
                view_count,
                created_at,
                now,
                eligible,
                source_type
            ))

            saved += 1

        conn.commit()

    return {
        "saved": saved,
        "eligible": eligible_count,
        "known": known_count,
        "discovery": discovery_count
    }


# ---------------- MAIN RADAR ----------------

def poll_once():
    if not _poll_lock.acquire(blocking=False):
        raise RuntimeError("A poll is already running")

    try:
        # Trouver la catégorie GTA VI.
        games = twitch_get(
            "games",
            {"name": "Grand Theft Auto VI"}
        ).get("data", [])

        if not games:
            raise RuntimeError(
                "Twitch GTA VI category was not found"
            )

        game_id = games[0]["id"]

        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=LOOKBACK_HOURS)

        started_at = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        ended_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1. Découverte générale.
        all_clips = collect_category_clips(
            game_id, started_at, ended_at
        )

        # 2. Résolution des streamers connus.
        users, missing = resolve_known_streamers()

        known_ids = set(users.values())

        # 3. Recherche supplémentaire par streamer connu.
        for login, broadcaster_id in users.items():
            streamer_clips = collect_known_streamer_clips(
                broadcaster_id,
                started_at,
                ended_at
            )

            # Ne conserver que les clips GTA VI.
            for clip in streamer_clips:
                if str(clip.get("game_id", "")) == game_id:
                    all_clips.append(clip)

        # 4. Éliminer les doublons.
        unique_clips = {
            clip["id"]: clip
            for clip in all_clips
            if clip.get("id")
        }

        clips = list(unique_clips.values())

        # 5. Enregistrer les résultats.
        stats = save_clips(clips, known_ids)

        poll_time = datetime.now(timezone.utc).isoformat()

        _state.update({
            "last_poll": poll_time,
            "last_error": None,
            "found": len(clips),
            "eligible": stats["eligible"],
            "known_streamer_clips": stats["known"],
            "discovered_streamer_clips": stats["discovery"]
        })

        return {
            "game_id": game_id,
            "clips_seen": len(clips),
            "clips_saved": stats["saved"],
            "eligible_for_processing": stats["eligible"],
            "known_streamer_clips": stats["known"],
            "discovered_streamer_clips": stats["discovery"],
            "known_streamers_found": list(users.keys()),
            "known_streamers_not_found": missing,
            "lookback_hours": LOOKBACK_HOURS,
            "minimum_views": MIN_VIEWS
        }

    finally:
        _poll_lock.release()


# ---------------- AUTOMATIC POLLING ----------------

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

    threading.Thread(
        target=poll_loop,
        daemon=True
    ).start()


# ---------------- API ENDPOINTS ----------------

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "twitch-gta6-radar",
        **_state
    }


@app.get("/clips")
def list_clips(
    limit: int = 50,
    eligible_only: bool = False,
    source_type: str = "all"
):
    limit = min(200, max(1, limit))

    if source_type not in ("all", "known", "discovery"):
        raise HTTPException(
            status_code=400,
            detail="source_type must be all, known or discovery"
        )

    query = "SELECT * FROM clips WHERE 1=1"
    params = []

    if eligible_only:
        query += " AND eligible_for_processing=1"

    if source_type != "all":
        query += " AND source_type=?"
        params.append(source_type)

    query += """
        ORDER BY
            created_at DESC,
            view_count DESC
        LIMIT ?
    """

    params.append(limit)

    with db() as conn:
        rows = conn.execute(query, params).fetchall()

    return {
        "count": len(rows),
        "clips": [dict(row) for row in rows]
    }


@app.post("/poll")
def manual_poll():
    try:
        return poll_once()

    except Exception as exc:
        _state["last_error"] = str(exc)[:500]

        raise HTTPException(
            status_code=502,
            detail=str(exc)
        )
