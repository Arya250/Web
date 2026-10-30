import os
import re
import uuid
import secrets
import logging
import mimetypes
from functools import wraps

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

from flask import (
    Flask, request, jsonify, abort,
    redirect, make_response, Response,
    render_template, stream_with_context
)

from dotenv import load_dotenv

load_dotenv()

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").replace("@", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "")

BASE_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET",
    secrets.token_hex(32)
)

logging.basicConfig(level=logging.INFO)

admin_states = {}


# ================= DATABASE =================

def db():

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=10
    )


def init_db():

    conn = db()
    cur = conn.cursor()

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
            channel_link TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_updates (
            update_id BIGINT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    cur.close()
    conn.close()


def get_setting(key, default=""):

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT value FROM settings WHERE key=%s",
        (key,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    return row["value"] if row else default


def set_setting(key, value):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO settings(key,value)
        VALUES(%s,%s)
        ON CONFLICT(key)
        DO UPDATE SET value=EXCLUDED.value
    """, (key, value))

    conn.commit()

    cur.close()
    conn.close()


# ================= TELEGRAM API =================

def tg(method, data=None, files=None):

    try:

        r = requests.post(
            f"{API}/{method}",
            data=data,
            files=files,
            timeout=40
        )

        return r.json()

    except Exception as e:

        logging.exception("Telegram API error")

        return {
            "ok": False,
            "description": str(e)
        }


def send_message(chat_id, text, keyboard=None):

    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return tg("sendMessage", data)


def answer_callback(callback_id, text=""):

    tg("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text
    })


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


def inline_keyboard(rows):

    return {
        "inline_keyboard": rows
    }


# ================= ADMIN =================

def is_admin(user_id):

    return int(user_id) == ADMIN_ID


def admin_panel(chat_id):

    keyboard = inline_keyboard([

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
                "text": "✏️ Start Message",
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
            }
        ],

        [
            {
                "text": "🔄 Refresh",
                "callback_data": "admin"
            }
        ]

    ])

    send_message(
        chat_id,
        "<b>⚡ AMR CLOUD ADMIN PANEL</b>\n\n"
        "Choose an option below:",
        keyboard
    )


# ================= USER DATABASE =================

def save_user(user):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
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

    conn.commit()

    cur.close()
    conn.close()


# ================= FORCE JOIN =================

def check_force_join(user_id):

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT chat_id,channel_link FROM channels"
    )

    channels = cur.fetchall()

    cur.close()
    conn.close()

    missing = []

    for channel in channels:

        result = tg("getChatMember", {
            "chat_id": channel["chat_id"],
            "user_id": user_id
        })

        if not result.get("ok"):

            missing.append(channel)
            continue

        status = result["result"]["status"]

        if status not in [
            "member",
            "administrator",
            "creator"
        ]:

            missing.append(channel)

    return missing


# ================= FILE MANAGEMENT =================

def get_media(message):

    media_types = [
        "document",
        "photo",
        "video",
        "audio",
        "voice",
        "animation",
        "video_note"
    ]

    for media_type in media_types:

        if media_type in message:

            media = message[media_type]

            if media_type == "photo":
                media = media[-1]

            original_name = media.get("file_name")

            file_name = original_name or (
                f"{media_type}_{uuid.uuid4().hex[:8]}"
            )

            mime_type = media.get("mime_type")

            if not mime_type:
                mime_type = (
                    mimetypes.guess_type(file_name)[0]
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

    token = secrets.token_urlsafe(12)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO files(
            token,
            file_id,
            file_name,
            file_type,
            file_size,
            mime_type
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

    conn.commit()

    cur.close()
    conn.close()

    return token


def get_file(token):

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM files WHERE token=%s",
        (token,)
    )

    result = cur.fetchone()

    cur.close()
    conn.close()

    return result


def delete_file(token):

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM files WHERE token=%s",
        (token,)
    )

    deleted = cur.rowcount

    conn.commit()

    cur.close()
    conn.close()

    return deleted


# ================= FILE LIST =================

def show_files(chat_id):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT token,file_name,file_type
        FROM files
        ORDER BY id DESC
        LIMIT 30
    """)

    files = cur.fetchall()

    cur.close()
    conn.close()

    if not files:

        send_message(
            chat_id,
            "No files uploaded yet."
        )

        return

    for f in files:

        keyboard = inline_keyboard([[
            {
                "text": "❌ Delete",
                "callback_data": "delete_" + f["token"]
            }
        ]])

        send_message(
            chat_id,
            f"<b>{f['file_name']}</b>\n"
            f"Type: {f['file_type']}\n"
            f"Token: <code>{f['token']}</code>",
            keyboard
        )


# ================= STATUS =================

def show_status(chat_id):

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(*) AS n FROM users"
    )
    users = cur.fetchone()["n"]

    cur.execute(
        "SELECT COUNT(*) AS n FROM files"
    )
    files = cur.fetchone()["n"]

    cur.execute(
        "SELECT COALESCE(SUM(views),0) AS n FROM files"
    )
    views = cur.fetchone()["n"]

    cur.execute(
        "SELECT COALESCE(SUM(downloads),0) AS n FROM files"
    )
    downloads = cur.fetchone()["n"]

    cur.close()
    conn.close()

    send_message(
        chat_id,
        "<b>📊 AMR CLOUD STATUS</b>\n\n"
        f"👥 Users: {users}\n"
        f"📁 Files: {files}\n"
        f"👁 Views: {views}\n"
        f"⬇️ Downloads: {downloads}"
    )


# ================= BROADCAST =================

def broadcast(text):

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id FROM users"
    )

    users = cur.fetchall()

    cur.close()
    conn.close()

    sent = 0

    for user in users:

        result = send_message(
            user["user_id"],
            text
        )

        if result.get("ok"):
            sent += 1

    return sent


# ================= START COMMAND =================

def handle_start(message):

    user = message["from"]
    chat_id = message["chat"]["id"]

    save_user(user)

    text = message.get("text", "")
    parts = text.split(maxsplit=1)

    payload = parts[1] if len(parts) > 1 else ""

    if is_admin(user["id"]) and not payload:

        admin_panel(chat_id)
        return

    missing = check_force_join(user["id"])

    if missing:

        rows = []

        for channel in missing:

            rows.append([{
                "text": "📢 Join Channel",
                "url": channel["channel_link"]
            }])

        rows.append([{
            "text": "✅ Joined - Verify",
            "callback_data": "verify"
        }])

        send_message(
            chat_id,
            "🔒 <b>Join our required channels first.</b>",
            inline_keyboard(rows)
        )

        return

    if payload.startswith("f_"):

        token = payload[2:]

        file = get_file(token)

        if not file:

            send_message(
                chat_id,
                "❌ File not found."
            )

            return

        url = f"{BASE_URL}/file/{token}"

        send_message(
            chat_id,
            "✅ File available!\n\n"
            f"<b>{file['file_name']}</b>\n\n"
            "Open the secure file page below:",
            inline_keyboard([[
                {
                    "text": "🌐 OPEN FILE",
                    "url": url
                }
            ]])
        )

        return

    custom = get_setting(
        "start_message",
        "👋 Welcome to <b>AMR CLOUD</b>!\n\n"
        "Send a file to access your download link."
    )

    send_message(chat_id, custom)


# ================= MESSAGE HANDLER =================

def handle_message(message):

    user = message.get("from", {})
    chat_id = message["chat"]["id"]

    if not user:
        return

    user_id = user["id"]

    if message.get("text", "").startswith("/start"):

        handle_start(message)
        return

    if is_admin(user_id):

        state = admin_states.get(user_id)

        if state == "startmsg":

            set_setting(
                "start_message",
                message.get("text", "")
            )

            admin_states.pop(user_id, None)

            send_message(
                chat_id,
                "✅ Start message updated."
            )

            return

        if state == "starmsg":

            set_setting(
                "star_message",
                message.get("text", "")
            )

            admin_states.pop(user_id, None)

            send_message(
                chat_id,
                "✅ Star message updated."
            )

            return

        if state == "addchannel":

            parts = message.get("text", "").split()

            if len(parts) < 2:

                send_message(
                    chat_id,
                    "Format:\n"
                    "<code>@channelusername https://t.me/channel</code>"
                )

                return

            channel_id = parts[0]
            channel_link = parts[1]

            conn = db()
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO channels(chat_id,channel_link)
                VALUES(%s,%s)
                ON CONFLICT(chat_id)
                DO UPDATE SET channel_link=EXCLUDED.channel_link
            """, (
                channel_id,
                channel_link
            ))

            conn.commit()

            cur.close()
            conn.close()

            admin_states.pop(user_id, None)

            send_message(
                chat_id,
                "✅ Channel configured."
            )

            return

        if state == "broadcast":

            admin_states.pop(user_id, None)

            sent = broadcast(
                message.get("text", "")
            )

            send_message(
                chat_id,
                f"✅ Broadcast completed.\nDelivered: {sent}"
            )

            return

        media = get_media(message)

        if state == "upload" and media:

            token = save_file(media)

            admin_states.pop(user_id, None)

            link = (
                f"https://t.me/{BOT_USERNAME}"
                f"?start=f_{token}"
            )

            send_message(
                chat_id,
                "✅ <b>FILE UPLOADED</b>\n\n"
                f"📁 {media['file_name']}\n"
                f"📦 {media['file_size']} bytes\n\n"
                f"<code>{link}</code>"
            )

            return

    if get_media(message):

        send_message(
            chat_id,
            "Please use the file links provided by AMR CLOUD."
        )


# ================= CALLBACK HANDLER =================

def handle_callback(callback):

    data = callback["data"]
    user_id = callback["from"]["id"]

    message = callback["message"]
    chat_id = message["chat"]["id"]

    answer_callback(callback["id"])

    if data == "verify":

        missing = check_force_join(user_id)

        if missing:

            send_message(
                chat_id,
                "❌ You have not joined all required channels."
            )

        else:

            send_message(
                chat_id,
                "✅ Verification successful. Send /start again."
            )

        return

    if not is_admin(user_id):
        return

    if data == "admin":

        admin_panel(chat_id)
        return

    if data == "upload":

        admin_states[user_id] = "upload"

        send_message(
            chat_id,
            "📤 Send your file now."
        )

        return

    if data == "files":

        show_files(chat_id)
        return

    if data.startswith("delete_"):

        token = data.replace("delete_", "", 1)

        deleted = delete_file(token)

        if deleted:

            send_message(
                chat_id,
                "✅ File deleted."
            )

        else:

            send_message(
                chat_id,
                "❌ File not found."
            )

        return

    if data == "startmsg":

        admin_states[user_id] = "startmsg"

        send_message(
            chat_id,
            "Send your new START message."
        )

        return

    if data == "starmsg":

        admin_states[user_id] = "starmsg"

        send_message(
            chat_id,
            "Send your new STAR message."
        )

        return

    if data == "channels":

        keyboard = inline_keyboard([

            [{
                "text": "➕ Add Force Join Channel",
                "callback_data": "addchannel"
            }],

            [{
                "text": "📋 Channel List",
                "callback_data": "channellist"
            }],

            [{
                "text": "❌ Remove Channel",
                "callback_data": "removechannel"
            }]

        ])

        send_message(
            chat_id,
            "🔒 <b>FORCE JOIN SETTINGS</b>",
            keyboard
        )

        return

    if data == "addchannel":

        admin_states[user_id] = "addchannel"

        send_message(
            chat_id,
            "Send channel username and invite link:\n\n"
            "<code>@channelusername https://t.me/channel</code>\n\n"
            "Add the bot as an administrator in that channel."
        )

        return

    if data == "channellist":

        conn = db()
        cur = conn.cursor()

        cur.execute(
            "SELECT * FROM channels"
        )

        rows = cur.fetchall()

        cur.close()
        conn.close()

        if not rows:

            send_message(
                chat_id,
                "No channels configured."
            )

        else:

            for channel in rows:

                send_message(
                    chat_id,
                    f"{channel['chat_id']}\n"
                    f"{channel['channel_link']}"
                )

        return

    if data == "removechannel":

        conn = db()
        cur = conn.cursor()

        cur.execute(
            "SELECT * FROM channels"
        )

        rows = cur.fetchall()

        cur.close()
        conn.close()

        for channel in rows:

            send_message(
                chat_id,
                f"{channel['chat_id']}",
                inline_keyboard([[
                    {
                        "text": "❌ Remove",
                        "callback_data":
                            "rmch_" + channel["chat_id"]
                    }
                ]])
            )

        return

    if data.startswith("rmch_"):

        channel_id = data.replace(
            "rmch_", "", 1
        )

        conn = db()
        cur = conn.cursor()

        cur.execute(
            "DELETE FROM channels WHERE chat_id=%s",
            (channel_id,)
        )

        conn.commit()

        cur.close()
        conn.close()

        send_message(
            chat_id,
            "✅ Channel removed."
        )

        return

    if data == "status":

        show_status(chat_id)
        return

    if data == "broadcast":

        admin_states[user_id] = "broadcast"

        send_message(
            chat_id,
            "📢 Send the broadcast text."
        )

        return


# ================= WEBHOOK =================

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        abort(403)

    update = request.get_json(silent=True) or {}

    update_id = update.get("update_id")

    if update_id is not None:

        conn = db()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO processed_updates(update_id)
            VALUES(%s)
            ON CONFLICT DO NOTHING
            RETURNING update_id
        """, (update_id,))

        fresh = cur.fetchone()

        conn.commit()

        cur.close()
        conn.close()

        if not fresh:
            return jsonify({"ok": True})

    try:

        if "message" in update:

            handle_message(update["message"])

        elif "callback_query" in update:

            handle_callback(update["callback_query"])

    except Exception:

        logging.exception(
            "Update processing failed"
        )

    return jsonify({"ok": True})


# ================= FILE SIZE =================

def format_size(size):

    size = int(size or 0)

    if size < 1024:
        return f"{size} B"

    if size < 1024 ** 2:
        return f"{size / 1024:.2f} KB"

    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.2f} MB"

    return f"{size / (1024 ** 3):.2f} GB"


# ================= HOME PAGE =================

@app.route("/")
def home():

    return """
    <h1>AMR CLOUD</h1>
    <p>Secure File Sharing Platform</p>
    <p>Service is running.</p>
    """


# ================= HEALTH =================

@app.route("/health")
def health():

    return jsonify({
        "status": "online",
        "service": "AMR CLOUD"
    })


# ================= WEBPAGE =================

@app.route("/file/<token>")
def file_page(token):

    file = get_file(token)

    if not file:
        abort(404)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "UPDATE files SET views=views+1 WHERE token=%s",
        (token,)
    )

    conn.commit()

    cur.close()
    conn.close()

    return render_template(
        "file.html",
        file={
            **file,
            "formatted_size": format_size(
                file["file_size"]
            )
        }
    )


# ================= ACCESS =================

@app.route("/access/<token>", methods=["POST"])
def access_file(token):

    file = get_file(token)

    if not file:
        abort(404)

    response = make_response(
        redirect(f"/file/{token}")
    )

    response.set_cookie(
        f"amr_access_{token}",
        "authorized",
        max_age=3600,
        httponly=True,
        secure=True,
        samesite="Lax"
    )

    return response


# ================= DOWNLOAD =================

@app.route("/download/<token>", methods=["POST"])
def download_file(token):

    file = get_file(token)

    if not file:
        abort(404)

    if request.cookies.get(
        f"amr_access_{token}"
    ) != "authorized":

        return redirect(f"/file/{token}")

    result = tg("getFile", {
        "file_id": file["file_id"]
    })

    if not result.get("ok"):

        return "Telegram file unavailable.", 502

    file_path = result["result"]["file_path"]

    download_url = (
        f"https://api.telegram.org/file/bot"
        f"{BOT_TOKEN}/{file_path}"
    )

    try:

        r = requests.get(
            download_url,
            stream=True,
            timeout=(15, 60)
        )

        if r.status_code != 200:

            r.close()

            return "Download failed.", 502

        filename = re.sub(
            r'[\r\n"\\\\]',
            "_",
            file["file_name"] or "download"
        )

        conn = db()
        cur = conn.cursor()

        cur.execute(
            "UPDATE files SET downloads=downloads+1 WHERE token=%s",
            (token,)
        )

        conn.commit()

        cur.close()
        conn.close()

        def generate():

            try:

                for chunk in r.iter_content(
                    chunk_size=64 * 1024
                ):

                    if chunk:
                        yield chunk

            finally:

                r.close()

        response = Response(
            stream_with_context(generate()),
            content_type=(
                file["mime_type"]
                or "application/octet-stream"
            ),
            headers={
                "Content-Disposition":
                    f'attachment; filename="{filename}"'
            }
        )

        return response

    except Exception:

        logging.exception(
            "Download error"
        )

        return "Download error.", 500


# ================= WEBHOOK SETUP =================

@app.route("/setup-webhook")
def setup_webhook():

    secret = request.args.get(
        "secret", ""
    )

    if not UPDATE_SECRET or not secrets.compare_digest(
        secret,
        UPDATE_SECRET
    ):

        abort(403)

    if not BASE_URL:
        return jsonify({
            "ok": False,
            "error": "RENDER_EXTERNAL_URL is missing"
        }), 500

    webhook_url = (
        f"{BASE_URL}/telegram-webhook"
    )

    result = tg("setWebhook", {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "drop_pending_updates": True
    })

    return jsonify(result)


# ================= STARTUP =================

init_db()

logging.info(
    "AMR CLOUD database initialized."
)


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get("PORT", 5000)
        )
                         )
