import logging
import threading
import time

from datetime import datetime, timezone

from pymongo import (
    MongoClient,
    ASCENDING,
    DESCENDING
)
from pymongo.errors import PyMongoError, DuplicateKeyError

from config import MONGO_URI, MONGO_DATABASE


logger = logging.getLogger(__name__)

client = None
db = None

users = None
files = None
settings = None
channels = None
webhook_updates = None
counters = None

_db_lock = threading.RLock()


# ==========================================
# BASIC HELPERS
# ==========================================

def now():
    return datetime.now(timezone.utc)


def is_database_ready():
    return client is not None and db is not None


def require_database():
    if not is_database_ready():
        raise RuntimeError("MongoDB is not initialized.")

    return db


def next_id(name):
    collection = require_database()["counters"]

    result = collection.find_one_and_update(
        {"_id": name},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=True
    )

    return result["value"]


# ==========================================
# DATABASE INITIALIZATION
# ==========================================

def initialize_database():
    global client, db
    global users, files, settings
    global channels, webhook_updates, counters

    with _db_lock:
        new_client = None

        try:
            if not MONGO_URI:
                raise RuntimeError("MONGO_URI is missing.")

            new_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=15000,
                connectTimeoutMS=15000,
                socketTimeoutMS=20000,
                retryWrites=True
            )

            new_client.admin.command("ping")

            new_db = new_client[MONGO_DATABASE]

            new_users = new_db["users"]
            new_files = new_db["files"]
            new_settings = new_db["settings"]
            new_channels = new_db["channels"]
            new_webhook_updates = new_db["webhook_updates"]
            new_counters = new_db["counters"]

            # Indexes
            new_users.create_index(
                [("telegram_id", ASCENDING)],
                unique=True
            )

            new_files.create_index(
                [("id", ASCENDING)],
                unique=True
            )

            new_files.create_index(
                [("token", ASCENDING)],
                unique=True
            )

            new_files.create_index(
                [("uploaded_by", ASCENDING)]
            )

            new_files.create_index(
                [("owner_id", ASCENDING)]
            )

            new_channels.create_index(
                [("id", ASCENDING)],
                unique=True
            )

            new_webhook_updates.create_index(
                [("update_id", ASCENDING)],
                unique=True
            )

            new_webhook_updates.create_index(
                [
                    ("status", ASCENDING),
                    ("next_retry_at", ASCENDING)
                ]
            )

            # Default settings
            new_settings.update_one(
                {"_id": "main"},
                {
                    "$setOnInsert": {
                        "start_message": "Welcome to DESI CLOUD!",
                        "help_message": (
                            "Send a file to upload it to your cloud."
                        ),
                        "about_message": (
                            "DESI CLOUD - Telegram File Storage."
                        ),
                        "star_message": (
                            "Thank you for using DESI CLOUD!"
                        ),
                        "force_join_enabled": False,
                        "website_enabled": True,
                        "created_at": now()
                    }
                },
                upsert=True
            )

            # Activate database
            client = new_client
            db = new_db

            users = new_users
            files = new_files
            settings = new_settings
            channels = new_channels
            webhook_updates = new_webhook_updates
            counters = new_counters

            logger.info(
                "MongoDB initialized successfully: %s",
                MONGO_DATABASE
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
            counters = None

            return False


def close_database():
    global client, db
    global users, files, settings
    global channels, webhook_updates, counters

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
        counters = None


# ==========================================
# USER MANAGEMENT
# ==========================================

def register_user(user):
    collection = require_database()["users"]

    user_id = int(user.id)

    data = {
        "telegram_id": user_id,
        "user_id": user_id,
        "first_name": user.first_name or "Unknown",
        "last_name": user.last_name or "",
        "username": user.username or "",
        "is_bot": bool(user.is_bot),
        "updated_at": now()
    }

    collection.update_one(
        {"telegram_id": user_id},
        {
            "$set": data,
            "$setOnInsert": {
                "created_at": now()
            }
        },
        upsert=True
    )

    return collection.find_one(
        {"telegram_id": user_id}
    )


def count_users():
    return require_database()["users"].count_documents({})


def get_users_paginated(page=0, limit=10):
    page = max(0, int(page))
    limit = max(1, int(limit))

    return list(
        require_database()["users"]
        .find({})
        .sort("created_at", DESCENDING)
        .skip(page * limit)
        .limit(limit)
    )


def get_all_user_ids():
    return list(
        require_database()["users"].find(
            {},
            {"telegram_id": 1}
        )
    )


# ==========================================
# FILE MANAGEMENT
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
    collection = require_database()["files"]

    uploaded_by = int(uploaded_by)

    file_document = {
        "id": next_id("file"),
        "token": token,
        "file_id": file_id,
        "file_type": file_type,
        "file_name": file_name or "Unknown",
        "file_size": int(file_size or 0),
        "mime_type": mime_type,
        "caption": caption or "",
        "uploaded_by": uploaded_by,
        "owner_id": uploaded_by,
        "sharing_enabled": True,
        "is_public": True,
        "views": 0,
        "downloads": 0,
        "created_at": now(),
        "updated_at": now()
    }

    collection.insert_one(file_document)

    return file_document


def count_user_files(user_id):
    return require_database()["files"].count_documents({
        "uploaded_by": int(user_id)
    })


def get_user_files(user_id, page=0, limit=8):
    page = max(0, int(page))
    limit = max(1, int(limit))

    return list(
        require_database()["files"]
        .find({"uploaded_by": int(user_id)})
        .sort("created_at", DESCENDING)
        .skip(page * limit)
        .limit(limit)
    )


def get_user_file(file_id, user_id):
    return require_database()["files"].find_one({
        "id": int(file_id),
        "uploaded_by": int(user_id)
    })


def get_file_by_token(token):
    return require_database()["files"].find_one({
        "token": str(token)
    })


def get_file_by_id(file_id):
    return require_database()["files"].find_one({
        "id": int(file_id)
    })


def increment_file_views(file_id):
    return require_database()["files"].update_one(
        {"id": int(file_id)},
        {
            "$inc": {"views": 1},
            "$set": {"updated_at": now()}
        }
    ).modified_count > 0


def increment_file_downloads(file_id):
    return require_database()["files"].update_one(
        {"id": int(file_id)},
        {
            "$inc": {"downloads": 1},
            "$set": {"updated_at": now()}
        }
    ).modified_count > 0


def set_file_sharing(file_id, user_id, enabled):
    result = require_database()["files"].update_one(
        {
            "id": int(file_id),
            "uploaded_by": int(user_id)
        },
        {
            "$set": {
                "sharing_enabled": bool(enabled),
                "is_public": bool(enabled),
                "updated_at": now()
            }
        }
    )

    return result.modified_count > 0 or result.matched_count > 0


def rename_file(file_id, user_id, new_name):
    result = require_database()["files"].update_one(
        {
            "id": int(file_id),
            "uploaded_by": int(user_id)
        },
        {
            "$set": {
                "file_name": str(new_name).strip(),
                "updated_at": now()
            }
        }
    )

    return result.matched_count > 0


def delete_file(file_id, user_id):
    result = require_database()["files"].delete_one({
        "id": int(file_id),
        "uploaded_by": int(user_id)
    })

    return result.deleted_count > 0


def count_all_files():
    return require_database()["files"].count_documents({})


def get_all_files(page=0, limit=10):
    page = max(0, int(page))
    limit = max(1, int(limit))

    return list(
        require_database()["files"]
        .find({})
        .sort("created_at", DESCENDING)
        .skip(page * limit)
        .limit(limit)
    )


def admin_delete_file(file_id):
    result = require_database()["files"].delete_one({
        "id": int(file_id)
    })

    return result.deleted_count > 0


# ==========================================
# SETTINGS
# ==========================================

def get_setting(key, default=None):
    document = require_database()["settings"].find_one(
        {"_id": "main"}
    )

    if not document:
        return default

    return document.get(key, default)


def set_setting(key, value):
    require_database()["settings"].update_one(
        {"_id": "main"},
        {
            "$set": {
                key: value,
                "updated_at": now()
            }
        },
        upsert=True
    )

    return True


# ==========================================
# FORCE JOIN CHANNELS
# ==========================================

def get_force_channels():
    return list(
        require_database()["channels"]
        .find({})
        .sort("created_at", DESCENDING)
    )


def add_force_channel(
    chat_id,
    channel_username=None,
    channel_title=None
):
    collection = require_database()["channels"]

    chat_id = int(chat_id)

    existing = collection.find_one({
        "chat_id": chat_id
    })

    if existing:
        collection.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "channel_username": channel_username or "",
                    "channel": channel_username or "",
                    "channel_title": channel_title or "",
                    "updated_at": now()
                }
            }
        )

        return existing.get("id")

    channel_id = next_id("channel")

    collection.insert_one({
        "id": channel_id,
        "chat_id": chat_id,
        "channel_username": channel_username or "",
        "channel": channel_username or "",
        "channel_title": channel_title or "",
        "created_at": now()
    })

    return channel_id


def remove_force_channel(channel_id):
    result = require_database()["channels"].delete_one({
        "id": int(channel_id)
    })

    return result.deleted_count > 0


# ==========================================
# STATISTICS
# ==========================================

def get_statistics():
    collection = require_database()["files"]

    total_users = count_users()
    total_files = collection.count_documents({})

    pipeline = [
        {
            "$group": {
                "_id": None,
                "total_views": {"$sum": "$views"},
                "total_downloads": {"$sum": "$downloads"},
                "total_storage": {"$sum": "$file_size"}
            }
        }
    ]

    result = list(collection.aggregate(pipeline))

    totals = result[0] if result else {}

    shared_files = collection.count_documents({
        "sharing_enabled": True
    })

    disabled_files = collection.count_documents({
        "sharing_enabled": False
    })

    total_channels = require_database()["channels"].count_documents({})

    return {
        "total_users": total_users,
        "total_files": total_files,
        "total_views": int(totals.get("total_views", 0)),
        "total_downloads": int(totals.get("total_downloads", 0)),
        "total_storage": int(totals.get("total_storage", 0)),
        "shared_files": shared_files,
        "disabled_files": disabled_files,
        "total_channels": total_channels
    }


# ==========================================
# WEBHOOK UPDATE QUEUE
# ==========================================

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

    return list(
        collection.find({
            "status": {"$in": ["pending", "retry"]},
            "next_retry_at": {"$lte": now()}
        })
        .sort("update_id", ASCENDING)
        .limit(max(1, int(limit)))
    )


def mark_webhook_processing(update_id):
    result = require_database()["webhook_updates"].update_one(
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
    require_database()["webhook_updates"].update_one(
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
        "pending": collection.count_documents({
            "status": "pending"
        }),
        "processing": collection.count_documents({
            "status": "processing"
        }),
        "retry": collection.count_documents({
            "status": "retry"
        }),
        "completed": collection.count_documents({
            "status": "completed"
        })
    }
