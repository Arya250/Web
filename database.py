import json
import logging
import threading

from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from config import DATABASE_URL


logger = logging.getLogger(__name__)

db_pool = None
pool_lock = threading.Lock()


# =====================================================
# DATABASE CONNECTION
# =====================================================

def initialize_database_pool():

    global db_pool

    with pool_lock:

        if db_pool is not None:
            return

        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing.")

        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL,
            cursor_factory=RealDictCursor
        )

    logger.info("PostgreSQL connection pool initialized.")

    # Automatically migrate database after connection.
    initialize_database()


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

    with pool_lock:

        if db_pool is not None:

            db_pool.closeall()
            db_pool = None

    logger.info("Database connection pool closed.")


# =====================================================
# DATABASE MIGRATION
# =====================================================

def initialize_database():

    with get_db() as cursor:

        # ---------------------------------------------
        # MIGRATION TRACKING
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(100) PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # ---------------------------------------------
        # USERS TABLE
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_active TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # Old database compatibility.
        cursor.execute("""
            DO $$
            BEGIN

                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'users'
                    AND column_name = 'user_id'
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'users'
                    AND column_name = 'telegram_id'
                )
                THEN
                    ALTER TABLE users
                    RENAME COLUMN user_id TO telegram_id;
                END IF;

            END $$;
        """)

        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS username VARCHAR(255);

            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS first_name VARCHAR(255);

            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS last_name VARCHAR(255);

            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS is_banned BOOLEAN
            NOT NULL DEFAULT FALSE;

            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ
            NOT NULL DEFAULT NOW();

            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ
            NOT NULL DEFAULT NOW();
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_username
            ON users(username);

            CREATE INDEX IF NOT EXISTS idx_users_joined_at
            ON users(joined_at);
        """)

        # ---------------------------------------------
        # FILES TABLE
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (

                id BIGSERIAL PRIMARY KEY,

                token VARCHAR(100) NOT NULL UNIQUE,

                uploaded_by BIGINT NOT NULL
                    REFERENCES users(telegram_id)
                    ON DELETE RESTRICT,

                file_id TEXT NOT NULL,
                file_unique_id TEXT,

                file_name TEXT NOT NULL DEFAULT 'Unknown file',
                file_type VARCHAR(50) NOT NULL DEFAULT 'document',

                file_size BIGINT NOT NULL DEFAULT 0,
                mime_type VARCHAR(255),

                caption TEXT,

                is_public BOOLEAN NOT NULL DEFAULT FALSE,
                sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,

                views BIGINT NOT NULL DEFAULT 0,
                downloads BIGINT NOT NULL DEFAULT 0,

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

            );
        """)

        # Add missing columns to old installations.
        cursor.execute("""
            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_unique_id TEXT;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_name TEXT;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_type VARCHAR(50);

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS file_size BIGINT
            NOT NULL DEFAULT 0;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS mime_type VARCHAR(255);

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS caption TEXT;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS is_public BOOLEAN
            NOT NULL DEFAULT FALSE;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS sharing_enabled BOOLEAN
            NOT NULL DEFAULT TRUE;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS views BIGINT
            NOT NULL DEFAULT 0;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS downloads BIGINT
            NOT NULL DEFAULT 0;

            ALTER TABLE files
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
            NOT NULL DEFAULT NOW();
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_files_uploaded_by
            ON files(uploaded_by);

            CREATE INDEX IF NOT EXISTS idx_files_token
            ON files(token);

            CREATE INDEX IF NOT EXISTS idx_files_created_at
            ON files(created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_files_sharing
            ON files(sharing_enabled, is_public);
        """)

        # ---------------------------------------------
        # FORCE JOIN CHANNELS
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS force_channels (

                id BIGSERIAL PRIMARY KEY,

                chat_id BIGINT UNIQUE,

                channel_username VARCHAR(255),

                channel_title VARCHAR(255),

                invite_link TEXT,

                is_active BOOLEAN NOT NULL DEFAULT TRUE,

                channel TEXT,

                added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

            );
        """)

        cursor.execute("""
            ALTER TABLE force_channels
            ADD COLUMN IF NOT EXISTS chat_id BIGINT;

            ALTER TABLE force_channels
            ADD COLUMN IF NOT EXISTS channel_username VARCHAR(255);

            ALTER TABLE force_channels
            ADD COLUMN IF NOT EXISTS channel_title VARCHAR(255);

            ALTER TABLE force_channels
            ADD COLUMN IF NOT EXISTS invite_link TEXT;

            ALTER TABLE force_channels
            ADD COLUMN IF NOT EXISTS is_active BOOLEAN
            NOT NULL DEFAULT TRUE;

            ALTER TABLE force_channels
            ADD COLUMN IF NOT EXISTS channel TEXT;

            ALTER TABLE force_channels
            ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ
            NOT NULL DEFAULT NOW();
        """)

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_force_channels_chat_id
            ON force_channels(chat_id)
            WHERE chat_id IS NOT NULL;
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_force_channels_active
            ON force_channels(is_active);
        """)

        # ---------------------------------------------
        # SETTINGS TABLE
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                setting_key VARCHAR(150) PRIMARY KEY,
                setting_value TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        cursor.execute("""
            DO $$
            BEGIN

                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'settings'
                    AND column_name = 'key'
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'settings'
                    AND column_name = 'setting_key'
                )
                THEN
                    ALTER TABLE settings
                    RENAME COLUMN key TO setting_key;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'settings'
                    AND column_name = 'value'
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'settings'
                    AND column_name = 'setting_value'
                )
                THEN
                    ALTER TABLE settings
                    RENAME COLUMN value TO setting_value;
                END IF;

            END $$;
        """)

        cursor.execute("""
            ALTER TABLE settings
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
            NOT NULL DEFAULT NOW();
        """)

        # ---------------------------------------------
        # ADMIN STATES
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_states (

                admin_id BIGINT PRIMARY KEY,

                state VARCHAR(100) NOT NULL,

                data JSONB NOT NULL DEFAULT '{}'::JSONB,

                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

            );
        """)

        # ---------------------------------------------
        # PROCESSED UPDATES
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_updates (

                update_id BIGINT PRIMARY KEY,

                processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

            );
        """)

        # ---------------------------------------------
        # WEBHOOK QUEUE
        # ---------------------------------------------

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS webhook_updates (

                update_id BIGINT PRIMARY KEY,

                payload JSONB NOT NULL,

                status VARCHAR(20) NOT NULL DEFAULT 'pending',

                attempts INTEGER NOT NULL DEFAULT 0,

                last_error TEXT,

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()

            );
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_webhook_updates_status
            ON webhook_updates(status, created_at);
        """)

        # ---------------------------------------------
        # MIGRATION VERSION
        # ---------------------------------------------

        cursor.execute("""
            INSERT INTO schema_migrations (version)
            VALUES ('001_initial_schema')
            ON CONFLICT (version) DO NOTHING;
        """)

        cursor.execute("""
            INSERT INTO schema_migrations (version)
            VALUES ('002_add_file_sharing_columns')
            ON CONFLICT (version) DO NOTHING;
        """)

    logger.info("Database schema and migrations initialized.")

    initialize_default_settings()


# =====================================================
# DEFAULT SETTINGS
# =====================================================

DEFAULT_SETTINGS = {

    "start_message":
        "Welcome to DESI CLOUD! Your personal Telegram cloud storage.",

    "help_message":
        "Send a file to upload it to DESI CLOUD.",

    "about_message":
        "DESI CLOUD is your personal Telegram cloud storage.",

    "star_message":
        "If you enjoy using DESI CLOUD, please support the project.",

    "force_join_enabled": "false",

    "website_enabled": "true"
}


def initialize_default_settings():

    with get_db() as cursor:

        for key, value in DEFAULT_SETTINGS.items():

            cursor.execute("""
                INSERT INTO settings (
                    setting_key,
                    setting_value
                )
                VALUES (%s, %s)

                ON CONFLICT (setting_key) DO NOTHING;
            """, (key, value))


# =====================================================
# USER OPERATIONS
# =====================================================

def register_user(user_id, username=None, first_name=None):

    with get_db() as cursor:

        cursor.execute("""
            INSERT INTO users (
                telegram_id,
                username,
                first_name
            )

            VALUES (%s, %s, %s)

            ON CONFLICT (telegram_id)

            DO UPDATE SET

                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_active = NOW()

            RETURNING *;
        """, (user_id, username, first_name))

        return cursor.fetchone()


def get_user(user_id):

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM users
            WHERE telegram_id = %s;
        """, (user_id,))

        return cursor.fetchone()


def get_total_users():

    with get_db() as cursor:

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM users;
        """)

        return cursor.fetchone()["total"]


def get_users_paginated(page=0, limit=10):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM users
            ORDER BY joined_at DESC
            LIMIT %s OFFSET %s;
        """, (limit, offset))

        return cursor.fetchall()


# =====================================================
# FILE OPERATIONS
# =====================================================

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

        cursor.execute("""
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

            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)

            RETURNING *;
        """, (
            token,
            file_id,
            file_type,
            file_name or "Unknown file",
            file_size or 0,
            mime_type,
            caption,
            uploaded_by
        ))

        return cursor.fetchone()


def get_file_by_token(token, user_id=None):

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM files
            WHERE token = %s
            AND sharing_enabled = TRUE;
        """, (token,))

        file = cursor.fetchone()

        if not file:
            return None

        # Private files must only be accessible to their owner.
        if not file["is_public"]:

            if user_id is None:
                return None

            if file["uploaded_by"] != user_id:
                return None

        return file


def get_file_by_id(file_id):

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM files
            WHERE id = %s;
        """, (file_id,))

        return cursor.fetchone()


def get_user_file(file_id, user_id):

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM files
            WHERE id = %s
            AND uploaded_by = %s;
        """, (file_id, user_id))

        return cursor.fetchone()


def get_user_files(user_id, page=0, limit=8):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM files
            WHERE uploaded_by = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """, (user_id, limit, offset))

        return cursor.fetchall()


def count_user_files(user_id):

    with get_db() as cursor:

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM files
            WHERE uploaded_by = %s;
        """, (user_id,))

        return cursor.fetchone()["total"]


def increment_file_views(file_id):

    with get_db() as cursor:

        cursor.execute("""
            UPDATE files
            SET views = views + 1,
                updated_at = NOW()
            WHERE id = %s
            RETURNING views;
        """, (file_id,))

        result = cursor.fetchone()

        return result["views"] if result else 0


def increment_file_downloads(file_id):

    with get_db() as cursor:

        cursor.execute("""
            UPDATE files
            SET downloads = downloads + 1,
                updated_at = NOW()
            WHERE id = %s
            RETURNING downloads;
        """, (file_id,))

        result = cursor.fetchone()

        return result["downloads"] if result else 0


def rename_file(file_id, user_id, new_name):

    with get_db() as cursor:

        cursor.execute("""
            UPDATE files
            SET file_name = %s,
                updated_at = NOW()
            WHERE id = %s
            AND uploaded_by = %s
            RETURNING *;
        """, (new_name, file_id, user_id))

        return cursor.fetchone()


def delete_file(file_id, user_id):

    with get_db() as cursor:

        cursor.execute("""
            DELETE FROM files
            WHERE id = %s
            AND uploaded_by = %s
            RETURNING id;
        """, (file_id, user_id))

        return cursor.fetchone()


def admin_delete_file(file_id):

    with get_db() as cursor:

        cursor.execute("""
            DELETE FROM files
            WHERE id = %s
            RETURNING id;
        """, (file_id,))

        return cursor.fetchone()


def set_file_sharing(file_id, user_id, enabled):

    with get_db() as cursor:

        cursor.execute("""
            UPDATE files
            SET sharing_enabled = %s,
                updated_at = NOW()
            WHERE id = %s
            AND uploaded_by = %s
            RETURNING *;
        """, (enabled, file_id, user_id))

        return cursor.fetchone()


def set_file_public(file_id, user_id, is_public):

    with get_db() as cursor:

        cursor.execute("""
            UPDATE files
            SET is_public = %s,
                updated_at = NOW()
            WHERE id = %s
            AND uploaded_by = %s
            RETURNING *;
        """, (is_public, file_id, user_id))

        return cursor.fetchone()


def regenerate_file_token(file_id, user_id, new_token):

    with get_db() as cursor:

        cursor.execute("""
            UPDATE files
            SET token = %s,
                sharing_enabled = TRUE,
                updated_at = NOW()
            WHERE id = %s
            AND uploaded_by = %s
            RETURNING *;
        """, (new_token, file_id, user_id))

        return cursor.fetchone()


# =====================================================
# ADMIN FILE OPERATIONS
# =====================================================

def get_all_files(page=0, limit=10):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM files
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """, (limit, offset))

        return cursor.fetchall()


def search_files(query, page=0, limit=10):

    offset = page * limit

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM files
            WHERE file_name ILIKE %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """, (f"%{query}%", limit, offset))

        return cursor.fetchall()


# =====================================================
# FORCE JOIN OPERATIONS
# =====================================================

def add_force_channel(channel):

    channel = str(channel).strip()

    if not channel:
        return None

    with get_db() as cursor:

        # Supports @username and numeric Telegram chat IDs.
        try:
            chat_id = int(channel)
        except (TypeError, ValueError):
            chat_id = None

        cursor.execute("""
            INSERT INTO force_channels (
                chat_id,
                channel_username,
                channel_title,
                channel,
                is_active
            )

            VALUES (%s, %s, %s, %s, TRUE)

            ON CONFLICT DO NOTHING

            RETURNING *;
        """, (
            chat_id,
            channel.lstrip("@") if chat_id is None else None,
            channel,
            channel
        ))

        return cursor.fetchone()


def remove_force_channel(channel_id):

    with get_db() as cursor:

        cursor.execute("""
            DELETE FROM force_channels
            WHERE id = %s
            RETURNING *;
        """, (channel_id,))

        return cursor.fetchone()


def get_force_channels():

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM force_channels
            WHERE is_active = TRUE
            ORDER BY id;
        """)

        return cursor.fetchall()


# =====================================================
# SETTINGS OPERATIONS
# =====================================================

def get_setting(key, default=None):

    with get_db() as cursor:

        cursor.execute("""
            SELECT setting_value
            FROM settings
            WHERE setting_key = %s;
        """, (key,))

        result = cursor.fetchone()

        return result["setting_value"] if result else default


def set_setting(key, value):

    with get_db() as cursor:

        cursor.execute("""
            INSERT INTO settings (
                setting_key,
                setting_value
            )

            VALUES (%s, %s)

            ON CONFLICT (setting_key)

            DO UPDATE SET

                setting_value = EXCLUDED.setting_value,
                updated_at = NOW()

            RETURNING *;
        """, (key, value))

        return cursor.fetchone()


# =====================================================
# ADMIN STATE OPERATIONS
# =====================================================

def get_admin_state(admin_id):

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM admin_states
            WHERE admin_id = %s;
        """, (admin_id,))

        return cursor.fetchone()


def set_admin_state(admin_id, state, data=None):

    with get_db() as cursor:

        cursor.execute("""
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
                updated_at = NOW()

            RETURNING *;
        """, (
            admin_id,
            state,
            json.dumps(data or {})
        ))

        return cursor.fetchone()


def clear_admin_state(admin_id):

    with get_db() as cursor:

        cursor.execute("""
            DELETE FROM admin_states
            WHERE admin_id = %s;
        """, (admin_id,))


# =====================================================
# STATISTICS
# =====================================================

def get_statistics():

    with get_db() as cursor:

        cursor.execute("""
            SELECT

                (SELECT COUNT(*)
                 FROM users) AS total_users,

                (SELECT COUNT(*)
                 FROM files) AS total_files,

                (SELECT COALESCE(SUM(views), 0)
                 FROM files) AS total_views,

                (SELECT COALESCE(SUM(downloads), 0)
                 FROM files) AS total_downloads,

                (SELECT COALESCE(SUM(file_size), 0)
                 FROM files) AS total_storage,

                (SELECT COUNT(*)
                 FROM files
                 WHERE sharing_enabled = TRUE) AS shared_files,

                (SELECT COUNT(*)
                 FROM files
                 WHERE sharing_enabled = FALSE) AS disabled_files,

                (SELECT COUNT(*)
                 FROM force_channels
                 WHERE is_active = TRUE) AS total_channels;

        """)

        return cursor.fetchone()


def get_recent_uploads(limit=10):

    with get_db() as cursor:

        cursor.execute("""
            SELECT *
            FROM files
            ORDER BY created_at DESC
            LIMIT %s;
        """, (limit,))

        return cursor.fetchall()
