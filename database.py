import json
import logging

from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL


logger = logging.getLogger(__name__)

db_pool = None


def initialize_database_pool():

    global db_pool

    if db_pool is not None:
        return

    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=DATABASE_URL,
        cursor_factory=RealDictCursor
    )

    logger.info("PostgreSQL connection pool initialized.")


@contextmanager
def get_db():

    if db_pool is None:
        raise RuntimeError(
            "Database pool is not initialized."
        )

    connection = db_pool.getconn()

    try:

        with connection.cursor() as cursor:

            yield cursor

        connection.commit()

    except Exception:

        connection.rollback()

        raise

    finally:

        db_pool.putconn(connection)


def close_database_pool():

    global db_pool

    if db_pool:

        db_pool.closeall()

        db_pool = None


def initialize_database():

    schema = """

    CREATE TABLE IF NOT EXISTS users (

        user_id BIGINT PRIMARY KEY,

        username TEXT,

        first_name TEXT,

        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    );


    CREATE TABLE IF NOT EXISTS files (

        id BIGSERIAL PRIMARY KEY,

        token TEXT UNIQUE NOT NULL,

        file_id TEXT NOT NULL,

        file_type TEXT NOT NULL,

        file_name TEXT,

        file_size BIGINT DEFAULT 0,

        mime_type TEXT,

        caption TEXT,

        uploaded_by BIGINT NOT NULL
            REFERENCES users(user_id),

        views BIGINT DEFAULT 0,

        is_public BOOLEAN DEFAULT FALSE,

        sharing_enabled BOOLEAN DEFAULT TRUE,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    );


    CREATE TABLE IF NOT EXISTS force_channels (

        id BIGSERIAL PRIMARY KEY,

        channel TEXT UNIQUE NOT NULL,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    );


    CREATE TABLE IF NOT EXISTS settings (

        key TEXT PRIMARY KEY,

        value TEXT

    );


    CREATE TABLE IF NOT EXISTS admin_states (

        admin_id BIGINT PRIMARY KEY,

        state TEXT NOT NULL,

        data JSONB DEFAULT '{}'::jsonb,

        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    );


    CREATE TABLE IF NOT EXISTS processed_updates (

        update_id BIGINT PRIMARY KEY,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    );


    CREATE INDEX IF NOT EXISTS idx_files_owner

    ON files(uploaded_by);


    CREATE INDEX IF NOT EXISTS idx_files_token

    ON files(token);


    CREATE INDEX IF NOT EXISTS idx_files_created

    ON files(created_at DESC);


    CREATE INDEX IF NOT EXISTS idx_files_sharing

    ON files(sharing_enabled);


    CREATE INDEX IF NOT EXISTS idx_users_joined

    ON users(joined_at DESC);

    """

    with get_db() as cursor:

        cursor.execute(schema)

    logger.info("Database schema initialized.")


# ==========================================
# USER OPERATIONS
# ==========================================

def register_user(user_id, username, first_name):

    with get_db() as cursor:

        cursor.execute(
            """
            INSERT INTO users (
                user_id,
                username,
                first_name
            )

            VALUES (%s, %s, %s)

            ON CONFLICT (user_id)

            DO UPDATE SET

                username = EXCLUDED.username,

                first_name = EXCLUDED.first_name

            RETURNING *
            """,

            (
                user_id,
                username,
                first_name
            )
        )

        return cursor.fetchone()


def get_user(user_id):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM users

            WHERE user_id = %s
            """,

            (user_id,)
        )

        return cursor.fetchone()


def get_total_users():

    with get_db() as cursor:

        cursor.execute(
            "SELECT COUNT(*) AS total FROM users"
        )

        return cursor.fetchone()["total"]


def get_users_paginated(page=0, limit=10):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM users

            ORDER BY joined_at DESC

            LIMIT %s OFFSET %s
            """,

            (limit, offset)
        )

        return cursor.fetchall()


# ==========================================
# FILE OPERATIONS
# ==========================================

def save_file(
    token,
    file_id,
    file_type,
    file_name,
    file_size,
    mime_type,
    caption,
    uploaded_by
):

    with get_db() as cursor:

        cursor.execute(
            """
            INSERT INTO files (

                token,
                file_id,
                file_type,
                file_name,
                file_size,
                mime_type,
                caption,
                uploaded_by

            )

            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)

            RETURNING *
            """,

            (
                token,
                file_id,
                file_type,
                file_name,
                file_size,
                mime_type,
                caption,
                uploaded_by
            )
        )

        return cursor.fetchone()


def get_file_by_token(token):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM files

            WHERE token = %s

            AND sharing_enabled = TRUE

            AND (
                is_public = TRUE
                OR uploaded_by = %s
            )
            """,

            (token, 0)
        )

        return cursor.fetchone()


def get_file_by_id(file_id):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM files

            WHERE id = %s
            """,

            (file_id,)
        )

        return cursor.fetchone()


def get_user_file(file_id, user_id):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM files

            WHERE id = %s

            AND uploaded_by = %s
            """,

            (file_id, user_id)
        )

        return cursor.fetchone()


def get_user_files(user_id, page=0, limit=8):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM files

            WHERE uploaded_by = %s

            ORDER BY created_at DESC

            LIMIT %s OFFSET %s
            """,

            (
                user_id,
                limit,
                offset
            )
        )

        return cursor.fetchall()


def count_user_files(user_id):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT COUNT(*) AS total

            FROM files

            WHERE uploaded_by = %s
            """,

            (user_id,)
        )

        return cursor.fetchone()["total"]


def increment_file_views(file_id):

    with get_db() as cursor:

        cursor.execute(
            """
            UPDATE files

            SET views = views + 1

            WHERE id = %s

            RETURNING views
            """,

            (file_id,)
        )

        result = cursor.fetchone()

        return result["views"] if result else 0


def rename_file(file_id, user_id, new_name):

    with get_db() as cursor:

        cursor.execute(
            """
            UPDATE files

            SET file_name = %s

            WHERE id = %s

            AND uploaded_by = %s

            RETURNING *
            """,

            (
                new_name,
                file_id,
                user_id
            )
        )

        return cursor.fetchone()


def delete_file(file_id, user_id):

    with get_db() as cursor:

        cursor.execute(
            """
            DELETE FROM files

            WHERE id = %s

            AND uploaded_by = %s

            RETURNING id
            """,

            (
                file_id,
                user_id
            )
        )

        return cursor.fetchone()


def admin_delete_file(file_id):

    with get_db() as cursor:

        cursor.execute(
            """
            DELETE FROM files

            WHERE id = %s

            RETURNING id
            """,

            (file_id,)
        )

        return cursor.fetchone()


def set_file_sharing(
    file_id,
    user_id,
    enabled
):

    with get_db() as cursor:

        cursor.execute(
            """
            UPDATE files

            SET sharing_enabled = %s

            WHERE id = %s

            AND uploaded_by = %s

            RETURNING *
            """,

            (
                enabled,
                file_id,
                user_id
            )
        )

        return cursor.fetchone()


def regenerate_file_token(
    file_id,
    user_id,
    new_token
):

    with get_db() as cursor:

        cursor.execute(
            """
            UPDATE files

            SET token = %s,

                sharing_enabled = TRUE

            WHERE id = %s

            AND uploaded_by = %s

            RETURNING *
            """,

            (
                new_token,
                file_id,
                user_id
            )
        )

        return cursor.fetchone()


# ==========================================
# ADMIN FILE OPERATIONS
# ==========================================

def get_all_files(page=0, limit=10):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM files

            ORDER BY created_at DESC

            LIMIT %s OFFSET %s
            """,

            (limit, offset)
        )

        return cursor.fetchall()


def search_files(query, page=0, limit=10):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM files

            WHERE file_name ILIKE %s

            ORDER BY created_at DESC

            LIMIT %s OFFSET %s
            """,

            (
                f"%{query}%",
                limit,
                offset
            )
        )

        return cursor.fetchall()


# ==========================================
# FORCE JOIN OPERATIONS
# ==========================================

def add_force_channel(channel):

    with get_db() as cursor:

        cursor.execute(
            """
            INSERT INTO force_channels (channel)

            VALUES (%s)

            ON CONFLICT (channel) DO NOTHING

            RETURNING *
            """,

            (channel,)
        )

        return cursor.fetchone()


def remove_force_channel(channel_id):

    with get_db() as cursor:

        cursor.execute(
            """
            DELETE FROM force_channels

            WHERE id = %s

            RETURNING *
            """,

            (channel_id,)
        )

        return cursor.fetchone()


def get_force_channels():

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM force_channels

            ORDER BY id
            """
        )

        return cursor.fetchall()


# ==========================================
# SETTINGS
# ==========================================

DEFAULT_SETTINGS = {

    "start_message": (
        "☁️ WELCOME TO DESI CLOUD\n\n"
        "Your personal cloud storage platform.\n\n"
        "📂 Store your files.\n"
        "🔗 Share your files.\n"
        "⚡ Access your files anytime.\n\n"
        "Welcome to a smarter way of sharing files!"
    ),

    "star_message": (
        "⭐ Thank you for supporting DESI CLOUD!"
    ),

    "about_message": (
        "☁️ DESI CLOUD\n\n"
        "Your personal Telegram cloud storage platform."
    ),

    "help_message": (
        "📖 DESI CLOUD HELP\n\n"
        "📤 Send a file to upload it.\n"
        "📁 My Files: Manage uploaded files.\n"
        "🔗 Generate Link: Share your files."
    ),

    "force_join_enabled": "false",

    "website_enabled": "true"
}


def initialize_default_settings():

    with get_db() as cursor:

        for key, value in DEFAULT_SETTINGS.items():

            cursor.execute(
                """
                INSERT INTO settings (key, value)

                VALUES (%s, %s)

                ON CONFLICT (key) DO NOTHING
                """,

                (key, value)
            )


def get_setting(key, default=None):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT value

            FROM settings

            WHERE key = %s
            """,

            (key,)
        )

        result = cursor.fetchone()

        return result["value"] if result else default


def set_setting(key, value):

    with get_db() as cursor:

        cursor.execute(
            """
            INSERT INTO settings (key, value)

            VALUES (%s, %s)

            ON CONFLICT (key)

            DO UPDATE SET value = EXCLUDED.value

            RETURNING *
            """,

            (key, value)
        )

        return cursor.fetchone()


def get_admin_state(admin_id):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM admin_states

            WHERE admin_id = %s
            """,

            (admin_id,)
        )

        return cursor.fetchone()


def set_admin_state(admin_id, state, data=None):

    with get_db() as cursor:

        cursor.execute(
            """
            INSERT INTO admin_states (
                admin_id,
                state,
                data
            )

            VALUES (%s, %s, %s)

            ON CONFLICT (admin_id)

            DO UPDATE SET

                state = EXCLUDED.state,

                data = EXCLUDED.data,

                updated_at = CURRENT_TIMESTAMP

            RETURNING *
            """,

            (
                admin_id,
                state,
                json.dumps(data or {})
            )
        )

        return cursor.fetchone()


def clear_admin_state(admin_id):

    with get_db() as cursor:

        cursor.execute(
            """
            DELETE FROM admin_states

            WHERE admin_id = %s
            """,

            (admin_id,)
        )


# ==========================================
# STATISTICS
# ==========================================

def get_statistics():

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT

                (SELECT COUNT(*)
                 FROM users) AS total_users,

                (SELECT COUNT(*)
                 FROM files) AS total_files,

                (SELECT COALESCE(SUM(views), 0)
                 FROM files) AS total_views,

                (SELECT COALESCE(SUM(file_size), 0)
                 FROM files) AS total_storage,

                (SELECT COUNT(*)
                 FROM files
                 WHERE sharing_enabled = TRUE) AS shared_files,

                (SELECT COUNT(*)
                 FROM files
                 WHERE sharing_enabled = FALSE) AS disabled_files,

                (SELECT COUNT(*)
                 FROM force_channels) AS total_channels

            """
        )

        return cursor.fetchone()


def get_recent_uploads(limit=10):

    with get_db() as cursor:

        cursor.execute(
            """
            SELECT *

            FROM files

            ORDER BY created_at DESC

            LIMIT %s
            """,

            (limit,)
        )

        return cursor.fetchall()
