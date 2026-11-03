import os
import time
import logging
import threading
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

client = None
db = None

users = None
files = None
settings = None
channels = None
webhook_updates = None

_db_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc)


def is_database_ready():
    return db is not None and client is not None


def require_database():
    if not is_database_ready():
        raise RuntimeError("MongoDB database is not initialized.")
    return db


def initialize_database():
    global client, db
    global users, files, settings, channels, webhook_updates

    with _db_lock:
        new_client = None

        try:
            mongo_uri = os.getenv("MONGO_URI")
            database_name = os.getenv(
                "MONGO_DATABASE",
                "desi_cloud"
            )

            if not mongo_uri:
                raise RuntimeError("MONGO_URI is missing.")

            new_client = MongoClient(
                mongo_uri,
                serverSelectionTimeoutMS=15000,
                connectTimeoutMS=15000,
                socketTimeoutMS=20000,
                retryWrites=True
            )

            new_client.admin.command("ping")

            new_db = new_client[database_name]

            new_users = new_db["users"]
            new_files = new_db["files"]
            new_settings = new_db["settings"]
            new_channels = new_db["channels"]
            new_webhook_updates = new_db["webhook_updates"]

            new_users.create_index(
                [("user_id", ASCENDING)],
                unique=True
            )

            new_files.create_index(
                [("token", ASCENDING)],
                unique=True
            )

            new_files.create_index(
                [("owner_id", ASCENDING)]
            )

            new_webhook_updates.create_index(
                [("update_id", ASCENDING)],
                unique=True
            )

            new_webhook_updates.create_index(
                [("status", ASCENDING), ("next_retry_at", ASCENDING)]
            )

            new_settings.update_one(
                {"_id": "main"},
                {
                    "$setOnInsert": {
                        "force_join": [],
                        "start_message": (
                            "Welcome to DESI CLOUD!"
                        ),
                        "updated_at": now()
                    }
                },
                upsert=True
            )

            client = new_client
            db = new_db

            users = new_users
            files = new_files
            settings = new_settings
            channels = new_channels
            webhook_updates = new_webhook_updates

            logger.info(
                "MongoDB initialized successfully: %s",
                database_name
            )

            return True

        except Exception:
            logger.exception("MongoDB initialization failed.")

            if new_client is not None:
                try:
                    new_client.close()
                except Exception:
                    pass

            client = None
            db = None

            users = None
            files = None
            settings = None
            channels = None
            webhook_updates = None

            return False


def close_database():
    global client, db
    global users, files, settings, channels, webhook_updates

    with _db_lock:
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.exception("MongoDB close failed.")

        client = None
        db = None

        users = None
        files = None
        settings = None
        channels = None
        webhook_updates = None


# --------------------------------------------------
# WEBHOOK UPDATE QUEUE
# --------------------------------------------------

def save_webhook_update(update_id, payload):
    collection = require_database()["webhook_updates"]

    update_id = int(update_id)

    result = collection.update_one(
        {"update_id": update_id},
        {
            "$setOnInsert": {
                "update_id": update_id,
                "payload": payload,
                "status": "pending",
                "attempts": 0,
                "created_at": now(),
                "next_retry_at": now()
            }
        },
        upsert=True
    )

    return result.upserted_id is not None


def get_pending_webhook_updates(limit=20):
    collection = require_database()["webhook_updates"]

    current_time = now()

    records = collection.find(
        {
            "status": {"$in": ["pending", "retry"]},
            "next_retry_at": {"$lte": current_time}
        }
    ).sort(
        "update_id",
        ASCENDING
    ).limit(limit)

    return list(records)


def mark_webhook_processing(update_id):
    collection = require_database()["webhook_updates"]

    result = collection.update_one(
        {
            "update_id": int(update_id),
            "status": {"$in": ["pending", "retry"]}
        },
        {
            "$set": {
                "status": "processing",
                "processing_at": now()
            },
            "$inc": {
                "attempts": 1
            }
        }
    )

    return result.modified_count > 0


def complete_webhook_update(update_id):
    collection = require_database()["webhook_updates"]

    collection.update_one(
        {"update_id": int(update_id)},
        {
            "$set": {
                "status": "completed",
                "completed_at": now()
            },
            "$unset": {
                "payload": ""
            }
        }
    )


def retry_webhook_update(update_id, error=None):
    collection = require_database()["webhook_updates"]

    record = collection.find_one(
        {"update_id": int(update_id)},
        {"attempts": 1}
    )

    attempts = int((record or {}).get("attempts", 1))

    delay = min(300, 2 ** min(attempts, 8))

    collection.update_one(
        {"update_id": int(update_id)},
        {
            "$set": {
                "status": "retry",
                "last_error": str(error or "")[:1000],
                "next_retry_at": datetime.fromtimestamp(
                    time.time() + delay,
                    tz=timezone.utc
                )
            }
        }
    )


def get_webhook_queue_stats():
    collection = require_database()["webhook_updates"]

    return {
        "pending": collection.count_documents(
            {"status": "pending"}
        ),
        "processing": collection.count_documents(
            {"status": "processing"}
        ),
        "retry": collection.count_documents(
            {"status": "retry"}
        ),
        "completed": collection.count_documents(
            {"status": "completed"}
        )
    }
