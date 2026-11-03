import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().replace("@", "")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DATABASE = os.getenv("MONGO_DATABASE", "desi_cloud").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).strip().rstrip("/")

WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH", "/telegram-webhook"
).strip()

if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

MAX_DOWNLOAD_SIZE = int(
    os.getenv("MAX_DOWNLOAD_SIZE", str(20 * 1024 * 1024))
)

# Compatibility with utils.py
MAX_FILE_SIZE = MAX_DOWNLOAD_SIZE

FILES_PER_PAGE = 8
ADMIN_FILES_PER_PAGE = 10
USERS_PER_PAGE = 10

BROADCAST_DELAY = 0.05

WEBHOOK_URL = (
    f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
    if RENDER_EXTERNAL_URL else ""
)


def validate_config():
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if not MONGO_URI:
        missing.append("MONGO_URI")

    if not ADMIN_ID:
        missing.append("ADMIN_ID")

    if not WEBHOOK_SECRET:
        missing.append("WEBHOOK_SECRET")

    if missing:
        raise RuntimeError(
            "Missing environment variables: " + ", ".join(missing)
        )
