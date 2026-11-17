
import os
import time
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query


# ============================================================
# CONFIGURATION
# ============================================================

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()

LOOKBACK_HOURS = max(1, int(os.getenv("CLIP_LOOKBACK_HOURS", "24")))
MIN_VIEWS = max(0, int(os.getenv("MIN_CLIP_VIEWS", "100")))

POLL_INTERVAL = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "900")))
MAX_CLIPS = min(100, max(1, int(os.getenv("MAX_CLIPS", "100"))))
KNOWN_CLIPS_LIMIT = min(100, max(1, int(os.getenv("KNOWN_CLIPS_LIMIT", "20"))))

DB_PATH = os.getenv("DATABASE_PATH", "/data/clips.sqlite3")

KNOWN_STREAMERS = {
    login.strip().lower().replace(" ", "")
    for login in os.getenv(
        "KNOWN_STREAMERS",
        "ishowspeed,kaicenat,xqc,jynxzi,rubius,jltomy"
    ).split(",")
    if login.strip()
}

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_URL = "https://api.twitch.tv/helix"

app = FastAPI(
    title="Twitch GTA VI Clipping Radar",
    description="Radar automatique de clips Twitch autour de GTA VI.",
    version="1.1.0"
)


# ============================================================
# ÉTAT DU SERVICE
# ============================================================

token_cache = {
    "access_token": None,
    "expires_at": 0
}

state = {
    "last_poll": None,
    "last_error": None,
    "found": 0
}

poll_lock = threading.Lock()


# ============================================================
# BASE DE DONNÉES SQLITE
# ============================================================

def get_connection():
    directory = os.path.dirname(DB_PATH)

    if directory:
        os.makedirs(directory, exist_ok=True)

    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row

    return connection


def init_database():
    connection = get_connection()

    try:
        # Création de la table pour les nouvelles installations.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY,
                url TEXT,
                embed_url TEXT,
                broadcaster_id TEXT,
                broadcaster_name TEXT,
                creator_name TEXT,
                video_id TEXT,
                game_id TEXT,
                language TEXT,
                title TEXT,
                view_count INTEGER DEFAULT 0,
                created_at TEXT,
                thumbnail_url TEXT,
                source_type TEXT DEFAULT 'discovery',
                eligible_for_processing INTEGER DEFAULT 0,
                fetched_at TEXT
            )
        """)

        # Lecture de la structure existante.
        existing_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(clips)"
            ).fetchall()
        }

        # CORRECTION DE L'ERREUR :
        # Si l'ancienne base ne possède pas id, on l'ajoute.
        if "id" not in existing_columns:
            connection.execute(
                "ALTER TABLE clips ADD COLUMN id TEXT"
            )
            existing_columns.add("id")

        # Migration des anciennes données utilisant clip_id.
        if "clip_id" in existing_columns:
            connection.execute("""
                UPDATE clips
                SET id = clip_id
                WHERE (id IS NULL OR id = '')
                  AND clip_id IS NOT NULL
            """)

        # Ajout des autres colonnes manquantes.
        migrations = {
            "url": "TEXT",
            "embed_url": "TEXT",
            "broadcaster_id": "TEXT",
            "broadcaster_name": "TEXT",
            "creator_name": "TEXT",
            "video_id": "TEXT",
            "game_id": "TEXT",
            "language": "TEXT",
            "title": "TEXT",
            "view_count": "INTEGER DEFAULT 0",
            "created_at": "TEXT",
            "thumbnail_url": "TEXT",
            "source_type": "TEXT DEFAULT 'discovery'",
            "eligible_for_processing": "INTEGER DEFAULT 0",
            "fetched_at": "TEXT"
        }

        for column, definition in migrations.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE clips ADD COLUMN {column} {definition}"
                )

        # Évite les doublons d'identifiants dans les anciennes bases.
        connection.execute("""
            UPDATE clips
            SET id = NULL
            WHERE id IS NOT NULL
              AND rowid NOT IN (
                  SELECT MIN(rowid)
                  FROM clips
                  WHERE id IS NOT NULL
                  GROUP BY id
              )
        """)

        # Rend id unique pour permettre ON CONFLICT(id).
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_id
            ON clips(id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_clips_created_at
            ON clips(created_at)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_clips_source_type
            ON clips(source_type)
        """)

        connection.commit()

        print("Base SQLite initialisée avec succès.")
        print("Migration des anciennes données terminée.")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# TWITCH : AUTHENTIFICATION
# ============================================================

def get_twitch_token():
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise RuntimeError(
            "Les variables TWITCH_CLIENT_ID et TWITCH_CLIENT_SECRET "
            "doivent être configurées dans Render."
        )

    now = time.time()

    if (
        token_cache["access_token"]
        and token_cache["expires_at"] > now + 60
    ):
        return token_cache["access_token"]

    response = requests.post(
        TWITCH_TOKEN_URL,
        params={
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials"
        },
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    token_cache["access_token"] = data["access_token"]
    token_cache["expires_at"] = now + int(data["expires_in"])

    return token_cache["access_token"]


def twitch_get(endpoint: str, params: dict | None = None):
    token = get_twitch_token()

    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }

    response = requests.get(
        f"{TWITCH_API_URL}/{endpoint}",
        headers=headers,
        params=params or {},
        timeout=30
    )

    # Renouvellement automatique si le token a expiré.
    if response.status_code == 401:
        token_cache["access_token"] = None
        token_cache["expires_at"] = 0

        token = get_twitch_token()
        headers["Authorization"] = f"Bearer {token}"

        response = requests.get(
            f"{TWITCH_API_URL}/{endpoint}",
            headers=headers,
            params=params or {},
            timeout=30
        )

    response.raise_for_status()

    return response.json()


# ============================================================
# TWITCH : IDENTIFIER GTA VI
# ============================================================

def get_gta_game_id():
    data = twitch_get(
        "games",
        {"name": "Grand Theft Auto VI"}
    )

    games = data.get("data", [])

    if not games:
        raise RuntimeError(
            "Impossible de trouver la catégorie Grand Theft Auto VI sur Twitch."
        )

    return games[0]["id"]


# ============================================================
# TWITCH : STREAMERS CONNUS
# ============================================================

def resolve_known_streamers():
    resolved = {}

    for login in sorted(KNOWN_STREAMERS):
        try:
            data = twitch_get(
                "users",
                {"login": login}
            )

            users = data.get("data", [])

            if users:
                user = users[0]
                resolved[user["id"]] = user["login"].lower()

        except requests.RequestException as error:
            print(
                f"Erreur pendant la recherche du streamer {login}: {error}"
            )

    return resolved


# ============================================================
# TWITCH : RÉCUPÉRER LES CLIPS
# ============================================================

def collect_category_clips(
    game_id: str,
    started_at: str,
    ended_at: str
):
    data = twitch_get(
        "clips",
        {
            "game_id": game_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "first": MAX_CLIPS
        }
    )

    return data.get("data", [])


def collect_known_streamer_clips(
    broadcaster_id: str,
    started_at: str,
    ended_at: str
):
    data = twitch_get(
        "clips",
        {
            "broadcaster_id": broadcaster_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "first": KNOWN_CLIPS_LIMIT
        }
    )

    return data.get("data", [])


# ============================================================
# DATES
# ============================================================

def parse_twitch_date(value: str):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


# ============================================================
# ENREGISTRER LES CLIPS
# ============================================================

def save_clips(
    clips: list[dict[str, Any]],
    known_ids: set[str]
):
    connection = get_connection()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)

    saved_count = 0
    eligible_count = 0

    try:
        for clip in clips:
            clip_id = clip.get("id")

            if not clip_id:
                continue

            created_at = clip.get("created_at")
            clip_date = parse_twitch_date(created_at)

            if clip_date is None:
                continue

            if clip_date < cutoff or clip_date > now:
                continue

            view_count = int(
                clip.get("view_count", 0) or 0
            )

            broadcaster_id = str(
                clip.get("broadcaster_id", "")
            )

            source_type = (
                "known"
                if broadcaster_id in known_ids
                else "discovery"
            )

            eligible = 1 if view_count >= MIN_VIEWS else 0

            if eligible:
                eligible_count += 1

            fetched_at = now.isoformat()

            connection.execute("""
                INSERT INTO clips (
                    id,
                    url,
                    embed_url,
                    broadcaster_id,
                    broadcaster_name,
                    creator_name,
                    video_id,
                    game_id,
                    language,
                    title,
                    view_count,
                    created_at,
                    thumbnail_url,
                    source_type,
                    eligible_for_processing,
                    fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    embed_url = excluded.embed_url,
                    broadcaster_id = excluded.broadcaster_id,
                    broadcaster_name = excluded.broadcaster_name,
                    creator_name = excluded.creator_name,
                    video_id = excluded.video_id,
                    game_id = excluded.game_id,
                    language = excluded.language,
                    title = excluded.title,
                    view_count = excluded.view_count,
                    created_at = excluded.created_at,
                    thumbnail_url = excluded.thumbnail_url,
                    source_type = excluded.source_type,
                    eligible_for_processing = excluded.eligible_for_processing,
                    fetched_at = excluded.fetched_at
            """, (
                clip_id,
                clip.get("url"),
                clip.get("embed_url"),
                broadcaster_id,
                clip.get("broadcaster_name"),
                clip.get("creator_name"),
                clip.get("video_id"),
                clip.get("game_id"),
                clip.get("language"),
                clip.get("title"),
                view_count,
                created_at,
                clip.get("thumbnail_url"),
                source_type,
                eligible,
                fetched_at
            ))

            saved_count += 1

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    return saved_count, eligible_count


# ============================================================
# LANCER UNE RECHERCHE TWITCH
# ============================================================

def poll_once():
    if not poll_lock.acquire(blocking=False):
        return {
            "status": "already_running",
            "message": "Une recherche est déjà en cours."
        }

    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=LOOKBACK_HOURS)

        started_at = cutoff.isoformat().replace("+00:00", "Z")
        ended_at = now.isoformat().replace("+00:00", "Z")

        game_id = get_gta_game_id()

        # Recherche générale GTA VI.
        category_clips = collect_category_clips(
            game_id,
            started_at,
            ended_at
        )

        # Recherche des streamers suivis.
        known_streamers = resolve_known_streamers()
        known_ids = set(known_streamers.keys())

        known_clips = []

        for broadcaster_id in known_ids:
            try:
                streamer_clips = collect_known_streamer_clips(
                    broadcaster_id,
                    started_at,
                    ended_at
                )

                for clip in streamer_clips:
                    if clip.get("game_id") == game_id:
                        known_clips.append(clip)

            except requests.RequestException as error:
                print(
                    f"Erreur de récupération pour "
                    f"{known_streamers.get(broadcaster_id, broadcaster_id)}: "
                    f"{error}"
                )

        # Fusion et suppression des doublons.
        all_clips = {}

        for clip in category_clips + known_clips:
            clip_id = clip.get("id")

            if clip_id:
                all_clips[clip_id] = clip

        unique_clips = list(all_clips.values())

        saved_count, eligible_count = save_clips(
            unique_clips,
            known_ids
        )

        known_streamer_names = sorted(
            set(known_streamers.values())
        )

        result = {
            "status": "success",
            "game_id": game_id,
            "clips_seen": len(unique_clips),
            "clips_saved": saved_count,
            "eligible_for_processing": eligible_count,
            "known_streamer_clips": len(known_clips),
            "discovered_streamer_clips": len(category_clips),
            "known_streamers_found": known_streamer_names,
            "known_streamers_not_found": sorted(
                KNOWN_STREAMERS - set(known_streamer_names)
            ),
            "lookback_hours": LOOKBACK_HOURS,
            "minimum_views": MIN_VIEWS
        }

        state["last_poll"] = datetime.now(
            timezone.utc
        ).isoformat()

        state["last_error"] = None
        state["found"] = len(unique_clips)

        print("Résultat de la recherche :", result)

        return result

    except Exception as error:
        state["last_error"] = str(error)

        print("Erreur pendant la recherche :", error)

        raise

    finally:
        poll_lock.release()


# ============================================================
# RECHERCHE AUTOMATIQUE EN ARRIÈRE-PLAN
# ============================================================

def poll_loop():
    time.sleep(10)

    while True:
        try:
            poll_once()

        except Exception as error:
            print(
                "Erreur dans le système automatique :",
                error
            )

        time.sleep(POLL_INTERVAL)


# ============================================================
# ROUTES API
# ============================================================

@app.on_event("startup")
def startup_event():
    init_database()

    thread = threading.Thread(
        target=poll_loop,
        daemon=True
    )

    thread.start()

    print("Twitch GTA VI Radar démarré.")
    print(f"Fenêtre de recherche : {LOOKBACK_HOURS} heures.")
    print(f"Minimum de vues : {MIN_VIEWS}.")
    print(f"Intervalle automatique : {POLL_INTERVAL} secondes.")
    print(f"Streamers suivis : {sorted(KNOWN_STREAMERS)}")


@app.get("/")
def home():
    return {
        "service": "Twitch GTA VI Clipping Radar",
        "status": "online",
        "docs": "/docs",
        "health": "/health",
        "clips": "/clips",
        "poll": "POST /poll"
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "twitch-gta6-radar",
        "last_poll": state["last_poll"],
        "last_error": state["last_error"],
        "found": state["found"],
        "lookback_hours": LOOKBACK_HOURS,
        "minimum_views": MIN_VIEWS,
        "poll_interval_seconds": POLL_INTERVAL,
        "known_streamers": sorted(KNOWN_STREAMERS)
    }


@app.post("/poll")
def manual_poll():
    try:
        return poll_once()

    except Exception as error:
        print("Erreur POST /poll :", error)

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/clips")
def get_clips(
    limit: int = Query(default=50, ge=1, le=100),
    eligible_only: bool = Query(default=False),
    source_type: str = Query(
        default="all",
        pattern="^(all|known|discovery)$"
    )
):
    connection = get_connection()

    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=LOOKBACK_HOURS)

        query = """
            SELECT *
            FROM clips
            WHERE julianday(created_at) >= julianday(?)
              AND julianday(created_at) <= julianday(?)
        """

        params = [
            cutoff.isoformat(),
            now.isoformat()
        ]

        if eligible_only:
            query += " AND view_count >= ?"
            params.append(MIN_VIEWS)

        if source_type != "all":
            query += " AND source_type = ?"
            params.append(source_type)

        query += """
            ORDER BY
                CASE WHEN source_type = 'known' THEN 0 ELSE 1 END,
                view_count DESC,
                created_at DESC
            LIMIT ?
        """

        params.append(limit)

        rows = connection.execute(
            query,
            params
        ).fetchall()

        clips = [dict(row) for row in rows]

        return {
            "count": len(clips),
            "lookback_hours": LOOKBACK_HOURS,
            "minimum_views": MIN_VIEWS,
            "eligible_only": eligible_only,
            "source_type": source_type,
            "clips": clips
        }

    finally:
        connection.close()
