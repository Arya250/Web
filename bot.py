import os
import re
import json
import uuid
import time
import hmac
import logging
import threading
from datetime import datetime
from urllib.parse import quote

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, Response, redirect
from dotenv import load_dotenv

load_dotenv()

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

WEBHOOK_PATH = "/telegram-webhook"
MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("AMR_CLOUD")

app = Flask(__name__)

db_lock = threading.RLock()
http = requests.Session()


# =========================================================
# VALIDATION
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID environment variable missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable missing")

if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET environment variable missing")

if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
    raise RuntimeError("WEBHOOK_SECRET contains invalid characters")


# =========================================================
# TELEGRAM API
# =========================================================

def tg(method, data=None, timeout=40):
    try:
        response = http.post(
            f"{API_URL}/{method}",
            json=data or {},
            timeout=timeout
        )

        result = response.json()

        if not result.get("ok"):
            log.error(
                "TELEGRAM API ERROR | %s | %s",
                method,
                result
            )

        return result

    except Exception:
        log.exception("TELEGRAM REQUEST FAILED | %s", method)
        return {"ok": False, "description": "Request failed"}


def send_message(chat_id, text, keyboard=None, parse_mode="HTML"):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return tg("sendMessage", data)


def edit_message(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return tg("editMessageText", data)


def answer_callback(callback_id, text=""):
    return tg(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": False
        }
    )


def send_document(chat_id, file_id, caption=""):
    return tg(
        "sendDocument",
        {
            "chat_id": chat_id,
            "document": file_id,
            "caption": caption
        }
    )


# =========================================================
# DATABASE
# =========================================================

def get_connection():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=15
    )


def db_execute(query, params=None, fetchone=False, fetchall=False):
    with db_lock:
        conn = get_connection()

        try:
            with conn.cursor() as cur:
                cur.execute(query, params or ())

                result = None

                if fetchone:
                    result = cur.fetchone()

                elif fetchall:
                    result = cur.fetchall()

                conn.commit()
                return result

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()


def init_db():
    log.info("Initializing PostgreSQL database")

    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS files (
            token TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            file_name TEXT,
            mime_type TEXT,
            file_size BIGINT DEFAULT 0,
            downloads BIGINT DEFAULT 0,
            views BIGINT DEFAULT 0,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id SERIAL PRIMARY KEY,
            chat_id TEXT UNIQUE NOT NULL,
            channel_name TEXT,
            channel_link TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS processed_updates (
            update_id BIGINT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS admin_states (
            admin_id BIGINT PRIMARY KEY,
            state TEXT,
            data JSONB DEFAULT '{}'::jsonb,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # IMPORTANT: Fix for the exact error in your Render logs.
    db_execute("""
        ALTER TABLE admin_states
        ADD COLUMN IF NOT EXISTS data JSONB DEFAULT '{}'::jsonb
    """)

    # Repair NULL values from older database versions.
    db_execute("""
        UPDATE admin_states
        SET data = '{}'::jsonb
        WHERE data IS NULL
    """)

    log.info("DATABASE INITIALIZED SUCCESSFULLY")


# =========================================================
# SETTINGS
# =========================================================

def get_setting(key, default=""):
    row = db_execute(
        "SELECT value FROM settings WHERE key=%s",
        (key,),
        fetchone=True
    )

    return row["value"] if row else default


def set_setting(key, value):
    db_execute("""
        INSERT INTO settings(key, value)
        VALUES(%s, %s)
        ON CONFLICT(key)
        DO UPDATE SET value=EXCLUDED.value
    """, (key, str(value)))


# =========================================================
# ADMIN STATES
# =========================================================

def set_state(admin_id, state, data=None):
    db_execute("""
        INSERT INTO admin_states(admin_id, state, data, updated_at)
        VALUES(%s, %s, %s::jsonb, CURRENT_TIMESTAMP)
        ON CONFLICT(admin_id)
        DO UPDATE SET
            state=EXCLUDED.state,
            data=EXCLUDED.data,
            updated_at=CURRENT_TIMESTAMP
    """, (
        admin_id,
        state,
        json.dumps(data or {})
    ))


def get_state(admin_id):
    row = db_execute("""
        SELECT state, data
        FROM admin_states
        WHERE admin_id=%s
    """, (admin_id,), fetchone=True)

    if not row:
        return None, {}

    return row["state"], row["data"] or {}


def clear_state(admin_id):
    db_execute(
        "DELETE FROM admin_states WHERE admin_id=%s",
        (admin_id,)
    )


# =========================================================
# KEYBOARDS
# =========================================================

def admin_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📤 Upload File", "callback_data": "upload"},
                {"text": "📁 Files List", "callback_data": "files"}
            ],
            [
                {"text": "📝 Start Message", "callback_data": "startmsg"},
                {"text": "⭐ Star Message", "callback_data": "starmsg"}
            ],
            [
                {"text": "📢 Channels", "callback_data": "channels"},
                {"text": "📊 Statistics", "callback_data": "status"}
            ],
            [
                {"text": "📣 Broadcast", "callback_data": "broadcast"}
            ]
        ]
    }


def back_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "⬅️ Admin Panel", "callback_data": "admin"}]
        ]
    }


def user_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "⭐ Star Message", "callback_data": "showstar"}]
        ]
    }


def channel_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "➕ Add Channel", "callback_data": "addchannel"}],
            [{"text": "📋 List Channels", "callback_data": "listchannels"}],
            [{"text": "⬅️ Admin Panel", "callback_data": "admin"}]
        ]
    }


# =========================================================
# USER MANAGEMENT
# =========================================================

def save_user(user):
    user_id = user.get("id")

    if not user_id:
        return

    db_execute("""
        INSERT INTO users(user_id, first_name, username)
        VALUES(%s, %s, %s)
        ON CONFLICT(user_id)
        DO UPDATE SET
            first_name=EXCLUDED.first_name,
            username=EXCLUDED.username
    """, (
        user_id,
        user.get("first_name", ""),
        user.get("username", "")
    ))


def is_admin(user_id):
    return int(user_id) == ADMIN_ID


# =========================================================
# CHANNEL / FORCE JOIN
# =========================================================

def get_channels():
    return db_execute("""
        SELECT * FROM channels
        ORDER BY id DESC
    """, fetchall=True) or []


def check_join(user_id):
    channels = get_channels()

    for channel in channels:
        result = tg("getChatMember", {
            "chat_id": channel["chat_id"],
            "user_id": user_id
        })

        if not result.get("ok"):
            log.error(
                "FORCE JOIN CHECK FAILED | channel=%s",
                channel["chat_id"]
            )
            return False

        status = result["result"].get("status")

        if status not in ("member", "administrator", "creator"):
            return False

    return True


def join_keyboard():
    rows = []

    for channel in get_channels():
        link = channel.get("channel_link")

        if link:
            rows.append([
                {
                    "text": "Join " + (channel.get("channel_name") or "Channel"),
                    "url": link
                }
            ])

    rows.append([
        {"text": "✅ Verify Join", "callback_data": "verifyjoin"}
    ])

    return {"inline_keyboard": rows}


def require_join(chat_id):
    return send_message(
        chat_id,
        "🔒 <b>Access Restricted</b>\n\n"
        "Please join all required channels first.\n"
        "Then press Verify Join.",
        join_keyboard()
    )


# =========================================================
# FILE MANAGEMENT
# =========================================================

def save_file(file_id, file_name, mime_type, file_size):
    token = uuid.uuid4().hex[:20]

    db_execute("""
        INSERT INTO files(
            token, file_id, file_name, mime_type, file_size
        )
        VALUES(%s, %s, %s, %s, %s)
    """, (
        token,
        file_id,
        file_name or "Telegram File",
        mime_type or "application/octet-stream",
        file_size or 0
    ))

    return token


def get_file(token):
    return db_execute("""
        SELECT * FROM files WHERE token=%s
    """, (token,), fetchone=True)


def file_link(token):
    return f"{RENDER_EXTERNAL_URL}/file/{token}"


def show_files(chat_id):
    files = db_execute("""
        SELECT token, file_name, downloads, uploaded_at
        FROM files
        ORDER BY uploaded_at DESC
        LIMIT 20
    """, fetchall=True) or []

    if not files:
        return send_message(
            chat_id,
            "📁 No files uploaded yet.",
            back_keyboard()
        )

    for item in files:
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "🗑 Delete",
                        "callback_data": "delete_" + item["token"]
                    }
                ]
            ]
        }

        text = (
            f"📄 <b>{item['file_name']}</b>\n"
            f"⬇️ Downloads: {item['downloads']}\n"
            f"🔗 <code>{file_link(item['token'])}</code>"
        )

        send_message(chat_id, text, keyboard)


# =========================================================
# ADMIN PANEL
# =========================================================

def show_admin(chat_id):
    send_message(
        chat_id,
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "      ☁️ <b>AMR CLOUD</b>\n"
        "       ADMIN PANEL\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Select an option below:",
        admin_keyboard()
    )


def show_status(chat_id):
    users = db_execute(
        "SELECT COUNT(*) AS total FROM users",
        fetchone=True
    )["total"]

    files = db_execute(
        "SELECT COUNT(*) AS total FROM files",
        fetchone=True
    )["total"]

    downloads = db_execute(
        "SELECT COALESCE(SUM(downloads),0) AS total FROM files",
        fetchone=True
    )["total"]

    text = (
        "📊 <b>AMR CLOUD STATISTICS</b>\n\n"
        f"👥 Users: <b>{users}</b>\n"
        f"📁 Files: <b>{files}</b>\n"
        f"⬇️ Downloads: <b>{downloads}</b>"
    )

    send_message(chat_id, text, back_keyboard())


# =========================================================
# CALLBACK HANDLER
# =========================================================

def handle_callback(callback):
    callback_id = callback["id"]
    user = callback.get("from", {})
    user_id = int(user.get("id", 0))

    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    data = callback.get("data", "")

    log.info(
        "CALLBACK RECEIVED | user=%s | data=%s",
        user_id,
        data
    )

    # Always answer callback so Telegram button stops loading.
    answer_callback(callback_id, "Processing...")

    if not chat_id:
        return

    # Normal user callbacks.
    if data == "showstar":
        star = get_setting(
            "star_message",
            "⭐ Thank you for supporting AMR CLOUD!"
        )

        send_message(chat_id, star)
        return

    if data == "verifyjoin":
        if check_join(user_id):
            send_message(
                chat_id,
                "✅ Verification successful!\n"
                "Now send your file link to access it."
            )
        else:
            require_join(chat_id)

        return

    # Admin authorization.
    if not is_admin(user_id):
        log.warning(
            "ADMIN CALLBACK REJECTED | user=%s | data=%s",
            user_id,
            data
        )
        return

    log.info("ADMIN CALLBACK ACCEPTED | %s", data)

    try:

        if data == "admin":
            show_admin(chat_id)

        elif data == "upload":
            set_state(user_id, "upload")
            send_message(
                chat_id,
                "📤 <b>Upload File</b>\n\n"
                "Now send the document or video.",
                back_keyboard()
            )

        elif data == "files":
            show_files(chat_id)

        elif data == "startmsg":
            set_state(user_id, "startmsg")
            send_message(
                chat_id,
                "📝 Send your new Start Message.",
                back_keyboard()
            )

        elif data == "starmsg":
            set_state(user_id, "starmsg")
            send_message(
                chat_id,
                "⭐ Send your new Star Message.",
                back_keyboard()
            )

        elif data == "channels":
            send_message(
                chat_id,
                "📢 <b>Channel Management</b>",
                channel_keyboard()
            )

        elif data == "addchannel":
            set_state(user_id, "addchannel")
            send_message(
                chat_id,
                "➕ Send channel details in this format:\n\n"
                "<code>@channelusername | https://t.me/channelusername</code>\n\n"
                "The bot must be an administrator in the channel.",
                back_keyboard()
            )

        elif data == "listchannels":
            channels = get_channels()

            if not channels:
                send_message(
                    chat_id,
                    "No channels added.",
                    channel_keyboard()
                )
                return

            for channel in channels:
                keyboard = {
                    "inline_keyboard": [[
                        {
                            "text": "🗑 Remove",
                            "callback_data": f"removechannel_{channel['id']}"
                        }
                    ]]
                }

                send_message(
                    chat_id,
                    f"📢 <b>{channel['channel_name']}</b>\n"
                    f"ID: <code>{channel['chat_id']}</code>\n"
                    f"{channel['channel_link']}",
                    keyboard
                )

        elif data.startswith("removechannel_"):
            channel_id = int(data.split("_", 1)[1])

            db_execute(
                "DELETE FROM channels WHERE id=%s",
                (channel_id,)
            )

            send_message(
                chat_id,
                "✅ Channel removed.",
                channel_keyboard()
            )

        elif data == "status":
            show_status(chat_id)

        elif data == "broadcast":
            set_state(user_id, "broadcast")
            send_message(
                chat_id,
                "📣 Send the message you want to broadcast.",
                back_keyboard()
            )

        elif data.startswith("delete_"):
            token = data.replace("delete_", "", 1)

            db_execute(
                "DELETE FROM files WHERE token=%s",
                (token,)
            )

            send_message(
                chat_id,
                "🗑 File deleted.",
                back_keyboard()
            )

        else:
            log.warning("UNKNOWN CALLBACK | %s", data)

            send_message(
                chat_id,
                "Unknown button action.",
                back_keyboard()
            )

    except Exception:
        log.exception(
            "ADMIN ACTION FAILED | user=%s | action=%s",
            user_id,
            data
        )

        send_message(
            chat_id,
            "❌ Action failed. Check Render logs.",
            back_keyboard()
        )


# =========================================================
# MESSAGE HANDLER
# =========================================================

def handle_message(message):
    user = message.get("from", {})
    user_id = int(user.get("id", 0))
    chat_id = message.get("chat", {}).get("id")

    if not chat_id or not user_id:
        return

    save_user(user)

    text = message.get("text", "")

    # Cancel current operation.
    if text == "/cancel":
        clear_state(user_id)

        send_message(
            chat_id,
            "❌ Operation cancelled."
        )
        return

    # Start command.
    if text.startswith("/start"):
        clear_state(user_id)

        parts = text.split(maxsplit=1)
        payload = parts[1] if len(parts) > 1 else ""

        if is_admin(user_id):
            show_admin(chat_id)
            return

        if not check_join(user_id):
            require_join(chat_id)
            return

        # File deep link: /start TOKEN
        if payload:
            file_data = get_file(payload)

            if file_data:
                db_execute("""
                    UPDATE files
                    SET views=views+1
                    WHERE token=%s
                """, (payload,))

                send_document(
                    chat_id,
                    file_data["file_id"],
                    file_data["file_name"]
                )
                return

        welcome = get_setting(
            "start_message",
            "☁️ Welcome to AMR CLOUD!\n\n"
            "Send a file link to access your file."
        )

        send_message(chat_id, welcome, user_keyboard())
        return

    if text.startswith("/stats") and is_admin(user_id):
        show_status(chat_id)
        return

    # Admin-only operations.
    if not is_admin(user_id):
        return

    state, state_data = get_state(user_id)

    # Upload operation.
    if state == "upload":
        document = message.get("document")

        if not document:
            send_message(
                chat_id,
                "❌ Please send a document file.",
                back_keyboard()
            )
            return

        if document.get("file_size", 0) > MAX_DOWNLOAD_SIZE:
            send_message(
                chat_id,
                "❌ File is larger than 20 MB.",
                back_keyboard()
            )
            return

        token = save_file(
            document["file_id"],
            document.get("file_name", "File"),
            document.get("mime_type", ""),
            document.get("file_size", 0)
        )

        clear_state(user_id)

        link = file_link(token)

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "🔗 Open File",
                        "url": link
                    }
                ],
                [
                    {
                        "text": "📨 Share via Telegram",
                        "url": (
                            f"https://t.me/{BOT_USERNAME}"
                            f"?start={token}"
                        )
                    }
                ]
            ]
        }

        send_message(
            chat_id,
            "✅ <b>File Uploaded Successfully</b>\n\n"
            f"📄 Name: {document.get('file_name', 'File')}\n"
            f"📦 Size: {document.get('file_size', 0)} bytes\n\n"
            f"🔗 Link:\n<code>{link}</code>",
            keyboard
        )

        return

    # Start message.
    if state == "startmsg":
        if not text:
            send_message(chat_id, "Please send text.")
            return

        set_setting("start_message", text)
        clear_state(user_id)

        send_message(
            chat_id,
            "✅ Start Message updated.",
            back_keyboard()
        )
        return

    # Star message.
    if state == "starmsg":
        if not text:
            send_message(chat_id, "Please send text.")
            return

        set_setting("star_message", text)
        clear_state(user_id)

        send_message(
            chat_id,
            "✅ Star Message updated.",
            back_keyboard()
        )
        return

    # Add channel.
    if state == "addchannel":
        try:
            parts = [x.strip() for x in text.split("|", 1)]

            if len(parts) != 2:
                raise ValueError("Invalid format")

            channel_id = parts[0]
            channel_link = parts[1]

            if not channel_link.startswith("https://t.me/"):
                raise ValueError("Invalid Telegram link")

            chat = tg("getChat", {"chat_id": channel_id})

            if not chat.get("ok"):
                raise ValueError("Bot cannot access channel")

            channel = chat["result"]

            bot_info = tg("getMe")

            if not bot_info.get("ok"):
                raise ValueError("Cannot verify bot")

            bot_id = bot_info["result"]["id"]

            member = tg("getChatMember", {
                "chat_id": channel_id,
                "user_id": bot_id
            })

            if not member.get("ok"):
                raise ValueError("Bot membership check failed")

            status = member["result"].get("status")

            if status not in ("administrator", "creator"):
                raise ValueError("Bot must be channel administrator")

            db_execute("""
                INSERT INTO channels(
                    chat_id, channel_name, channel_link
                )
                VALUES(%s, %s, %s)
                ON CONFLICT(chat_id)
                DO UPDATE SET
                    channel_name=EXCLUDED.channel_name,
                    channel_link=EXCLUDED.channel_link
            """, (
                channel_id,
                channel.get("title", channel_id),
                channel_link
            ))

            clear_state(user_id)

            send_message(
                chat_id,
                "✅ Channel added successfully.",
                channel_keyboard()
            )

        except Exception as exc:
            log.exception("ADD CHANNEL FAILED")

            send_message(
                chat_id,
                f"❌ Could not add channel.\n\n<code>{str(exc)}</code>",
                back_keyboard()
            )

        return

    # Broadcast operation.
    if state == "broadcast":
        if not text:
            send_message(chat_id, "Please send a text message.")
            return

        clear_state(user_id)

        users = db_execute(
            "SELECT user_id FROM users",
            fetchall=True
        ) or []

        success = 0
        failed = 0

        for item in users:
            result = send_message(
                item["user_id"],
                text,
                parse_mode=None
            )

            if result.get("ok"):
                success += 1
            else:
                failed += 1

            time.sleep(0.05)

        send_message(
            chat_id,
            "📣 <b>Broadcast Finished</b>\n\n"
            f"✅ Sent: {success}\n"
            f"❌ Failed: {failed}",
            back_keyboard()
        )


# =========================================================
# UPDATE PROCESSOR
# =========================================================

def process_update(update):
    if "callback_query" in update:
        handle_callback(update["callback_query"])

    elif "message" in update:
        handle_message(update["message"])

    else:
        log.info("IGNORED UPDATE TYPE")


# =========================================================
# WEBHOOK
# =========================================================

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    ):
        log.warning("WEBHOOK SECRET INVALID")
        return jsonify({"ok": False}), 403

    update = request.get_json(silent=True)

    if not isinstance(update, dict):
        return jsonify({"ok": False}), 400

    update_id = update.get("update_id")

    if update_id is None:
        return jsonify({"ok": False}), 400

    log.info(
        "WEBHOOK RECEIVED | update_id=%s | keys=%s",
        update_id,
        list(update.keys())
    )

    try:
        # Claim update once.
        claimed = db_execute("""
            INSERT INTO processed_updates(update_id)
            VALUES(%s)
            ON CONFLICT(update_id) DO NOTHING
            RETURNING update_id
        """, (update_id,), fetchone=True)

        if not claimed:
            log.info("DUPLICATE UPDATE | %s", update_id)
            return jsonify({"ok": True, "duplicate": True}), 200

        process_update(update)

        log.info("UPDATE PROCESSED | %s", update_id)

        return jsonify({"ok": True}), 200

    except Exception:
        log.exception("UPDATE PROCESSING FAILED | %s", update_id)

        # Allow Telegram to retry failed updates.
        try:
            db_execute(
                "DELETE FROM processed_updates WHERE update_id=%s",
                (update_id,)
            )
        except Exception:
            log.exception("FAILED TO RELEASE UPDATE LOCK")

        return jsonify({"ok": False}), 500


# =========================================================
# FILE ACCESS / DOWNLOAD
# =========================================================

@app.route("/file/<token>")
def file_page(token):
    item = get_file(token)

    if not item:
        return "File not found", 404

    db_execute("""
        UPDATE files SET views=views+1
        WHERE token=%s
    """, (token,))

    if not BOT_USERNAME:
        return "Bot username not configured", 500

    deep_link = f"https://t.me/{BOT_USERNAME}?start={token}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>AMR CLOUD</title>
        <style>
            *{{box-sizing:border-box}}
            body{{
                margin:0;
                min-height:100vh;
                display:flex;
                align-items:center;
                justify-content:center;
                background:#090b10;
                color:#fff;
                font-family:Arial,sans-serif;
                padding:20px;
            }}
            .card{{
                max-width:420px;
                width:100%;
                padding:35px;
                text-align:center;
                border:1px solid #30333d;
                border-radius:22px;
                background:#151821;
            }}
            h1{{font-size:28px}}
            p{{color:#aeb5c4;line-height:1.6}}
            a{{
                display:block;
                padding:15px;
                margin-top:20px;
                border-radius:12px;
                background:#ffffff;
                color:#111;
                text-decoration:none;
                font-weight:bold;
            }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>☁️ AMR CLOUD</h1>
            <p>{item['file_name']}</p>
            <p>Open Telegram to access your file.</p>
            <a href="{deep_link}">OPEN FILE</a>
        </div>
    </body>
    </html>
    """

    return Response(html, mimetype="text/html")


@app.route("/access/<token>")
def access_file(token):
    item = get_file(token)

    if not item:
        return "File not found", 404

    db_execute(
        "UPDATE files SET views=views+1 WHERE token=%s",
        (token,)
    )

    return redirect(
        f"https://t.me/{BOT_USERNAME}?start={token}"
    )


@app.route("/download/<token>")
def download_file(token):
    item = get_file(token)

    if not item:
        return "File not found", 404

    result = tg("getFile", {
        "file_id": item["file_id"]
    })

    if not result.get("ok"):
        return "Unable to retrieve file", 502

    file_path = result["result"]["file_path"]

    url = f"{FILE_API_URL}/{quote(file_path)}"

    try:
        response = http.get(
            url,
            stream=True,
            timeout=60
        )

        if response.status_code != 200:
            return "Download failed", 502

        db_execute(
            "UPDATE files SET downloads=downloads+1 WHERE token=%s",
            (token,)
        )

        def generate():
            for chunk in response.iter_content(64 * 1024):
                if chunk:
                    yield chunk

        filename = item["file_name"].replace('"', "")

        return Response(
            generate(),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": item["mime_type"] or "application/octet-stream"
            }
        )

    except Exception:
        log.exception("FILE DOWNLOAD FAILED")
        return "Download error", 500


# =========================================================
# HEALTH / WEBHOOK SETUP
# =========================================================

@app.route("/")
def home():
    return "AMR CLOUD is running ☁️", 200


@app.route("/health")
def health():
    try:
        db_execute("SELECT 1")
        return jsonify({
            "status": "healthy",
            "database": "connected"
        }), 200

    except Exception:
        log.exception("HEALTH CHECK FAILED")

        return jsonify({
            "status": "error",
            "database": "disconnected"
        }), 500


def check_update_secret():
    supplied = request.args.get("secret", "")

    if not UPDATE_SECRET:
        return False

    return hmac.compare_digest(supplied, UPDATE_SECRET)


@app.route("/setup-webhook")
def setup_webhook():
    if not check_update_secret():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    if not RENDER_EXTERNAL_URL:
        return jsonify({
            "ok": False,
            "error": "RENDER_EXTERNAL_URL missing"
        }), 500

    webhook_url = RENDER_EXTERNAL_URL + WEBHOOK_PATH

    result = tg("setWebhook", {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False
    })

    return jsonify(result)


@app.route("/webhook-info")
def webhook_info():
    if not check_update_secret():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    return jsonify(tg("getWebhookInfo"))


# =========================================================
# STARTUP
# =========================================================

init_db()

log.info("AMR CLOUD BOT INITIALIZED")
