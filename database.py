import logging

from datetime import datetime, timezone

from pymongo import (
    MongoClient,
    DESCENDING,
    ReturnDocument
)

from pymongo.errors import DuplicateKeyError

from config import MONGO_URI, MONGO_DATABASE


logger = logging.getLogger(__name__)

client = None
db = None

users = None
files = None
settings = None
force_channels = None
counters = None
webhook_updates = None


def now():
    return datetime.now(timezone.utc)


def initialize_database():
    global client, db
    global users, files, settings
    global force_channels, counters, webhook_updates

    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is missing.")

    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        retryWrites=True
    )

    client.admin.command("ping")

    db = client[MONGO_DATABASE]

    users = db["users"]
    files = db["files"]
    settings = db["settings"]
    force_channels = db["force_channels"]
    counters = db["counters"]
    webhook_updates = db["webhook_updates"]

    users.create_index("telegram_id", unique=True)

    files.create_index("id", unique=True)
    files.create_index("token", unique=True)
    files.create_index("uploaded_by")
    files.create_index("created_at")

    settings.create_index("key", unique=True)

    force_channels.create_index("id", unique=True)
    force_channels.create_index("chat_id", unique=True)

    webhook_updates.create_index("update_id", unique=True)

    defaults = {
        "start_message": "Welcome to DESI CLOUD!",
        "about_message": "DESI CLOUD - Telegram File Storage.",
        "help_message": "Send a file to upload it to your cloud.",
        "force_join_enabled": False,
        "website_enabled": True,
        "star_message": ""
    }

    for key, value in defaults.items():
        settings.update_one(
            {"key": key},
            {"$setOnInsert": {"value": value}},
            upsert=True
        )

    # Recover updates left in processing state after a restart.
    webhook_updates.update_many(
        {"status": "processing"},
        {"$set": {"status": "pending"}}
    )

    logger.info("MongoDB initialized successfully.")


def close_database():
    global client

    if client:
        client.close()


# ---------------- USERS ----------------

def register_user(user):
    telegram_id = user.id

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
    return list(
        users.find(
            {"is_banned": {"$ne": True}},
            {"telegram_id": 1, "_id": 0}
        )
    )


def get_users_paginated(page=0, per_page=10):
    skip = page * per_page

    return list(
        users.find()
        .sort("joined_at", DESCENDING)
        .skip(skip)
        .limit(per_page)
    )


def count_users():
    return users.count_documents({})


def get_user(user_id):
    return users.find_one({"telegram_id": int(user_id)})


# ---------------- FILES ----------------

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
    next_id = counters.find_one_and_update(
        {"_id": "files"},
        {"$inc": {"value": 1}},
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
    return files.find_one({"id": int(file_id)})


def get_file_by_token(token):
    return files.find_one({"token": token})


def get_user_file(file_id, user_id):
    return files.find_one({
        "id": int(file_id),
        "uploaded_by": int(user_id)
    })


def get_user_files(user_id, page=0, per_page=8):
    return list(
        files.find({"uploaded_by": int(user_id)})
        .sort("created_at", DESCENDING)
        .skip(page * per_page)
        .limit(per_page)
    )


def count_user_files(user_id):
    return files.count_documents({
        "uploaded_by": int(user_id)
    })


def get_all_files(page=0, per_page=10):
    return list(
        files.find()
        .sort("created_at", DESCENDING)
        .skip(page * per_page)
        .limit(per_page)
    )


def count_all_files():
    return files.count_documents({})


def increment_file_views(file_id):
    return files.update_one(
        {"id": int(file_id)},
        {"$inc": {"views": 1}}
    )


def increment_file_downloads(file_id):
    return files.update_one(
        {"id": int(file_id)},
        {"$inc": {"downloads": 1}}
    )


def rename_file(file_id, user_id, new_name):
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
    return files.delete_one({
        "id": int(file_id),
        "uploaded_by": int(user_id)
    }).deleted_count > 0


def admin_delete_file(file_id):
    return files.delete_one({
        "id": int(file_id)
    }).deleted_count > 0


def set_file_sharing(file_id, user_id, enabled):
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
    return files.find_one({
        "token": token,
        "sharing_enabled": True,
        "is_public": True
    })


# ---------------- SETTINGS ----------------

def get_setting(key, default=None):
    result = settings.find_one({"key": key})

    if not result:
        return default

    return result.get("value", default)


def set_setting(key, value):
    settings.update_one(
        {"key": key},
        {"$set": {"value": value}},
        upsert=True
    )


# ---------------- FORCE JOIN ----------------

def add_force_channel(chat_id, channel_username=None, channel_title=None):
    next_id = counters.find_one_and_update(
        {"_id": "force_channels"},
        {"$inc": {"value": 1}},
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
        return force_channels.find_one({"chat_id": int(chat_id)})


def get_force_channels():
    return list(force_channels.find().sort("id", DESCENDING))


def remove_force_channel(channel_id):
    return force_channels.delete_one({
        "id": int(channel_id)
    }).deleted_count > 0


# ---------------- ADMIN STATISTICS ----------------

def get_statistics():
    total_users = users.count_documents({})
    total_files = files.count_documents({})

    pipeline = [{
        "$group": {
            "_id": None,
            "views": {"$sum": "$views"},
            "downloads": {"$sum": "$downloads"},
            "storage": {"$sum": "$file_size"}
        }
    }]

    result = list(files.aggregate(pipeline))

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


# ---------------- WEBHOOK QUEUE ----------------

def save_webhook_update(update_id, payload):
    try:
        webhook_updates.insert_one({
            "update_id": int(update_id),
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "created_at": now()
        })
        return True
    except DuplicateKeyError:
        return False


def claim_webhook_update():
    return webhook_updates.find_one_and_update(
        {"status": "pending"},
        {
            "$set": {
                "status": "processing",
                "processing_at": now()
            },
            "$inc": {"attempts": 1}
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER
    )


def complete_webhook_update(update_id):
    webhook_updates.delete_one({
        "update_id": int(update_id)
    })


def retry_webhook_update(update_id, error=None):
    webhook_updates.update_one(
        {"update_id": int(update_id)},
        {
            "$set": {
                "status": "pending",
                "last_error": str(error or "")[:1000]
            }
        }
    )
