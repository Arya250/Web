import logging
import threading

from datetime import datetime, timezone, timedelta

from pymongo import (
    MongoClient,
    DESCENDING,
    ReturnDocument
)

from pymongo.errors import (
    DuplicateKeyError,
    PyMongoError
)

from config import MONGO_URI, MONGO_DATABASE


logger = logging.getLogger(__name__)


# ==========================================
# DATABASE CONNECTION
# ==========================================

client = None
db = None

users = None
files = None
settings = None
force_channels = None
counters = None
webhook_updates = None

database_lock = threading.RLock()


# ==========================================
# TIME
# ==========================================

def now():
    return datetime.now(timezone.utc)


# ==========================================
# DATABASE INITIALIZATION
# ==========================================

def initialize_database():

    global client, db
    global users, files, settings
    global force_channels, counters
    global webhook_updates

    with database_lock:

        if not MONGO_URI:
            raise RuntimeError("MONGO_URI is missing.")

        if client is not None and db is not None:

            try:
                client.admin.command("ping")
                logger.info("MongoDB connection already active.")
                return True

            except PyMongoError:
                logger.warning(
                    "Existing MongoDB connection unavailable. Reconnecting."
                )

                try:
                    client.close()
                except Exception:
                    pass

                client = None
                db = None

        try:

            logger.info("Connecting to MongoDB Atlas...")

            new_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=10000,
                connectTimeoutMS=10000,
                socketTimeoutMS=20000,
                retryWrites=True,
                appname="DESI_CLOUD"
            )

            # Verify connection before publishing globals.
            new_client.admin.command("ping")

            new_db = new_client[MONGO_DATABASE]

            new_users = new_db["users"]
            new_files = new_db["files"]
            new_settings = new_db["settings"]
            new_force_channels = new_db["force_channels"]
            new_counters = new_db["counters"]
            new_webhook_updates = new_db["webhook_updates"]

            # ==================================
            # INDEXES
            # ==================================

            new_users.create_index(
                "telegram_id",
                unique=True
            )

            new_files.create_index(
                "id",
                unique=True
            )

            new_files.create_index(
                "token",
                unique=True
            )

            new_files.create_index("uploaded_by")
            new_files.create_index("created_at")

            new_settings.create_index(
                "key",
                unique=True
            )

            new_force_channels.create_index(
                "id",
                unique=True
            )

            new_force_channels.create_index(
                "chat_id",
                unique=True
            )

            new_webhook_updates.create_index(
                "update_id",
                unique=True
            )

            new_webhook_updates.create_index([
                ("status", 1),
                ("created_at", 1)
            ])

            new_counters.create_index(
                "_id",
                unique=True
            )

            # ==================================
            # DEFAULT SETTINGS
            # ==================================

            defaults = {
                "start_message": "Welcome to DESI CLOUD!",
                "about_message": "DESI CLOUD - Telegram File Storage.",
                "help_message": "Send a file to upload it to your cloud.",
                "force_join_enabled": False,
                "website_enabled": True,
                "star_message": ""
            }

            for key, value in defaults.items():

                new_settings.update_one(
                    {"key": key},
                    {
                        "$setOnInsert": {
                            "value": value
                        }
                    },
                    upsert=True
                )

            # ==================================
            # RECOVER INTERRUPTED UPDATES
            # ==================================

            new_webhook_updates.update_many(
                {"status": "processing"},
                {
                    "$set": {
                        "status": "pending",
                        "recovered_at": now()
                    }
                }
            )

            # ==================================
            # PUBLISH CONNECTION
            # ==================================

            client = new_client
            db = new_db

            users = new_users
            files = new_files
            settings = new_settings
            force_channels = new_force_channels
            counters = new_counters
            webhook_updates = new_webhook_updates

            logger.info("MongoDB initialized successfully.")

            return True

        except Exception:

            logger.exception(
                "MongoDB initialization failed."
            )

            try:
                new_client.close()
            except Exception:
                pass

            client = None
            db = None

            users = None
            files = None
            settings = None
            force_channels = None
            counters = None
            webhook_updates = None

            raise


# ==========================================
# DATABASE HEALTH
# ==========================================

def is_database_ready():

    if client is None or db is None:
        return False

    try:

        client.admin.command("ping")

        return True

    except Exception:

        logger.exception(
            "MongoDB health check failed."
        )

        return False


def close_database():

    global client, db

    with database_lock:

        if client is not None:

            try:
                client.close()
            except Exception:
                logger.exception(
                    "MongoDB close error."
                )

        client = None
        db = None


# ==========================================
# INTERNAL CHECK
# ==========================================

def require_database():

    if db is None:
        raise RuntimeError(
            "Database is not initialized."
        )


# ==========================================
# USERS
# ==========================================

def register_user(user):

    require_database()

    telegram_id = int(user.id)

    users.update_one(
        {"telegram_id": telegram_id},
        {
            "$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "last_active": now()
            },
            "$setOnInsert": {
                "telegram_id": telegram_id,
                "is_banned": False,
                "joined_at": now()
            }
        },
        upsert=True
    )


def get_all_user_ids():

    require_database()

    return list(
        users.find(
            {
                "is_banned": {
                    "$ne": True
                }
            },
            {
                "telegram_id": 1,
                "_id": 0
            }
        )
    )


def get_users_paginated(page=0, per_page=10):

    require_database()

    page = max(0, int(page))

    skip = page * per_page

    return list(
        users.find()
        .sort("joined_at", DESCENDING)
        .skip(skip)
        .limit(per_page)
    )


def count_users():

    require_database()

    return users.count_documents({})


def get_user(user_id):

    require_database()

    return users.find_one({
        "telegram_id": int(user_id)
    })


# ==========================================
# FILES
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

    require_database()

    next_id = counters.find_one_and_update(
        {"_id": "files"},
        {
            "$inc": {
                "value": 1
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    document = {
        "id": next_id["value"],
        "token": token,
        "file_id": file_id,
        "file_type": file_type,
        "file_name": file_name,
        "file_size": file_size or 0,
        "mime_type": mime_type,
        "caption": caption or "",
        "uploaded_by": int(uploaded_by),
        "views": 0,
        "downloads": 0,
        "is_public": False,
        "sharing_enabled": True,
        "created_at": now(),
        "updated_at": now()
    }

    files.insert_one(document)

    return document


def get_file_by_id(file_id):

    require_database()

    return files.find_one({
        "id": int(file_id)
    })


def get_file_by_token(token):

    require_database()

    return files.find_one({
        "token": token
    })


def get_user_file(file_id, user_id):

    require_database()

    return files.find_one({
        "id": int(file_id),
        "uploaded_by": int(user_id)
    })


def get_user_files(user_id, page=0, per_page=8):

    require_database()

    page = max(0, int(page))

    return list(
        files.find({
            "uploaded_by": int(user_id)
        })
        .sort("created_at", DESCENDING)
        .skip(page * per_page)
        .limit(per_page)
    )


def count_user_files(user_id):

    require_database()

    return files.count_documents({
        "uploaded_by": int(user_id)
    })


def get_all_files(page=0, per_page=10):

    require_database()

    page = max(0, int(page))

    return list(
        files.find()
        .sort("created_at", DESCENDING)
        .skip(page * per_page)
        .limit(per_page)
    )


def count_all_files():

    require_database()

    return files.count_documents({})


def increment_file_views(file_id):

    require_database()

    return files.update_one(
        {"id": int(file_id)},
        {
            "$inc": {
                "views": 1
            }
        }
    )


def increment_file_downloads(file_id):

    require_database()

    return files.update_one(
        {"id": int(file_id)},
        {
            "$inc": {
                "downloads": 1
            }
        }
    )


def rename_file(file_id, user_id, new_name):

    require_database()

    return files.update_one(
        {
            "id": int(file_id),
            "uploaded_by": int(user_id)
        },
        {
            "$set": {
                "file_name": new_name,
                "updated_at": now()
            }
        }
    ).modified_count > 0


def delete_file(file_id, user_id):

    require_database()

    return files.delete_one({
        "id": int(file_id),
        "uploaded_by": int(user_id)
    }).deleted_count > 0


def admin_delete_file(file_id):

    require_database()

    return files.delete_one({
        "id": int(file_id)
    }).deleted_count > 0


def set_file_sharing(file_id, user_id, enabled):

    require_database()

    return files.update_one(
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
    ).modified_count > 0


def get_public_file(token):

    require_database()

    return files.find_one({
        "token": token,
        "sharing_enabled": True,
        "is_public": True
    })


# ==========================================
# SETTINGS
# ==========================================

def get_setting(key, default=None):

    require_database()

    result = settings.find_one({
        "key": key
    })

    if not result:
        return default

    return result.get("value", default)


def set_setting(key, value):

    require_database()

    settings.update_one(
        {"key": key},
        {
            "$set": {
                "value": value
            }
        },
        upsert=True
    )


# ==========================================
# FORCE JOIN
# ==========================================

def add_force_channel(
    chat_id,
    channel_username=None,
    channel_title=None
):

    require_database()

    existing = force_channels.find_one({
        "chat_id": int(chat_id)
    })

    if existing:
        return existing

    next_id = counters.find_one_and_update(
        {"_id": "force_channels"},
        {
            "$inc": {
                "value": 1
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    document = {
        "id": next_id["value"],
        "chat_id": int(chat_id),
        "channel_username": channel_username,
        "channel": channel_username or str(chat_id),
        "channel_title": channel_title or "",
        "created_at": now()
    }

    try:

        force_channels.insert_one(document)

        return document

    except DuplicateKeyError:

        return force_channels.find_one({
            "chat_id": int(chat_id)
        })


def get_force_channels():

    require_database()

    return list(
        force_channels.find()
        .sort("id", DESCENDING)
    )


def remove_force_channel(channel_id):

    require_database()

    return force_channels.delete_one({
        "id": int(channel_id)
    }).deleted_count > 0


# ==========================================
# ADMIN STATISTICS
# ==========================================

def get_statistics():

    require_database()

    total_users = users.count_documents({})
    total_files = files.count_documents({})

    pipeline = [
        {
            "$group": {
                "_id": None,
                "views": {
                    "$sum": "$views"
                },
                "downloads": {
                    "$sum": "$downloads"
                },
                "storage": {
                    "$sum": "$file_size"
                }
            }
        }
    ]

    result = list(
        files.aggregate(pipeline)
    )

    totals = result[0] if result else {}

    return {
        "total_users": total_users,
        "total_files": total_files,
        "total_views": totals.get("views", 0),
        "total_downloads": totals.get("downloads", 0),
        "total_storage": totals.get("storage", 0),

        "shared_files": files.count_documents({
            "sharing_enabled": True,
            "is_public": True
        }),

        "disabled_files": files.count_documents({
            "sharing_enabled": False
        }),

        "total_channels": force_channels.count_documents({})
    }


def get_public_statistics():

    stats = get_statistics()

    return {
        "total_files": stats["total_files"],
        "total_users": stats["total_users"],
        "total_views": stats["total_views"],
        "total_downloads": stats["total_downloads"]
    }


# ==========================================
# WEBHOOK QUEUE
# ==========================================

def save_webhook_update(update_id, payload):

    require_database()

    try:

        webhook_updates.insert_one({
            "update_id": int(update_id),
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "created_at": now(),
            "next_attempt": now()
        })

        return True

    except DuplicateKeyError:

        # Update already exists in the queue.
        return False


def claim_webhook_update():

    require_database()

    current_time = now()

    return webhook_updates.find_one_and_update(
        {
            "status": "pending",
            "$or": [
                {
                    "next_attempt": {
                        "$lte": current_time
                    }
                },
                {
                    "next_attempt": {
                        "$exists": False
                    }
                }
            ]
        },
        {
            "$set": {
                "status": "processing",
                "processing_at": current_time
            },
            "$inc": {
                "attempts": 1
            }
        },
        sort=[
            ("created_at", 1)
        ],
        return_document=ReturnDocument.AFTER
    )


def complete_webhook_update(update_id):

    require_database()

    webhook_updates.update_one(
        {
            "update_id": int(update_id)
        },
        {
            "$set": {
                "status": "completed",
                "completed_at": now()
            },
            {
                "$unset": {
                    "payload": ""
                }
            }
        }
    )


def retry_webhook_update(update_id, error=None):

    require_database()

    update = webhook_updates.find_one({
        "update_id": int(update_id)
    })

    if not update:
        return

    attempts = int(update.get("attempts", 1))

    if attempts >= 5:

        webhook_updates.update_one(
            {
                "update_id": int(update_id)
            },
            {
                "$set": {
                    "status": "failed",
                    "last_error": str(error or "")[:1000],
                    "failed_at": now()
                }
            }
        )

        logger.error(
            "Webhook update %s moved to failed state.",
            update_id
        )

        return

    delay = min(
        60,
        2 ** attempts
    )

    webhook_updates.update_one(
        {
            "update_id": int(update_id)
        },
        {
            "$set": {
                "status": "pending",
                "last_error": str(error or "")[:1000],
                "next_attempt": now() + timedelta(seconds=delay)
            }
        }
    )


def get_webhook_queue_statistics():

    require_database()

    return {
        "pending": webhook_updates.count_documents({
            "status": "pending"
        }),

        "processing": webhook_updates.count_documents({
            "status": "processing"
        }),

        "completed": webhook_updates.count_documents({
            "status": "completed"
        }),

        "failed": webhook_updates.count_documents({
            "status": "failed"
        })
    }
