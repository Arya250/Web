import os
import secrets
from urllib.parse import urlparse


def get_required(name):
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Required environment variable missing: {name}"
        )

    return value.strip()


BOT_TOKEN = get_required("BOT_TOKEN")

ADMIN_ID = int(get_required("ADMIN_ID"))

DATABASE_URL = get_required("DATABASE_URL")

RENDER_EXTERNAL_URL = get_required(
    "RENDER_EXTERNAL_URL"
).rstrip("/")

WEBHOOK_SECRET = get_required("WEBHOOK_SECRET")

PORT = int(os.getenv("PORT", "10000"))

BOT_USERNAME = os.getenv(
    "BOT_USERNAME", ""
).strip().lstrip("@")

WEBHOOK_PATH = "/telegram-webhook"

WEBHOOK_URL = (
    f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
)

WEBHOOK_MAX_CONNECTIONS = 40

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

FILES_PER_PAGE = 8

USERS_PER_PAGE = 10

ADMIN_FILES_PER_PAGE = 10

BROADCAST_DELAY = 0.05

UPDATE_QUEUE_SIZE = 500

LOG_LEVEL = os.getenv(
    "LOG_LEVEL", "INFO"
).upper()

SECRET_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{1,256}$"

TOKEN_BYTES = 24


def generate_file_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def validate_webhook_secret(value):
    if not value:
        return False

    return secrets.compare_digest(
        value,
        WEBHOOK_SECRET
    )


def validate_database_url():
    parsed = urlparse(DATABASE_URL)

    if parsed.scheme not in (
        "postgres",
        "postgresql"
    ):
        raise RuntimeError(
            "DATABASE_URL must be a PostgreSQL URL."
        )


validate_database_url()
