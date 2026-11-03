-- =====================================================
-- DESI CLOUD
-- Initial Database Migration
-- Migration: 001_initial_schema
-- =====================================================

BEGIN;


CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(100) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    is_banned BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_username
ON users(username);

CREATE INDEX IF NOT EXISTS idx_users_joined_at
ON users(joined_at);


CREATE TABLE IF NOT EXISTS files (
    id BIGSERIAL PRIMARY KEY,
    token VARCHAR(100) NOT NULL UNIQUE,

    uploaded_by BIGINT NOT NULL
        REFERENCES users(telegram_id)
        ON DELETE RESTRICT,

    file_id TEXT NOT NULL,
    file_unique_id TEXT,

    file_name TEXT NOT NULL,
    file_type VARCHAR(50) NOT NULL,

    file_size BIGINT NOT NULL DEFAULT 0,
    mime_type VARCHAR(255),

    is_public BOOLEAN NOT NULL DEFAULT FALSE,
    sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,

    views BIGINT NOT NULL DEFAULT 0,
    downloads BIGINT NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT files_size_nonnegative
        CHECK (file_size >= 0),

    CONSTRAINT files_views_nonnegative
        CHECK (views >= 0),

    CONSTRAINT files_downloads_nonnegative
        CHECK (downloads >= 0)
);

CREATE INDEX IF NOT EXISTS idx_files_uploaded_by
ON files(uploaded_by);

CREATE INDEX IF NOT EXISTS idx_files_token
ON files(token);

CREATE INDEX IF NOT EXISTS idx_files_created_at
ON files(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_files_sharing
ON files(sharing_enabled, is_public);


CREATE TABLE IF NOT EXISTS force_channels (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL UNIQUE,
    channel_username VARCHAR(255),
    channel_title VARCHAR(255) NOT NULL,
    invite_link TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_force_channels_active
ON force_channels(is_active);


CREATE TABLE IF NOT EXISTS settings (
    setting_key VARCHAR(150) PRIMARY KEY,
    setting_value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS admin_states (
    admin_id BIGINT PRIMARY KEY,
    state VARCHAR(100) NOT NULL,
    data JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS processed_updates (
    update_id BIGINT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS webhook_updates (
    update_id BIGINT PRIMARY KEY,
    payload JSONB NOT NULL,

    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT webhook_status_valid
        CHECK (
            status IN (
                'pending',
                'processing',
                'done',
                'failed'
            )
        ),

    CONSTRAINT webhook_attempts_nonnegative
        CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS idx_webhook_updates_status
ON webhook_updates(status, created_at);


INSERT INTO settings (setting_key, setting_value)
VALUES
    ('start_message',
     'Welcome to DESI CLOUD! Your personal Telegram cloud storage.'),

    ('help_message',
     'Send a file to upload it to DESI CLOUD.'),

    ('about_message',
     'DESI CLOUD is your personal Telegram cloud storage.'),

    ('star_message',
     'If you enjoy using DESI CLOUD, please support the project.'),

    ('force_join_enabled', 'false')

ON CONFLICT (setting_key) DO NOTHING;


INSERT INTO schema_migrations (version)
VALUES ('001_initial_schema')
ON CONFLICT (version) DO NOTHING;


COMMIT;
