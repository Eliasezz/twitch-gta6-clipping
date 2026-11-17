
def init_database():
    connection = get_connection()

    # Création de la nouvelle structure si la table n'existe pas.
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

    # Lecture de la structure actuelle.
    existing_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(clips)"
        ).fetchall()
    }

    # CORRECTION PRINCIPALE :
    # Ajout de id dans les anciennes bases qui ne le possèdent pas.
    if "id" not in existing_columns:
        connection.execute(
            "ALTER TABLE clips ADD COLUMN id TEXT"
        )
        existing_columns.add("id")

    # Récupération des anciens identifiants clip_id.
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

    # Protection contre les éventuels identifiants en double.
    # On conserve la première occurrence et toutes les autres données.
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

    # Nécessaire pour ON CONFLICT(id) dans save_clips().
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
    connection.close()

    print("Base SQLite initialisée et migrations terminées.")
