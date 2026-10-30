import os
import re
import uuid
import time
import hmac
import secrets
import logging
import mimetypes
import threading

from html import escape
from urllib.parse import quote

import requests
import psycopg2

from psycopg2.extras import RealDictCursor

from flask import (
    Flask, request, jsonify, abort,
    redirect, make_response, Response,
    render_template_string, stream_with_context
)

from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

# =====================================================
# CONFIG
# =====================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").replace("@", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "").strip()

BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
FLASK_SECRET = os.getenv("FLASK_SECRET") or secrets.token_hex(32)

MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

app = Flask(__name__)
app.secret_key = FLASK_SECRET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logging.info("Starting AMR CLOUD...")


# =====================================================
# DATABASE
# =====================================================

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing")

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=15,
        sslmode="require"
    )


def db_execute(query, params=None, fetchone=False, fetchall=False):
    conn = db()
    cur = conn.cursor()

    try:
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
        cur.close()
        conn.close()


def init_db():
    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id SERIAL PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                file_id TEXT NOT NULL,
                file_name TEXT,
                file_type TEXT,
                file_size BIGINT DEFAULT 0,
                mime_type TEXT,
                views INTEGER DEFAULT 0,
                downloads INTEGER DEFAULT 0,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                channel_link TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_states (
                user_id BIGINT PRIMARY KEY,
                state TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        logging.info("Database initialized successfully.")

    except Exception:
        conn.rollback()
        logging.exception("Database initialization failed")
        raise

    finally:
        cur.close()
        conn.close()


# =====================================================
# TELEGRAM API
# =====================================================

session = requests.Session()


def tg(method, data=None):
    try:
        response = session.post(
            f"{API}/{method}",
            json=data or {},
            timeout=(10, 40)
        )

        try:
            result = response.json()
        except ValueError:
            logging.error(
                "Invalid Telegram response [%s]: HTTP %s",
                method,
                response.status_code
            )
            return {
                "ok": False,
                "description": "Invalid Telegram response"
            }

        if not result.get("ok"):
            logging.error(
                "Telegram API error [%s]: HTTP %s | %s",
                method,
                response.status_code,
                result.get("description")
            )

        return result

    except requests.RequestException:
        logging.exception("Telegram request failed: %s", method)

        return {
            "ok": False,
            "description": "Telegram network request failed"
        }


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": str(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if keyboard is not None:
        data["reply_markup"] = keyboard

    result = tg("sendMessage", data)

    if not result.get("ok"):
        logging.error(
            "SEND MESSAGE FAILED | chat=%s | error=%s",
            chat_id,
            result.get("description")
        )

    return result


def edit_message(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": str(text),
        "parse_mode": "HTML"
    }

    if keyboard is not None:
        data["reply_markup"] = keyboard

    return tg("editMessageText", data)


def answer_callback(callback_id, text=""):
    if not callback_id:
        return {"ok": False}

    return tg("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": str(text)[:200],
        "cache_time": 0
    })


def keyboard(rows):
    return {
        "inline_keyboard": rows
    }


# =====================================================
# DATABASE SETTINGS
# =====================================================

def get_setting(key, default=""):
    result = db_execute(
        "SELECT value FROM settings WHERE key=%s",
        (key,),
        fetchone=True
    )

    return result["value"] if result else default


def set_setting(key, value):
    db_execute("""
        INSERT INTO settings(key,value)
        VALUES(%s,%s)
        ON CONFLICT(key)
        DO UPDATE SET value=EXCLUDED.value
    """, (key, value))


# =====================================================
# ADMIN
# =====================================================

def is_admin(user_id):
    try:
        return int(user_id) == ADMIN_ID and ADMIN_ID != 0
    except (ValueError, TypeError):
        return False


def set_state(user_id, state):
    db_execute("""
        INSERT INTO admin_states(user_id,state)
        VALUES(%s,%s)
        ON CONFLICT(user_id)
        DO UPDATE SET
            state=EXCLUDED.state,
            updated_at=CURRENT_TIMESTAMP
    """, (user_id, state))


def get_state(user_id):
    result = db_execute(
        "SELECT state FROM admin_states WHERE user_id=%s",
        (user_id,),
        fetchone=True
    )

    return result["state"] if result else None


def clear_state(user_id):
    db_execute(
        "DELETE FROM admin_states WHERE user_id=%s",
        (user_id,)
    )


def admin_panel(chat_id):
    rows = [
        [
            {"text": "📤 Upload File", "callback_data": "upload"},
            {"text": "📁 Uploaded Files", "callback_data": "files"}
        ],
        [
            {"text": "✏️ Start Message", "callback_data": "startmsg"},
            {"text": "⭐ Star Message", "callback_data": "starmsg"}
        ],
        [
            {"text": "🔒 Force Join", "callback_data": "channels"},
            {"text": "📊 Bot Status", "callback_data": "status"}
        ],
        [
            {"text": "📢 Broadcast", "callback_data": "broadcast"}
        ],
        [
            {"text": "🔄 Refresh", "callback_data": "admin"}
        ]
    ]

    result = send_message(
        chat_id,
        "<b>⚡ AMR CLOUD ADMIN PANEL</b>\n\nSelect an option:",
        keyboard(rows)
    )

    if result.get("ok"):
        logging.info("ADMIN PANEL SENT | chat=%s", chat_id)
    else:
        logging.error("ADMIN PANEL FAILED: %s", result)


# =====================================================
# USERS
# =====================================================

def save_user(user):
    db_execute("""
        INSERT INTO users(user_id,username,first_name)
        VALUES(%s,%s,%s)
        ON CONFLICT(user_id)
        DO UPDATE SET
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name
    """, (
        user["id"],
        user.get("username"),
        user.get("first_name")
    ))


# =====================================================
# FORCE JOIN
# =====================================================

def get_channels():
    return db_execute(
        "SELECT chat_id,channel_link FROM channels ORDER BY chat_id",
        fetchall=True
    )


def check_force_join(user_id):
    missing = []

    for channel in get_channels():
        result = tg("getChatMember", {
            "chat_id": channel["chat_id"],
            "user_id": user_id
        })

        if not result.get("ok"):
            logging.warning(
                "FORCE JOIN CHECK FAILED | channel=%s | %s",
                channel["chat_id"],
                result.get("description")
            )
            missing.append(channel)
            continue

        member = result["result"]
        status = member.get("status")

        joined = (
            status in ("member", "administrator", "creator")
            or (
                status == "restricted"
                and member.get("is_member", False)
            )
        )

        if not joined:
            missing.append(channel)

    return missing


# =====================================================
# FILE HELPERS
# =====================================================

def format_size(size):
    size = int(size or 0)

    if size < 1024:
        return f"{size} B"

    if size < 1024 ** 2:
        return f"{size / 1024:.2f} KB"

    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.2f} MB"

    return f"{size / (1024 ** 3):.2f} GB"


def get_media(message):
    media_types = [
        "document", "photo", "video",
        "audio", "voice", "animation", "video_note"
    ]

    for media_type in media_types:
        if media_type not in message:
            continue

        media = message[media_type]

        if media_type == "photo":
            media = media[-1]

        original_name = media.get("file_name")

        if not original_name:
            original_name = (
                f"{media_type}_{uuid.uuid4().hex[:8]}"
            )

        file_name = secure_filename(original_name) or "download"

        mime_type = (
            media.get("mime_type")
            or mimetypes.guess_type(file_name)[0]
            or "application/octet-stream"
        )

        return {
            "file_id": media["file_id"],
            "file_name": file_name,
            "file_type": media_type,
            "file_size": media.get("file_size", 0),
            "mime_type": mime_type
        }

    return None


def save_file(media):
    token = secrets.token_urlsafe(18)

    db_execute("""
        INSERT INTO files(
            token,file_id,file_name,file_type,file_size,mime_type
        )
        VALUES(%s,%s,%s,%s,%s,%s)
    """, (
        token,
        media["file_id"],
        media["file_name"],
        media["file_type"],
        media["file_size"],
        media["mime_type"]
    ))

    return token


def get_file(token):
    return db_execute(
        "SELECT * FROM files WHERE token=%s",
        (token,),
        fetchone=True
    )


def delete_file(token):
    result = db_execute(
        "DELETE FROM files WHERE token=%s RETURNING token",
        (token,),
        fetchone=True
    )

    return bool(result)


def show_files(chat_id):
    files = db_execute("""
        SELECT token,file_name,file_type,file_size
        FROM files
        ORDER BY id DESC
        LIMIT 30
    """, fetchall=True)

    if not files:
        send_message(chat_id, "📁 No files uploaded yet.")
        return

    send_message(chat_id, "<b>📁 RECENT UPLOADED FILES</b>")

    for file in files:
        token = file["token"]

        send_message(
            chat_id,
            f"<b>{escape(file['file_name'] or 'Unnamed')}</b>\n"
            f"Type: {escape(file['file_type'] or 'Unknown')}\n"
            f"Size: {format_size(file['file_size'])}",
            keyboard([[
                {
                    "text": "❌ Delete File",
                    "callback_data": "delete_" + token
                }
            ]])
        )


# =====================================================
# STATISTICS
# =====================================================

def show_status(chat_id):
    users = db_execute(
        "SELECT COUNT(*) AS n FROM users",
        fetchone=True
    )["n"]

    files = db_execute(
        "SELECT COUNT(*) AS n FROM files",
        fetchone=True
    )["n"]

    views = db_execute(
        "SELECT COALESCE(SUM(views),0) AS n FROM files",
        fetchone=True
    )["n"]

    downloads = db_execute(
        "SELECT COALESCE(SUM(downloads),0) AS n FROM files",
        fetchone=True
    )["n"]

    channels = db_execute(
        "SELECT COUNT(*) AS n FROM channels",
        fetchone=True
    )["n"]

    send_message(
        chat_id,
        "<b>📊 AMR CLOUD STATUS</b>\n\n"
        f"👥 Users: {users}\n"
        f"📁 Files: {files}\n"
        f"👁 Views: {views}\n"
        f"⬇️ Downloads: {downloads}\n"
        f"🔒 Force Join Channels: {channels}"
    )


# =====================================================
# BROADCAST
# =====================================================

def broadcast(text):
    users = db_execute(
        "SELECT user_id FROM users",
        fetchall=True
    )

    sent = 0
    failed = 0

    for user in users:
        result = send_message(user["user_id"], text)

        if result.get("ok"):
            sent += 1
        else:
            failed += 1

        time.sleep(0.05)

    return sent, failed


# =====================================================
# START
# =====================================================

def handle_start(message):
    user = message["from"]
    chat_id = message["chat"]["id"]

    save_user(user)

    parts = message.get("text", "").split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""

    if is_admin(user["id"]) and not payload:
        clear_state(user["id"])
        admin_panel(chat_id)
        return

    missing = check_force_join(user["id"])

    if missing:
        rows = []

        for channel in missing:
            link = channel["channel_link"]

            if link.startswith("https://t.me/"):
                rows.append([{
                    "text": "📢 Join Channel",
                    "url": link
                }])

        rows.append([{
            "text": "✅ Joined - Verify",
            "callback_data": "verify"
        }])

        send_message(
            chat_id,
            "🔒 <b>Please join all required channels first.</b>",
            keyboard(rows)
        )
        return

    if payload.startswith("f_"):
        token = payload[2:]
        file = get_file(token)

        if not file:
            send_message(chat_id, "❌ File not found or deleted.")
            return

        url = f"{BASE_URL}/file/{token}"

        send_message(
            chat_id,
            "✅ <b>FILE AVAILABLE</b>\n\n"
            f"📁 {escape(file['file_name'] or 'File')}\n"
            f"📦 {format_size(file['file_size'])}\n\n"
            "Open the secure file page:",
            keyboard([[
                {"text": "🌐 OPEN FILE", "url": url}
            ]])
        )
        return

    custom = get_setting(
        "start_message",
        "👋 Welcome to <b>AMR CLOUD</b>!\n\nSend /help to see available commands."
    )

    send_message(chat_id, custom)


def handle_help(chat_id):
    send_message(
        chat_id,
        "<b>☁️ AMR CLOUD HELP</b>\n\n"
        "/start - Start the bot\n"
        "/help - Show help\n"
        "/star - Support message\n"
        "/cancel - Cancel current action"
    )


def handle_star(chat_id):
    send_message(
        chat_id,
        get_setting(
            "star_message",
            "⭐ Thank you for supporting AMR CLOUD!"
        )
    )


# =====================================================
# MESSAGE HANDLER
# =====================================================

def handle_message(message):
    user = message.get("from", {})
    chat_id = message["chat"]["id"]

    if not user:
        return

    user_id = user["id"]
    text = message.get("text", "")

    logging.info(
        "MESSAGE RECEIVED | user=%s | chat=%s | text=%s",
        user_id,
        chat_id,
        text[:100]
    )

    if text.startswith("/start"):
        handle_start(message)
        return

    if text.startswith("/help"):
        handle_help(chat_id)
        return

    if text.startswith("/star"):
        handle_star(chat_id)
        return

    if text.startswith("/cancel"):
        clear_state(user_id)
        send_message(chat_id, "✅ Current action cancelled.")
        return

    if text.startswith("/admin"):
        if is_admin(user_id):
            clear_state(user_id)
            admin_panel(chat_id)
        else:
            send_message(chat_id, "⛔ Access denied.")
        return

    if text.startswith("/stats"):
        if is_admin(user_id):
            show_status(chat_id)
        else:
            send_message(chat_id, "⛔ Access denied.")
        return

    if is_admin(user_id):
        state = get_state(user_id)

        if state in ("startmsg", "starmsg"):
            if not text:
                send_message(chat_id, "Please send a text message.")
                return

            key = "start_message" if state == "startmsg" else "star_message"

            set_setting(key, text)
            clear_state(user_id)

            send_message(chat_id, "✅ Message updated.")
            return

        if state == "addchannel":
            parts = text.split()

            if len(parts) < 2:
                send_message(
                    chat_id,
                    "Format:\n<code>@channelusername https://t.me/channel</code>"
                )
                return

            channel_id = parts[0]
            channel_link = parts[1]

            if not channel_link.startswith("https://t.me/"):
                send_message(chat_id, "❌ Invalid Telegram channel link.")
                return

            result = tg("getChat", {"chat_id": channel_id})

            if not result.get("ok"):
                send_message(chat_id, "❌ Channel not found.")
                return

            db_execute("""
                INSERT INTO channels(chat_id,channel_link)
                VALUES(%s,%s)
                ON CONFLICT(chat_id)
                DO UPDATE SET channel_link=EXCLUDED.channel_link
            """, (str(result["result"]["id"]), channel_link))

            clear_state(user_id)
            send_message(chat_id, "✅ Channel configured successfully.")
            return

        if state == "broadcast":
            if not text:
                send_message(chat_id, "Please send broadcast text.")
                return

            clear_state(user_id)
            send_message(chat_id, "⏳ Broadcast started...")

            sent, failed = broadcast(text)

            send_message(
                chat_id,
                f"✅ <b>Broadcast completed</b>\n\nDelivered: {sent}\nFailed: {failed}"
            )
            return

        if state == "upload":
            media = get_media(message)

            if not media:
                send_message(
                    chat_id,
                    "❌ Unsupported file. Send a document, photo, video, audio, voice or animation."
                )
                return

            token = save_file(media)
            clear_state(user_id)

            link = f"https://t.me/{BOT_USERNAME}?start=f_{token}"

            send_message(
                chat_id,
                "✅ <b>FILE UPLOADED</b>\n\n"
                f"📁 {escape(media['file_name'])}\n"
                f"📦 {format_size(media['file_size'])}\n\n"
                "Your file link:\n"
                f"<code>{escape(link)}</code>"
            )
            return

    if get_media(message):
        send_message(chat_id, "Please use the file links provided by AMR CLOUD.")


# =====================================================
# CALLBACK HANDLER
# =====================================================

def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    user_id = callback.get("from", {}).get("id")

    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")

    logging.info(
        "CALLBACK RECEIVED | data=%s | user=%s | chat=%s",
        data,
        user_id,
        chat_id
    )

    # Answer Telegram callback immediately.
    answer_callback(callback_id)

    if not chat_id or not user_id:
        logging.warning("Callback missing chat or user.")
        return

    if data == "verify":
        missing = check_force_join(user_id)

        if missing:
            send_message(chat_id, "❌ You have not joined all required channels.")
        else:
            send_message(chat_id, "✅ Verification successful. Send /start again.")
        return

    if not is_admin(user_id):
        logging.warning("Unauthorized callback | user=%s", user_id)
        return

    logging.info("ADMIN CALLBACK ACCEPTED | %s", data)

    if data == "admin":
        clear_state(user_id)
        admin_panel(chat_id)
        return

    if data == "upload":
        set_state(user_id, "upload")
        send_message(
            chat_id,
            "📤 <b>UPLOAD FILE</b>\n\nSend your file now.\n\nUse /cancel to cancel."
        )
        return

    if data == "files":
        show_files(chat_id)
        return

    if data.startswith("delete_"):
        token = data[len("delete_"):]
        deleted = delete_file(token)

        send_message(
            chat_id,
            "✅ File deleted." if deleted else "❌ File not found."
        )
        return

    if data == "startmsg":
        set_state(user_id, "startmsg")
        send_message(chat_id, "✏️ Send your new START message.\n\nUse /cancel to cancel.")
        return

    if data == "starmsg":
        set_state(user_id, "starmsg")
        send_message(chat_id, "⭐ Send your new STAR message.\n\nUse /cancel to cancel.")
        return

    if data == "channels":
        send_message(
            chat_id,
            "<b>🔒 FORCE JOIN SETTINGS</b>",
            keyboard([
                [{"text": "➕ Add Channel", "callback_data": "addchannel"}],
                [{"text": "📋 Channel List", "callback_data": "channellist"}],
                [{"text": "❌ Remove Channel", "callback_data": "removechannel"}],
                [{"text": "⬅️ Back", "callback_data": "admin"}]
            ])
        )
        return

    if data == "addchannel":
        set_state(user_id, "addchannel")
        send_message(
            chat_id,
            "Send channel username and invite link:\n\n"
            "<code>@channelusername https://t.me/channel</code>\n\n"
            "The bot must be an administrator in the channel."
        )
        return

    if data == "channellist":
        channels = get_channels()

        if not channels:
            send_message(chat_id, "No channels configured.")
            return

        for channel in channels:
            send_message(
                chat_id,
                f"Channel ID: <code>{escape(channel['chat_id'])}</code>\n"
                f"Link: {escape(channel['channel_link'])}"
            )
        return

    if data == "removechannel":
        channels = get_channels()

        if not channels:
            send_message(chat_id, "No channels configured.")
            return

        for channel in channels:
            channel_id = channel["chat_id"]

            send_message(
                chat_id,
                f"Channel: <code>{escape(channel_id)}</code>",
                keyboard([[
                    {
                        "text": "❌ Remove",
                        "callback_data": "rmch_" + channel_id
                    }
                ]])
            )
        return

    if data.startswith("rmch_"):
        channel_id = data[len("rmch_"):]

        db_execute(
            "DELETE FROM channels WHERE chat_id=%s",
            (channel_id,)
        )

        send_message(chat_id, "✅ Channel removed.")
        return

    if data == "status":
        show_status(chat_id)
        return

    if data == "broadcast":
        set_state(user_id, "broadcast")
        send_message(
            chat_id,
            "📢 Send broadcast text.\n\nUse /cancel to cancel."
        )
        return

    logging.warning("UNKNOWN CALLBACK DATA: %s", data)


# =====================================================
# WEBHOOK
# =====================================================

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():

    if WEBHOOK_SECRET:
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            logging.warning("WEBHOOK REJECTED: Invalid secret.")
            abort(403)

    update = request.get_json(silent=True)

    if not isinstance(update, dict):
        logging.warning("WEBHOOK RECEIVED INVALID JSON")
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    update_id = update.get("update_id")

    logging.info(
        "WEBHOOK RECEIVED | update_id=%s | keys=%s",
        update_id,
        list(update.keys())
    )

    try:
        if update_id is not None:
            # Atomic duplicate check.
            claimed = db_execute("""
                INSERT INTO processed_updates(update_id)
                VALUES(%s)
                ON CONFLICT DO NOTHING
                RETURNING update_id
            """, (update_id,), fetchone=True)

            if not claimed:
                logging.info("DUPLICATE UPDATE IGNORED: %s", update_id)
                return jsonify({"ok": True, "duplicate": True})

        if "callback_query" in update:
            handle_callback(update["callback_query"])

        elif "message" in update:
            handle_message(update["message"])

        else:
            logging.info("UNHANDLED UPDATE TYPE: %s", list(update.keys()))

        logging.info("UPDATE PROCESSED SUCCESSFULLY: %s", update_id)

        return jsonify({"ok": True})

    except Exception:
        logging.exception("WEBHOOK PROCESSING FAILED: %s", update_id)

        # Allow Telegram to retry a failed update.
        if update_id is not None:
            try:
                db_execute(
                    "DELETE FROM processed_updates WHERE update_id=%s",
                    (update_id,)
                )
            except Exception:
                logging.exception("Could not release failed update claim")

        return jsonify({"ok": False}), 500


# =====================================================
# HOME PAGE
# =====================================================

@app.route("/")
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>AMR CLOUD</title>
    <style>
    *{box-sizing:border-box}
    body{
        margin:0;min-height:100vh;display:flex;align-items:center;
        justify-content:center;font-family:Arial;color:white;
        background:radial-gradient(circle at 10% 10%,#29205c,transparent 40%),
        radial-gradient(circle at 90% 90%,#174a57,transparent 40%),#080b14
    }
    .card{
        width:90%;max-width:440px;padding:35px 25px;
        border:1px solid #ffffff24;border-radius:25px;text-align:center;
        background:#ffffff12;backdrop-filter:blur(20px);
        box-shadow:0 20px 70px #0007
    }
    h1{font-size:36px}
    p{color:#c5c8dc;line-height:1.6}
    .status{
        display:inline-block;margin-top:15px;padding:10px 17px;
        border-radius:30px;background:#123e30;color:#80ffba
    }
    </style>
    </head>
    <body>
    <div class="card">
        <h1>☁️ AMR CLOUD</h1>
        <p>Secure File Sharing Platform</p>
        <div class="status">● Service Online</div>
        <p>Fast • Simple • Secure</p>
    </div>
    </body>
    </html>
    """)


# =====================================================
# HEALTH
# =====================================================

@app.route("/health")
def health():
    try:
        db_execute("SELECT 1", fetchone=True)

        return jsonify({
            "status": "online",
            "database": "connected",
            "service": "AMR CLOUD"
        })

    except Exception:
        logging.exception("Health check failed")

        return jsonify({
            "status": "error",
            "database": "disconnected"
        }), 503


# =====================================================
# FILE PAGE
# =====================================================

@app.route("/file/<token>")
def file_page(token):
    file = get_file(token)

    if not file:
        abort(404)

    db_execute(
        "UPDATE files SET views=views+1 WHERE token=%s",
        (token,)
    )

    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ filename }} - AMR CLOUD</title>
    <style>
    *{box-sizing:border-box}
    body{
        margin:0;min-height:100vh;padding:25px 15px;
        display:flex;align-items:center;justify-content:center;
        font-family:Arial;color:white;
        background:radial-gradient(circle at 10% 10%,#29205c,transparent 40%),#080b14
    }
    .card{
        width:100%;max-width:450px;padding:27px;border-radius:25px;
        background:#ffffff10;border:1px solid #ffffff25;
        box-shadow:0 20px 60px #0008;backdrop-filter:blur(20px)
    }
    h1{margin-top:0}
    .muted{color:#aeb4c8;overflow-wrap:anywhere}
    .ad{
        margin:18px 0;padding:22px 10px;border:1px dashed #68708b;
        border-radius:13px;text-align:center;color:#aeb4c8;font-size:12px
    }
    button{
        width:100%;border:0;border-radius:12px;padding:16px;
        margin-top:12px;font-weight:bold;font-size:14px;
        cursor:pointer;color:white;background:#6655e8
    }
    .download{background:#168c67}
    .note{font-size:12px;color:#aeb4c8;line-height:1.5}
    </style>
    </head>
    <body>
    <div class="card">
        <h1>☁️ AMR CLOUD</h1>
        <p class="muted">Secure File Sharing</p>
        <hr style="border-color:#ffffff22">
        <h3>📁 {{ filename }}</h3>
        <p class="muted">File size: {{ size }}</p>

        <div class="ad">ADVERTISEMENT</div>

        <form method="POST" action="/access/{{ token }}">
            <button type="submit">ACCESS FILE</button>
        </form>

        <p class="note">
        Advertisements are separate from file access.
        Viewing an advertisement is not automatically verified.
        </p>

        <div class="ad">ADVERTISEMENT</div>

        <form method="POST" action="/download/{{ token }}">
            <button class="download" type="submit">DOWNLOAD FILE</button>
        </form>
    </div>
    </body>
    </html>
    """,
        filename=file["file_name"] or "File",
        size=format_size(file["file_size"]),
        token=token
    )


# =====================================================
# ACCESS
# =====================================================

@app.route("/access/<token>", methods=["POST"])
def access_file(token):
    file = get_file(token)

    if not file:
        abort(404)

    response = make_response(redirect(f"/file/{token}"))

    response.set_cookie(
        f"amr_access_{token}",
        "authorized",
        max_age=3600,
        httponly=True,
        secure=True,
        samesite="Lax"
    )

    return response


# =====================================================
# DOWNLOAD
# =====================================================

@app.route("/download/<token>", methods=["POST"])
def download_file(token):
    file = get_file(token)

    if not file:
        abort(404)

    if request.cookies.get(f"amr_access_{token}") != "authorized":
        return redirect(f"/file/{token}")

    result = tg("getFile", {"file_id": file["file_id"]})

    if not result.get("ok"):
        return "Telegram file unavailable.", 502

    file_path = result["result"].get("file_path")

    if not file_path:
        return "File path unavailable.", 502

    file_size = int(result["result"].get("file_size", 0))

    if file_size > MAX_DOWNLOAD_SIZE:
        return "File exceeds Telegram hosted Bot API download limit of 20 MB.", 413

    download_url = f"{FILE_API}/{file_path}"

    try:
        r = session.get(
            download_url,
            stream=True,
            timeout=(15, 60)
        )

        if r.status_code != 200:
            r.close()
            return "Download failed.", 502

        filename = secure_filename(file["file_name"] or "download") or "download"

        def generate():
            downloaded = 0

            try:
                for chunk in r.iter_content(64 * 1024):
                    if not chunk:
                        continue

                    downloaded += len(chunk)

                    if downloaded > MAX_DOWNLOAD_SIZE:
                        logging.warning("Download size limit exceeded")
                        break

                    yield chunk

            finally:
                r.close()

        db_execute(
            "UPDATE files SET downloads=downloads+1 WHERE token=%s",
            (token,)
        )

        return Response(
            stream_with_context(generate()),
            content_type=file["mime_type"] or "application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                "X-Content-Type-Options": "nosniff"
            }
        )

    except Exception:
        logging.exception("Download error")
        return "Download error.", 500


# =====================================================
# WEBHOOK SETUP
# =====================================================

@app.route("/setup-webhook", methods=["GET", "POST"])
def setup_webhook():
    secret = request.args.get("secret", "")

    if not UPDATE_SECRET or not secrets.compare_digest(secret, UPDATE_SECRET):
        abort(403)

    if not BASE_URL:
        return jsonify({
            "ok": False,
            "error": "RENDER_EXTERNAL_URL missing"
        }), 500

    webhook_data = {
        "url": f"{BASE_URL}/telegram-webhook",
        "drop_pending_updates": False,
        "allowed_updates": ["message", "callback_query"]
    }

    if WEBHOOK_SECRET:
        webhook_data["secret_token"] = WEBHOOK_SECRET

    result = tg("setWebhook", webhook_data)

    logging.info(
        "WEBHOOK SETUP RESULT: %s",
        result
    )

    return jsonify(result)


@app.route("/webhook-info")
def webhook_info():
    secret = request.args.get("secret", "")

    if not UPDATE_SECRET or not secrets.compare_digest(secret, UPDATE_SECRET):
        abort(403)

    result = tg("getWebhookInfo")

    if result.get("ok"):
        info = result.get("result", {})

        logging.info(
            "WEBHOOK INFO | url=%s | pending=%s | last_error=%s",
            info.get("url"),
            info.get("pending_update_count"),
            info.get("last_error_message")
        )

    return jsonify(result)


# =====================================================
# STARTUP
# =====================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME missing")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID missing")

if not BASE_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing")

if not UPDATE_SECRET:
    raise RuntimeError("UPDATE_SECRET missing")

init_db()

logging.info("AMR CLOUD is ready.")


# =====================================================
# LOCAL DEVELOPMENT
# =====================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000"))
                )
