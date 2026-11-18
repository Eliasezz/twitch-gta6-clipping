
import os
import time
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query, Header


# ============================================================
# 1. CONFIGURATION
# ============================================================

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
# If omitted, manually-triggered polls are disabled (scheduled polls continue).
POLL_ADMIN_TOKEN = os.getenv("POLL_ADMIN_TOKEN", "").strip()

LOOKBACK_HOURS = max(1, int(os.getenv("CLIP_LOOKBACK_HOURS", "24")))
MIN_VIEWS = max(0, int(os.getenv("MIN_CLIP_VIEWS", "100")))

POLL_INTERVAL = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "900")))
# Twitch accepte au maximum 100 éléments par page. La limite totale
# peut être supérieure : la collecte utilise alors la pagination.
MAX_CLIPS = min(1000, max(1, int(os.getenv(
    "MAX_CLIPS_PER_POLL",
    os.getenv("MAX_CLIPS", "100")
))))
KNOWN_CLIPS_LIMIT = min(1000, max(1, int(os.getenv("KNOWN_CLIPS_LIMIT", "20"))))

ALLOWED_BROADCASTER_IDS = {
    value.strip()
    for value in os.getenv("ALLOWED_BROADCASTER_IDS", "").split(",")
    if value.strip()
}

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
    title="Twitch GTA VI Clipping + Video Workflow",
    description="Radar Twitch et montage MP4 de fichiers autorisés.",
    docs_url="/api/docs",
    version="1.4.0"
)

# The video workflow is isolated from the scheduled Twitch metadata poller.
from .video_routes import router as video_router
from .autopilot import router as autopilot_router
from .large_upload import router as large_upload_router
app.include_router(video_router)
app.include_router(autopilot_router)
app.include_router(large_upload_router)


# ============================================================
# 2. ÉTAT DU SERVICE
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
# 3. BASE DE DONNÉES
# ============================================================

def get_connection():
    directory = os.path.dirname(DB_PATH)

    if directory:
        os.makedirs(directory, exist_ok=True)

    connection = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")

    return connection


def init_database():
    connection = get_connection()

    try:
        # Nouvelle structure standard.
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
                fetched_at TEXT,
                discovered_at TEXT
            )
        """)

        # Lire la structure réelle de la base existante.
        existing_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(clips)"
            ).fetchall()
        }

        # Colonnes à ajouter aux anciennes bases.
        migrations = {
            "id": "TEXT",
            "clip_id": "TEXT",
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
            "fetched_at": "TEXT",
            "discovered_at": "TEXT"
        }

        for column, definition in migrations.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE clips ADD COLUMN {column} {definition}"
                )

        # Mettre à jour la liste des colonnes après migration.
        existing_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(clips)"
            ).fetchall()
        }

        # Récupérer les anciens identifiants.
        if "clip_id" in existing_columns:
            connection.execute("""
                UPDATE clips
                SET id = clip_id
                WHERE (id IS NULL OR id = '')
                  AND clip_id IS NOT NULL
            """)

            connection.execute("""
                UPDATE clips
                SET clip_id = id
                WHERE (clip_id IS NULL OR clip_id = '')
                  AND id IS NOT NULL
            """)

        # Éliminer les éventuels doublons d'id avant indexation.
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

        # Récupérer discovered_at depuis fetched_at ou created_at
        # lorsque la colonne est vide.
        connection.execute("""
            UPDATE clips
            SET discovered_at = COALESCE(
                NULLIF(discovered_at, ''),
                NULLIF(fetched_at, ''),
                created_at
            )
            WHERE discovered_at IS NULL
               OR discovered_at = ''
        """)

        # Index unique nécessaire pour ON CONFLICT(id).
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

        # Les anciennes versions marquaient "eligible" sur le seul nombre
        # de vues. Réinitialiser ces drapeaux historiques : le prochain poll
        # ne les réactivera que si le créateur figure dans l'allowlist.
        connection.execute("""
            UPDATE clips
            SET eligible_for_processing = 0
            WHERE eligible_for_processing IS NULL
               OR eligible_for_processing != 0
        """)

        connection.commit()

        print("SQLite : migration terminée.")
        print("SQLite : colonnes vérifiées :", sorted(existing_columns))

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# 4. TWITCH : AUTHENTIFICATION
# ============================================================

def get_twitch_token():

    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        raise RuntimeError(
            "TWITCH_CLIENT_ID ou TWITCH_CLIENT_SECRET manquant dans Render."
        )

    now = time.time()

    if (
        token_cache["access_token"]
        and token_cache["expires_at"] > now + 60
    ):
        return token_cache["access_token"]

    # OAuth secrets must be sent in the form BODY, never in URL query strings.
    response = requests.post(
        TWITCH_TOKEN_URL,
        data={
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
# 5. IDENTIFIER GTA VI
# ============================================================

def get_gta_game_id():

    data = twitch_get(
        "games",
        {"name": "Grand Theft Auto VI"}
    )

    games = data.get("data", [])

    if not games:
        raise RuntimeError(
            "Catégorie Grand Theft Auto VI introuvable sur Twitch."
        )

    return games[0]["id"]


# ============================================================
# 6. STREAMERS CONNUS
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
            print(f"Erreur streamer {login}: {error}")

    return resolved


# ============================================================
# 7. RÉCUPÉRER LES CLIPS TWITCH
# ============================================================

def collect_clips_paginated(base_params: dict, limit: int):
    """Récupère plusieurs pages Twitch sans dépasser la limite configurée."""
    clips = []
    cursor = None
    page_size = min(100, limit)

    while len(clips) < limit:
        params = dict(base_params)
        params["first"] = min(page_size, limit - len(clips))
        if cursor:
            params["after"] = cursor

        payload = twitch_get("clips", params)
        page = payload.get("data", [])
        clips.extend(page)

        pagination = payload.get("pagination") or {}
        next_cursor = pagination.get("cursor")
        if not page or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return clips[:limit]


def collect_category_clips(game_id, started_at, ended_at):
    return collect_clips_paginated(
        {
            "game_id": game_id,
            "started_at": started_at,
            "ended_at": ended_at
        },
        MAX_CLIPS
    )


def collect_known_streamer_clips(
    broadcaster_id,
    started_at,
    ended_at
):
    return collect_clips_paginated(
        {
            "broadcaster_id": broadcaster_id,
            "started_at": started_at,
            "ended_at": ended_at
        },
        KNOWN_CLIPS_LIMIT
    )


# ============================================================
# 8. DATES
# ============================================================

def parse_twitch_date(value):

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
# 9. ENREGISTREMENT COMPATIBLE AVEC LES ANCIENNES BASES
# ============================================================

def save_clips(clips: list[dict[str, Any]], known_ids: set[str]):

    connection = get_connection()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)

    saved_count = 0
    eligible_count = 0

    try:

        # Lire toutes les colonnes réelles.
        schema = connection.execute(
            "PRAGMA table_info(clips)"
        ).fetchall()

        columns = [row["name"] for row in schema]

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

            view_count = int(clip.get("view_count", 0) or 0)

            broadcaster_id = str(
                clip.get("broadcaster_id", "")
            )

            source_type = (
                "known"
                if broadcaster_id in known_ids
                else "discovery"
            )

            # Une vue élevée ne donne pas le droit de réutiliser une vidéo.
            # L'éligibilité exige aussi une autorisation explicite du créateur.
            eligible = int(
                view_count >= MIN_VIEWS
                and broadcaster_id in ALLOWED_BROADCASTER_IDS
            )

            fetched_at = now.isoformat()

            if eligible:
                eligible_count += 1

            # Toutes les valeurs connues.
            values = {
                "id": clip_id,
                "clip_id": clip_id,
                "discovered_at": fetched_at,
                "url": clip.get("url"),
                "embed_url": clip.get("embed_url"),
                "broadcaster_id": broadcaster_id,
                "broadcaster_name": clip.get("broadcaster_name"),
                "creator_name": clip.get("creator_name"),
                "video_id": clip.get("video_id"),
                "game_id": clip.get("game_id"),
                "language": clip.get("language"),
                "title": clip.get("title"),
                "view_count": view_count,
                "created_at": created_at,
                "thumbnail_url": clip.get("thumbnail_url"),
                "source_type": source_type,
                "eligible_for_processing": eligible,
                "fetched_at": fetched_at
            }

            # Compatibilité avec les colonnes supplémentaires
            # obligatoires éventuellement présentes dans l'ancienne base.
            insert_columns = []
            insert_values = []

            for column_info in schema:

                column = column_info["name"]
                column_type = (column_info["type"] or "").upper()
                not_null = column_info["notnull"]
                default_value = column_info["dflt_value"]
                is_primary_key = column_info["pk"]

                # Laisser SQLite gérer les clés primaires
                # INTEGER AUTOINCREMENT.
                if (
                    is_primary_key
                    and column_type == "INTEGER"
                    and column not in values
                ):
                    continue

                if column in values:
                    value = values[column]

                elif not_null and default_value is None:
                    # Valeur de secours pour une ancienne colonne
                    # obligatoire non connue par cette version.
                    if "INT" in column_type or "REAL" in column_type:
                        value = 0
                    elif "BLOB" in column_type:
                        value = b""
                    else:
                        value = ""

                else:
                    # Ne pas remplacer les valeurs par défaut SQLite.
                    continue

                insert_columns.append(column)
                insert_values.append(value)

            placeholders = ", ".join(
                ["?"] * len(insert_columns)
            )

            column_names = ", ".join(
                f'"{column}"' for column in insert_columns
            )

            # Toutes les colonnes fournies sont actualisées
            # sauf l'identifiant principal.
            update_columns = [
                column for column in insert_columns
                if column != "id"
            ]

            update_clause = ", ".join(
                f'"{column}" = excluded."{column}"'
                for column in update_columns
            )

            if update_clause:
                sql = f"""
                    INSERT INTO clips ({column_names})
                    VALUES ({placeholders})
                    ON CONFLICT(id) DO UPDATE SET
                    {update_clause}
                """
            else:
                sql = f"""
                    INSERT INTO clips ({column_names})
                    VALUES ({placeholders})
                    ON CONFLICT(id) DO NOTHING
                """

            connection.execute(sql, insert_values)

            saved_count += 1

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    return saved_count, eligible_count


# ============================================================
# 10. LANCER UNE RECHERCHE TWITCH
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
                    "Erreur de récupération streamer :",
                    error
                )

        # Fusion et dédoublonnage.
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
            "minimum_views": MIN_VIEWS,
            "authorized_broadcasters_configured": len(ALLOWED_BROADCASTER_IDS)
        }

        state["last_poll"] = datetime.now(
            timezone.utc
        ).isoformat()

        state["last_error"] = None
        state["found"] = len(unique_clips)

        print("Résultat de la recherche :", result)

        return result

    except Exception as error:

        # Public /health must not expose request URLs or account details.
        state["last_error"] = type(error).__name__

        print("Erreur pendant la recherche :", type(error).__name__, error)

        raise

    finally:
        poll_lock.release()


# ============================================================
# 11. RECHERCHE AUTOMATIQUE
# ============================================================

def poll_loop():

    time.sleep(10)

    while True:

        try:
            poll_once()
            # Conditional, rights-gated autopilot; no downloads or posts
            # unless creator OAuth + Metricool API credentials are present.
            from .autopilot import run_automatic_after_poll
            run_automatic_after_poll()

        except Exception as error:
            print("Erreur système automatique :", error)

        time.sleep(POLL_INTERVAL)


# ============================================================
# 12. ROUTES API
# ============================================================

@app.on_event("startup")
def startup_event():

    init_database()
    from .autopilot import init_automation_db
    init_automation_db()

    # Recover previously uploaded, fully received videos rejected by the
    # former 15-minute limit, without requiring another upload.
    from .large_upload import recover_completed_uploads
    recover_completed_uploads()

    # The user may explicitly request a retry of a previously failed
    # CaptionError job without re-uploading its original source file.
    from .video_routes import recover_failed_video_jobs_on_startup
    recover_failed_video_jobs_on_startup()

    thread = threading.Thread(
        target=poll_loop,
        daemon=True
    )

    thread.start()

    # Opt-in, one-time original/synthetic MP4 smoke test on the existing server.


    print("Twitch GTA VI Radar démarré.")
    print("Fenêtre :", LOOKBACK_HOURS, "heures")
    print("Minimum de vues :", MIN_VIEWS)
    print("Intervalle :", POLL_INTERVAL, "secondes")
    print("Streamers :", sorted(KNOWN_STREAMERS))
    print("Créateurs autorisés :", len(ALLOWED_BROADCASTER_IDS))
    print("Limite clips par source :", MAX_CLIPS)


@app.get("/docs", include_in_schema=False)
def studio_entry():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/video/uploader")


@app.get("/")
def home():

    return {
        "service": "Twitch GTA VI Clipping Radar",
        "status": "online",
        "docs": "/api/docs",
        "studio": "/video/uploader",
        "health": "/health",
        "clips": "/clips",
        "poll": "POST /poll",
        "autopilot": "/automation/status"
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
        "known_streamers": sorted(KNOWN_STREAMERS),
        "authorized_broadcasters_configured": len(ALLOWED_BROADCASTER_IDS),
        "reuse_gate_enabled": True
    }


@app.post("/poll")
def manual_poll(x_admin_token: str | None = Header(default=None)):
    # Public POST would otherwise allow anyone to consume Twitch rate limits.
    if not POLL_ADMIN_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Manual polling disabled. Configure POLL_ADMIN_TOKEN in Render."
        )
    if x_admin_token != POLL_ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token.")

    try:
        return poll_once()
    except Exception as error:
        print("Erreur POST /poll :", type(error).__name__, error)
        raise HTTPException(
            status_code=502,
            detail="Twitch polling failed. Check the server logs."
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
            query += " AND eligible_for_processing = 1"

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
