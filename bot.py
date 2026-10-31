import os
import re
import time
import json
import uuid
import html
import logging
import secrets
import mimetypes
import threading
from functools import wraps
from urllib.parse import quote

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import (
    Flask, request, jsonify, redirect,
    make_response, render_template_string, abort
)
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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is missing.")

if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME environment variable is missing.")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", secrets.token_hex(32))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("AMR_CLOUD")

db_lock = threading.RLock()


# =========================================================
# DATABASE
# =========================================================

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=15,
        cursor_factory=RealDictCursor
    )


def db_execute(query, params=None, fetchone=False, fetchall=False):
    with db_lock:
        conn = get_db()

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
    queries = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
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
        """,

        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS channels (
            id SERIAL PRIMARY KEY,
            chat_id TEXT NOT NULL,
            channel_name TEXT,
            channel_link TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS processed_updates (
            update_id BIGINT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,

        """
        CREATE TABLE IF NOT EXISTS admin_states (
            admin_id BIGINT PRIMARY KEY,
            state TEXT,
            data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    ]

    for query in queries:
        db_execute(query)

    defaults = {
        "start_message": (
            "👋 Welcome to AMR CLOUD!\n\n"
            "Send /start to use the bot."
        ),
        "star_message": (
            "⭐ Please support AMR CLOUD."
        )
    }

    for key, value in defaults.items():
        db_execute(
            """
            INSERT INTO settings(key, value)
            VALUES (%s, %s)
            ON CONFLICT(key) DO NOTHING
            """,
            (key, value)
        )

    log.info("DATABASE INITIALIZED SUCCESSFULLY")


# =========================================================
# TELEGRAM API
# =========================================================

def tg(method, data=None, files=None, timeout=40):
    try:
        response = requests.post(
            f"{API_URL}/{method}",
            data=data,
            files=files,
            timeout=timeout
        )

        try:
            result = response.json()
        except Exception:
            log.error("Telegram returned invalid JSON: %s", method)
            return {"ok": False, "description": "Invalid Telegram response"}

        if not result.get("ok"):
            log.error(
                "TELEGRAM API ERROR | method=%s | description=%s",
                method,
                result.get("description")
            )

        return result

    except requests.RequestException as exc:
        log.exception("TELEGRAM REQUEST FAILED | %s", method)
        return {"ok": False, "description": str(exc)}


def send_message(chat_id, text, keyboard=None, parse_mode=None):
    data = {
        "chat_id": chat_id,
        "text": str(text)
    }

    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)

    if parse_mode:
        data["parse_mode"] = parse_mode

    return tg("sendMessage", data)


def edit_message(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": str(text)
    }

    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)

    return tg("editMessageText", data)


def answer_callback(callback_id, text=""):
    result = tg(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text[:180],
            "show_alert": False
        }
    )

    return result.get("ok", False)


def get_setting(key, default=""):
    row = db_execute(
        "SELECT value FROM settings WHERE key=%s",
        (key,),
        fetchone=True
    )

    return row["value"] if row else default


def set_setting(key, value):
    db_execute(
        """
        INSERT INTO settings(key, value)
        VALUES (%s, %s)
        ON CONFLICT(key)
        DO UPDATE SET value=EXCLUDED.value
        """,
        (key, value)
    )


# =========================================================
# ADMIN STATE
# =========================================================

def set_state(admin_id, state, data=None):
    db_execute(
        """
        INSERT INTO admin_states(admin_id, state, data)
        VALUES (%s, %s, %s)
        ON CONFLICT(admin_id)
        DO UPDATE SET
            state=EXCLUDED.state,
            data=EXCLUDED.data,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            admin_id,
            state,
            json.dumps(data or {})
        )
    )

    check = get_state(admin_id)
    return check and check["state"] == state


def get_state(admin_id):
    return db_execute(
        """
        SELECT state, data
        FROM admin_states
        WHERE admin_id=%s
        """,
        (admin_id,),
        fetchone=True
    )


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
                {
                    "text": "📤 Upload File",
                    "callback_data": "upload"
                },
                {
                    "text": "📁 Uploaded Files",
                    "callback_data": "files"
                }
            ],
            [
                {
                    "text": "📝 Start Message",
                    "callback_data": "startmsg"
                },
                {
                    "text": "⭐ Star Message",
                    "callback_data": "starmsg"
                }
            ],
            [
                {
                    "text": "🔒 Force Join",
                    "callback_data": "channels"
                },
                {
                    "text": "📊 Bot Status",
                    "callback_data": "status"
                }
            ],
            [
                {
                    "text": "📢 Broadcast",
                    "callback_data": "broadcast"
                },
                {
                    "text": "🔄 Refresh",
                    "callback_data": "admin"
                }
            ]
        ]
    }


def channel_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "➕ Add Channel",
                    "callback_data": "addchannel"
                }
            ],
            [
                {
                    "text": "📋 Channel List",
                    "callback_data": "listchannels"
                }
            ],
            [
                {
                    "text": "⬅️ Back",
                    "callback_data": "admin"
                }
            ]
        ]
    }


def back_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "⬅️ Admin Panel",
                    "callback_data": "admin"
                }
            ]
        ]
    }


# =========================================================
# ADMIN PANEL
# =========================================================

def is_admin(user_id):
    return int(user_id) == ADMIN_ID


def admin_panel(chat_id):
    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       ☁️ AMR CLOUD\n"
        "       ADMIN PANEL\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Welcome, Admin!\n\n"
        "Select an option below:"
    )

    return send_message(
        chat_id,
        text,
        keyboard=admin_keyboard()
    )


# =========================================================
# USER MANAGEMENT
# =========================================================

def save_user(user):
    db_execute(
        """
        INSERT INTO users(user_id, first_name, username)
        VALUES (%s, %s, %s)
        ON CONFLICT(user_id)
        DO UPDATE SET
            first_name=EXCLUDED.first_name,
            username=EXCLUDED.username
        """,
        (
            user.get("id"),
            user.get("first_name", ""),
            user.get("username", "")
        )
    )


# =========================================================
# FILE HANDLING
# =========================================================

def get_media(message):
    media_types = [
        "document",
        "video",
        "audio",
        "animation",
        "voice",
        "video_note"
    ]

    for media_type in media_types:
        media = message.get(media_type)

        if media:
            file_name = media.get("file_name")

            if not file_name:
                extension = mimetypes.guess_extension(
                    media.get("mime_type", "")
                ) or ""

                file_name = f"{media_type}_{int(time.time())}{extension}"

            return {
                "file_id": media.get("file_id"),
                "file_name": file_name,
                "mime_type": media.get("mime_type", ""),
                "file_size": media.get("file_size", 0)
            }

    if message.get("photo"):
        photo = message["photo"][-1]

        return {
            "file_id": photo.get("file_id"),
            "file_name": f"photo_{int(time.time())}.jpg",
            "mime_type": "image/jpeg",
            "file_size": photo.get("file_size", 0)
        }

    return None


def save_file(media):
    token = secrets.token_urlsafe(18)

    db_execute(
        """
        INSERT INTO files(
            token, file_id, file_name,
            mime_type, file_size
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            token,
            media["file_id"],
            media["file_name"],
            media["mime_type"],
            media["file_size"]
        )
    )

    return token


def get_file(token):
    return db_execute(
        "SELECT * FROM files WHERE token=%s",
        (token,),
        fetchone=True
    )


def file_link(token):
    return f"https://t.me/{BOT_USERNAME}?start=f_{token}"


# =========================================================
# FORCE JOIN
# =========================================================

def get_channels():
    return db_execute(
        "SELECT * FROM channels ORDER BY id DESC",
        fetchall=True
    )


def check_join(user_id):
    channels = get_channels()

    for channel in channels:
        result = tg(
            "getChatMember",
            {
                "chat_id": channel["chat_id"],
                "user_id": user_id
            }
        )

        if not result.get("ok"):
            return False

        status = result["result"].get("status")

        if status not in ("member", "administrator", "creator"):
            return False

    return True


def join_keyboard():
    channels = get_channels()
    rows = []

    for channel in channels:
        link = channel.get("channel_link")

        if link:
            rows.append([
                {
                    "text": "Join " + (
                        channel.get("channel_name") or "Channel"
                    ),
                    "url": link
                }
            ])

    rows.append([
        {
            "text": "✅ Verify Join",
            "callback_data": "verifyjoin"
        }
    ])

    return {"inline_keyboard": rows}


def send_force_join(chat_id):
    send_message(
        chat_id,
        "🔒 To use AMR CLOUD, please join the required channels first.",
        keyboard=join_keyboard()
    )


# =========================================================
# FILE PAGE
# =========================================================

FILE_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMR CLOUD</title>
<style>
*{box-sizing:border-box}
body{
    margin:0;
    background:#090d18;
    color:white;
    font-family:Arial,sans-serif;
    display:flex;
    align-items:center;
    justify-content:center;
    min-height:100vh;
    padding:20px
}
.card{
    width:100%;
    max-width:420px;
    padding:30px;
    border:1px solid #26334c;
    border-radius:22px;
    background:#111a2b;
    text-align:center
}
.logo{font-size:38px}
h2{margin:15px 0}
.name{
    padding:15px;
    background:#0a1221;
    border-radius:12px;
    overflow-wrap:anywhere
}
a{
    display:block;
    padding:15px;
    margin-top:15px;
    border-radius:12px;
    text-decoration:none;
    color:white;
    background:#2864dc;
    font-weight:bold
}
.small{color:#9eacc4;font-size:13px;margin-top:20px}
</style>
</head>
<body>
<div class="card">
<div class="logo">☁️</div>
<h2>AMR CLOUD</h2>
<p>Your file is ready.</p>
<div class="name">{{ filename }}</div>
{% if allowed %}
<a href="/download/{{ token }}">⬇ DOWNLOAD FILE</a>
{% else %}
<a href="/access/{{ token }}">🔓 ACCESS FILE</a>
{% endif %}
<div class="small">Secure file sharing powered by AMR CLOUD</div>
</div>
</body>
</html>
"""


@app.route("/file/<token>")
def file_page(token):
    file = get_file(token)

    if not file:
        return "File not found", 404

    allowed = request.cookies.get("amr_access") == token

    db_execute(
        "UPDATE files SET views=views+1 WHERE token=%s",
        (token,)
    )

    return render_template_string(
        FILE_PAGE,
        filename=file["file_name"],
        token=token,
        allowed=allowed
    )


@app.route("/access/<token>")
def access_file(token):
    file = get_file(token)

    if not file:
        return "File not found", 404

    response = make_response(
        redirect(f"/file/{quote(token)}")
    )

    response.set_cookie(
        "amr_access",
        token,
        max_age=3600,
        httponly=True,
        secure=True,
        samesite="Lax"
    )

    return response


@app.route("/download/<token>")
def download_file(token):
    file = get_file(token)

    if not file:
        return "File not found", 404

    if request.cookies.get("amr_access") != token:
        return redirect(f"/file/{quote(token)}")

    if file["file_size"] > MAX_DOWNLOAD_SIZE:
        return "File exceeds the supported download limit.", 413

    result = tg(
        "getFile",
        {"file_id": file["file_id"]}
    )

    if not result.get("ok"):
        return "Unable to retrieve file from Telegram.", 502

    file_path = result["result"]["file_path"]

    try:
        response = requests.get(
            f"{FILE_API_URL}/{file_path}",
            stream=True,
            timeout=(15, 90)
        )

        if response.status_code != 200:
            return "Download failed.", 502

        db_execute(
            "UPDATE files SET downloads=downloads+1 WHERE token=%s",
            (token,)
        )

        filename = file["file_name"] or "download"

        from flask import Response

        def generate():
            for chunk in response.iter_content(1024 * 64):
                if chunk:
                    yield chunk

        return Response(
            generate(),
            content_type=file["mime_type"] or "application/octet-stream",
            headers={
                "Content-Disposition":
                    f"attachment; filename*=UTF-8''{quote(filename)}"
            }
        )

    except requests.RequestException:
        log.exception("FILE DOWNLOAD ERROR")
        return "Download failed.", 502


# =========================================================
# ADMIN ACTIONS
# =========================================================

def show_files(chat_id):
    files = db_execute(
        """
        SELECT * FROM files
        ORDER BY uploaded_at DESC
        LIMIT 30
        """,
        fetchall=True
    )

    if not files:
        return send_message(
            chat_id,
            "📁 No files uploaded yet.",
            keyboard=back_keyboard()
        )

    for file in files:
        text = (
            f"📄 {file['file_name']}\n"
            f"🆔 Token: {file['token']}\n"
            f"⬇ Downloads: {file['downloads']}\n"
            f"👁 Views: {file['views']}"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "🔗 Get Link",
                        "url": file_link(file["token"])
                    },
                    {
                        "text": "🗑 Delete",
                        "callback_data": "delete_" + file["token"]
                    }
                ]
            ]
        }

        send_message(chat_id, text, keyboard=keyboard)

    send_message(
        chat_id,
        "End of file list.",
        keyboard=back_keyboard()
    )


def show_status(chat_id):
    users = db_execute(
        "SELECT COUNT(*) AS total FROM users",
        fetchone=True
    )

    files = db_execute(
        "SELECT COUNT(*) AS total FROM files",
        fetchone=True
    )

    downloads = db_execute(
        "SELECT COALESCE(SUM(downloads),0) AS total FROM files",
        fetchone=True
    )

    channels = db_execute(
        "SELECT COUNT(*) AS total FROM channels",
        fetchone=True
    )

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       📊 BOT STATUS\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👥 Users: {users['total']}\n"
        f"📁 Files: {files['total']}\n"
        f"⬇ Downloads: {downloads['total']}\n"
        f"🔒 Force Join Channels: {channels['total']}\n\n"
        "🟢 Bot is running."
    )

    send_message(chat_id, text, keyboard=back_keyboard())


def show_channels(chat_id):
    channels = get_channels()

    if not channels:
        send_message(
            chat_id,
            "No force-join channels added.",
            keyboard=channel_keyboard()
        )
        return

    for channel in channels:
        text = (
            f"📢 {channel['channel_name'] or 'Channel'}\n"
            f"🆔 {channel['chat_id']}\n"
            f"🔗 {channel['channel_link'] or 'No link'}"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "🗑 Remove",
                        "callback_data": f"removech_{channel['id']}"
                    }
                ]
            ]
        }

        send_message(chat_id, text, keyboard=keyboard)

    send_message(
        chat_id,
        "Channel management:",
        keyboard=channel_keyboard()
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    user = callback.get("from", {})
    user_id = user.get("id")

    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")

    log.info(
        "CALLBACK RECEIVED | data=%s | user=%s | chat=%s",
        data,
        user_id,
        chat_id
    )

    # Answer callback immediately so Telegram stops loading.
    answer_callback(callback_id, "Processing...")

    if not user_id or not chat_id:
        log.error("CALLBACK MISSING USER OR CHAT")
        return

    if not is_admin(user_id):
        if data == "verifyjoin":
            if check_join(user_id):
                send_message(chat_id, "✅ Verification successful. Send /start again.")
            else:
                send_force_join(chat_id)
        else:
            send_message(chat_id, "⛔ You are not authorized.")
        return

    log.info("ADMIN CALLBACK ACCEPTED | %s", data)

    try:

        if data == "admin":
            clear_state(user_id)
            admin_panel(chat_id)

        elif data == "upload":
            if set_state(user_id, "upload"):
                send_message(
                    chat_id,
                    "📤 Send the file you want to upload.\n\n"
                    "Supported: Document, Video, Audio, Photo and more.\n\n"
                    "Use /cancel to cancel."
                )
            else:
                send_message(chat_id, "❌ Unable to save upload state.")

        elif data == "files":
            show_files(chat_id)

        elif data == "startmsg":
            set_state(user_id, "startmsg")
            send_message(
                chat_id,
                "📝 Send your new Start Message.\n\n"
                "Use /cancel to cancel."
            )

        elif data == "starmsg":
            set_state(user_id, "starmsg")
            send_message(
                chat_id,
                "⭐ Send your new Star Message.\n\n"
                "Use /cancel to cancel."
            )

        elif data == "channels":
            send_message(
                chat_id,
                "🔒 Force Join Management",
                keyboard=channel_keyboard()
            )

        elif data == "addchannel":
            set_state(user_id, "addchannel")
            send_message(
                chat_id,
                "➕ Send channel details in this format:\n\n"
                "CHANNEL_ID | CHANNEL_NAME | CHANNEL_LINK\n\n"
                "Example:\n"
                "-1001234567890 | My Channel | https://t.me/mychannel\n\n"
                "Make sure the bot is an administrator in the channel."
            )

        elif data == "listchannels":
            show_channels(chat_id)

        elif data.startswith("removech_"):
            channel_id = data.replace("removech_", "", 1)

            if not channel_id.isdigit():
                send_message(chat_id, "Invalid channel ID.")
                return

            db_execute(
                "DELETE FROM channels WHERE id=%s",
                (int(channel_id),)
            )

            send_message(
                chat_id,
                "✅ Channel removed.",
                keyboard=channel_keyboard()
            )

        elif data == "status":
            show_status(chat_id)

        elif data == "broadcast":
            set_state(user_id, "broadcast")
            send_message(
                chat_id,
                "📢 Send the broadcast message.\n\n"
                "Use /cancel to cancel."
            )

        elif data.startswith("delete_"):
            token = data.replace("delete_", "", 1)

            db_execute(
                "DELETE FROM files WHERE token=%s",
                (token,)
            )

            send_message(chat_id, "🗑 File deleted.")

        elif data == "verifyjoin":
            if check_join(user_id):
                send_message(
                    chat_id,
                    "✅ Verification successful!\n\nSend /start again."
                )
            else:
                send_force_join(chat_id)

        else:
            send_message(
                chat_id,
                "⚠️ Unknown button action.",
                keyboard=back_keyboard()
            )

        log.info(
            "ADMIN ACTION COMPLETED | data=%s | user=%s",
            data,
            user_id
        )

    except Exception as exc:
        log.exception(
            "ADMIN ACTION FAILED | data=%s | user=%s",
            data,
            user_id
        )

        send_message(
            chat_id,
            "❌ Action failed.\n\n"
            "Please check Render logs or try again."
        )


# =========================================================
# MESSAGE HANDLER
# =========================================================

def handle_message(message):
    user = message.get("from", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user_id = user.get("id")

    if not chat_id or not user_id:
        return

    save_user(user)

    text = message.get("text", "").strip()

    # Cancel current admin action.
    if text == "/cancel" and is_admin(user_id):
        clear_state(user_id)
        send_message(
            chat_id,
            "❎ Action cancelled.",
            keyboard=admin_keyboard()
        )
        return

    # Commands
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1] if len(parts) > 1 else ""

        if is_admin(user_id):
            admin_panel(chat_id)
            return

        if not check_join(user_id):
            send_force_join(chat_id)
            return

        if payload.startswith("f_"):
            token = payload[2:]
            file = get_file(token)

            if not file:
                send_message(chat_id, "❌ File not found.")
                return

            send_message(
                chat_id,
                "☁️ Your file is ready!\n\n"
                f"📄 {file['file_name']}\n\n"
                "Open the button below to access your file.",
                keyboard={
                    "inline_keyboard": [
                        [
                            {
                                "text": "📥 Open File",
                                "url": f"{RENDER_EXTERNAL_URL}/file/{quote(token)}"
                            }
                        ]
                    ]
                }
            )
            return

        send_message(
            chat_id,
            get_setting("start_message"),
            keyboard={
                "inline_keyboard": [
                    [
                        {
                            "text": "⭐ Star Message",
                            "callback_data": "showstar"
                        }
                    ]
                ]
            }
        )
        return

    if text.startswith("/stats"):
        if is_admin(user_id):
            show_status(chat_id)
        return

    if text.startswith("/cancel"):
        if is_admin(user_id):
            clear_state(user_id)
            send_message(chat_id, "Cancelled.")
        return

    # Admin-only state actions
    if is_admin(user_id):
        state_data = get_state(user_id)

        if not state_data:
            if text:
                send_message(
                    chat_id,
                    "Use the admin panel buttons to select an action.",
                    keyboard=admin_keyboard()
                )
            return

        state = state_data.get("state")

        if state == "upload":
            media = get_media(message)

            if not media:
                send_message(
                    chat_id,
                    "❌ Please send a supported file."
                )
                return

            if media["file_size"] > MAX_DOWNLOAD_SIZE:
                send_message(
                    chat_id,
                    "❌ File is larger than the supported 20 MB limit."
                )
                return

            try:
                token = save_file(media)
                clear_state(user_id)

                send_message(
                    chat_id,
                    "✅ File uploaded successfully!\n\n"
                    f"📄 {media['file_name']}\n\n"
                    f"🔗 {file_link(token)}",
                    keyboard=admin_keyboard()
                )

            except Exception:
                log.exception("FILE SAVE FAILED")
                send_message(chat_id, "❌ File could not be saved.")
            return

        if not text:
            send_message(chat_id, "Please send text.")
            return

        if state == "startmsg":
            set_setting("start_message", text)
            clear_state(user_id)

            send_message(
                chat_id,
                "✅ Start Message updated successfully.",
                keyboard=admin_keyboard()
            )
            return

        if state == "starmsg":
            set_setting("star_message", text)
            clear_state(user_id)

            send_message(
                chat_id,
                "✅ Star Message updated successfully.",
                keyboard=admin_keyboard()
            )
            return

        if state == "broadcast":
            clear_state(user_id)

            users = db_execute(
                "SELECT user_id FROM users",
                fetchall=True
            )

            success = 0
            failed = 0

            send_message(chat_id, "📢 Broadcast started...")

            for item in users:
                result = send_message(
                    item["user_id"],
                    text
                )

                if result.get("ok"):
                    success += 1
                else:
                    failed += 1

                time.sleep(0.05)

            send_message(
                chat_id,
                "✅ Broadcast completed.\n\n"
                f"Successful: {success}\n"
                f"Failed: {failed}",
                keyboard=admin_keyboard()
            )
            return

        if state == "addchannel":
            parts = [x.strip() for x in text.split("|", 2)]

            if len(parts) != 3:
                send_message(
                    chat_id,
                    "❌ Invalid format.\n\n"
                    "Use:\nCHANNEL_ID | CHANNEL_NAME | CHANNEL_LINK"
                )
                return

            channel_id, channel_name, channel_link = parts

            if not channel_id or not channel_link.startswith("https://t.me/"):
                send_message(
                    chat_id,
                    "❌ Invalid channel ID or link."
                )
                return

            member_check = tg(
                "getChatMember",
                {
                    "chat_id": channel_id,
                    "user_id": int(
                        tg("getMe")["result"]["id"]
                    )
                }
            )

            if not member_check.get("ok"):
                send_message(
                    chat_id,
                    "❌ Bot cannot access this channel.\n\n"
                    "Add the bot as an administrator first."
                )
                return

            db_execute(
                """
                INSERT INTO channels(chat_id, channel_name, channel_link)
                VALUES (%s, %s, %s)
                """,
                (channel_id, channel_name, channel_link)
            )

            clear_state(user_id)

            send_message(
                chat_id,
                "✅ Force Join channel added successfully.",
                keyboard=channel_keyboard()
            )
            return

    # Normal user fallback
    send_message(
        chat_id,
        "Use /start to access AMR CLOUD."
    )


# =========================================================
# WEBHOOK
# =========================================================

def process_update(update):
    if "callback_query" in update:
        handle_callback(update["callback_query"])

    elif "message" in update:
        handle_message(update["message"])

    else:
        log.info("IGNORED UPDATE TYPE")


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    if WEBHOOK_SECRET:
        received = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if not secrets.compare_digest(received, WEBHOOK_SECRET):
            log.warning("WEBHOOK SECRET INVALID")
            return "Unauthorized", 403

    update = request.get_json(silent=True)

    if not isinstance(update, dict):
        return "Invalid update", 400

    update_id = update.get("update_id")

    log.info(
        "WEBHOOK RECEIVED | update_id=%s | keys=%s",
        update_id,
        list(update.keys())
    )

    if update_id is None:
        return "Missing update ID", 400

    # Claim update atomically. Duplicate webhook deliveries are ignored.
    inserted = db_execute(
        """
        INSERT INTO processed_updates(update_id)
        VALUES (%s)
        ON CONFLICT(update_id) DO NOTHING
        RETURNING update_id
        """,
        (update_id,),
        fetchone=True
    )

    if not inserted:
        log.info("DUPLICATE UPDATE IGNORED | %s", update_id)
        return "OK", 200

    try:
        process_update(update)

        log.info("UPDATE PROCESSED SUCCESSFULLY | %s", update_id)
        return "OK", 200

    except Exception:
        log.exception("UPDATE PROCESSING FAILED | %s", update_id)

        # Allow Telegram to retry a failed update.
        db_execute(
            "DELETE FROM processed_updates WHERE update_id=%s",
            (update_id,)
        )

        return "Processing error", 500


# =========================================================
# WEB ROUTES
# =========================================================

@app.route("/")
def home():
    return (
        "<h2>☁️ AMR CLOUD</h2>"
        "<p>Bot service is running.</p>"
        "<p>Status: Online</p>"
    )


@app.route("/health")
def health():
    try:
        db_execute("SELECT 1")
        return jsonify({
            "status": "ok",
            "bot": "AMR CLOUD",
            "database": "connected"
        })
    except Exception:
        log.exception("HEALTH CHECK FAILED")
        return jsonify({
            "status": "error",
            "database": "disconnected"
        }), 500


def secret_authorized():
    supplied = request.args.get("secret", "")

    return bool(
        UPDATE_SECRET
        and secrets.compare_digest(supplied, UPDATE_SECRET)
    )


@app.route("/setup-webhook")
def setup_webhook():
    if not secret_authorized():
        return "Unauthorized", 403

    if not RENDER_EXTERNAL_URL:
        return "RENDER_EXTERNAL_URL is missing.", 500

    webhook_url = RENDER_EXTERNAL_URL + WEBHOOK_PATH

    data = {
        "url": webhook_url,
        "allowed_updates": json.dumps([
            "message",
            "callback_query"
        ]),
        "drop_pending_updates": "false"
    }

    if WEBHOOK_SECRET:
        data["secret_token"] = WEBHOOK_SECRET

    result = tg("setWebhook", data)

    if result.get("ok"):
        log.info("WEBHOOK CONFIGURED SUCCESSFULLY")
        return jsonify({
            "success": True,
            "webhook_url": webhook_url,
            "telegram_response": result
        })

    return jsonify({
        "success": False,
        "telegram_response": result
    }), 500


@app.route("/webhook-info")
def webhook_info():
    if not secret_authorized():
        return "Unauthorized", 403

    return jsonify(tg("getWebhookInfo"))


# =========================================================
# STARTUP
# =========================================================

try:
    init_db()
except Exception:
    log.exception("DATABASE INITIALIZATION FAILED")
    raise


log.info("AMR CLOUD BOT STARTED")
