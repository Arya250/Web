import logging
import secrets
from datetime import datetime, timezone

from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError

from config import MONGO_URI, MONGO_DATABASE

logger = logging.getLogger(__name__)

client = None
db = None

users = None
files = None
settings = None
force_channels = None
admin_states = None
processed_updates = None


def now():
    return datetime.now(timezone.utc)


def initialize_database():
    global client, db
    global users, files, settings
    global force_channels, admin_states, processed_updates

    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is missing")

    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=20000,
        retryWrites=True,
    )

    client.admin.command("ping")

    db = client[MONGO_DATABASE]

    users = db["users"]
    files = db["files"]
    settings = db["settings"]
    force_channels = db["force_channels"]
    admin_states = db["admin_states"]
    processed_updates = db["processed_updates"]

    users.create_index("telegram_id", unique=True)

    files.create_index("token", unique=True)
    files.create_index([("uploaded_by", DESCENDING)])
    files.create_index([("created_at", DESCENDING)])

    settings.create_index("key", unique=True)
    force_channels.create_index("chat_id", unique=True)
    admin_states.create_index("admin_id", unique=True)
    processed_updates.create_index("update_id", unique=True)

    defaults = {
        "start_message": "Welcome to DESI CLOUD!",
        "about_message": "DESI CLOUD - Your Telegram file storage.",
        "help_message": "Send a file to upload it to your cloud.",
        "force_join_enabled": False,
        "website_enabled": True,
    }

    for key, value in defaults.items():
        settings.update_one(
            {"key": key},
            {"$setOnInsert": {
                "key": key,
                "value": value,
                "updated_at": now(),
            }},
            upsert=True,
        )

    logger.info("MongoDB connected and initialized successfully.")
    return True


def close_database():
    global client

    if client:
        client.close()
        client = None


def check_database():
    if client is None:
        raise RuntimeError("Database is not initialized")

    client.admin.command("ping")
    return True


# ---------------- USERS ----------------

def register_user(user_id, username=None, first_name=None, last_name=None):
    users.update_one(
        {"telegram_id": int(user_id)},
        {
            "$set": {
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "last_active": now(),
            },
            "$setOnInsert": {
                "telegram_id": int(user_id),
                "is_banned": False,
                "joined_at": now(),
            },
        },
        upsert=True,
    )


def get_user(user_id):
    return users.find_one({"telegram_id": int(user_id)})


def get_total_users():
    return users.count_documents({})


def get_all_user_ids():
    return [
        item["telegram_id"]
        for item in users.find(
            {"is_banned": {"$ne": True}},
            {"telegram_id": 1},
        )
    ]


def ban_user(user_id):
    users.update_one(
        {"telegram_id": int(user_id)},
        {"$set": {"is_banned": True}},
    )


def unban_user(user_id):
    users.update_one(
        {"telegram_id": int(user_id)},
        {"$set": {"is_banned": False}},
    )


# ---------------- FILES ----------------

def generate_token():
    while True:
        token = secrets.token_urlsafe(12)

        if not files.find_one({"token": token}):
            return token


def save_file(
    token,
    file_id,
    file_type,
    file_name,
    file_size,
    mime_type,
    caption,
    uploaded_by,
):
    document = {
        "token": token,
        "file_id": file_id,
        "file_type": file_type,
        "file_name": file_name,
        "file_size": int(file_size or 0),
        "mime_type": mime_type,
        "caption": caption,
        "uploaded_by": int(uploaded_by),
        "views": 0,
        "downloads": 0,
        "is_public": False,
        "sharing_enabled": True,
        "created_at": now(),
        "updated_at": now(),
    }

    files.insert_one(document)
    return document


def get_file_by_token(token):
    return files.find_one({"token": token})


def get_file_by_id(file_id):
    return files.find_one({"_id": file_id})


def get_user_file(token, user_id):
    return files.find_one({
        "token": token,
        "uploaded_by": int(user_id),
    })


def get_user_files(user_id, limit=20, skip=0):
    return list(
        files.find({"uploaded_by": int(user_id)})
        .sort("created_at", DESCENDING)
        .skip(skip)
        .limit(limit)
    )


def count_user_files(user_id):
    return files.count_documents({"uploaded_by": int(user_id)})


def increment_file_views(token):
    files.update_one(
        {"token": token},
        {"$inc": {"views": 1}},
    )


def increment_file_downloads(token):
    files.update_one(
        {"token": token},
        {"$inc": {"downloads": 1}},
    )


def rename_file(token, user_id, new_name):
    result = files.update_one(
        {
            "token": token,
            "uploaded_by": int(user_id),
        },
        {
            "$set": {
                "file_name": new_name,
                "updated_at": now(),
            }
        },
    )

    return result.modified_count > 0


def delete_file(token, user_id):
    result = files.delete_one({
        "token": token,
        "uploaded_by": int(user_id),
    })

    return result.deleted_count > 0


def set_file_sharing(token, user_id, enabled):
    result = files.update_one(
        {
            "token": token,
            "uploaded_by": int(user_id),
        },
        {
            "$set": {
                "sharing_enabled": bool(enabled),
                "updated_at": now(),
            }
        },
    )

    return result.modified_count > 0


def regenerate_file_token(token, user_id):
    new_token = generate_token()

    result = files.update_one(
        {
            "token": token,
            "uploaded_by": int(user_id),
        },
        {
            "$set": {
                "token": new_token,
                "updated_at": now(),
            }
        },
    )

    return new_token if result.modified_count else None


def get_recent_files(limit=10):
    return list(
        files.find()
        .sort("created_at", DESCENDING)
        .limit(limit)
    )


def get_total_files():
    return files.count_documents({})


def get_total_views():
    result = files.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$views"}}}
    ])

    result = list(result)
    return result[0]["total"] if result else 0


def get_total_downloads():
    result = files.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$downloads"}}}
    ])

    result = list(result)
    return result[0]["total"] if result else 0


# ---------------- SETTINGS ----------------

def get_setting(key, default=None):
    item = settings.find_one({"key": key})
    return item["value"] if item else default


def set_setting(key, value):
    settings.update_one(
        {"key": key},
        {
            "$set": {
                "value": value,
                "updated_at": now(),
            }
        },
        upsert=True,
    )


# ---------------- FORCE JOIN ----------------

def add_force_channel(chat_id, channel_username=None, channel_title=None):
    force_channels.update_one(
        {"chat_id": str(chat_id)},
        {
            "$set": {
                "channel_username": channel_username,
                "channel_title": channel_title,
                "is_active": True,
                "updated_at": now(),
            },
            "$setOnInsert": {"added_at": now()},
        },
        upsert=True,
    )


def get_force_channels():
    return list(force_channels.find({"is_active": True}))


def remove_force_channel(chat_id):
    result = force_channels.delete_one({"chat_id": str(chat_id)})
    return result.deleted_count > 0


# ---------------- ADMIN STATES ----------------

def set_admin_state(admin_id, state, data=None):
    admin_states.update_one(
        {"admin_id": int(admin_id)},
        {
            "$set": {
                "state": state,
                "data": data or {},
                "updated_at": now(),
            }
        },
        upsert=True,
    )


def get_admin_state(admin_id):
    return admin_states.find_one({"admin_id": int(admin_id)})


def clear_admin_state(admin_id):
    admin_states.delete_one({"admin_id": int(admin_id)})


# ---------------- UPDATE PROTECTION ----------------

def is_update_processed(update_id):
    return processed_updates.find_one(
        {"update_id": int(update_id)}
    ) is not None


def mark_update_processed(update_id):
    try:
        processed_updates.insert_one({
            "update_id": int(update_id),
            "processed_at": now(),
        })
        return True
    except PyMongoError:
        return False
