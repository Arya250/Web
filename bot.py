import os
import uuid
import secrets
import mimetypes
import logging

import requests
import psycopg2

from urllib.parse import quote

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    Response,
    abort
)

from dotenv import load_dotenv

load_dotenv()

# ==========================================
# AMR CLOUD CONFIGURATION
# ==========================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("AMR-CLOUD")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BOT_USERNAME = os.getenv(
    "BOT_USERNAME", ""
).strip().lstrip("@")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

UPDATE_SECRET = os.getenv("UPDATE_SECRET", "").strip()

SITE_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).strip().rstrip("/")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024

REQUEST_TIMEOUT = 30


# ==========================================
# DATABASE CONNECTION
# ==========================================

def db():

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10
    )


# ==========================================
# DATABASE INITIALIZATION
# ==========================================

def init_db():

    con = db()

    try:

        with con:

            with con.cursor() as cur:

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS files (

                        id SERIAL PRIMARY KEY,

                        token VARCHAR(64) UNIQUE NOT NULL,

                        file_id TEXT NOT NULL,

                        file_name TEXT NOT NULL,

                        mime_type TEXT,

                        file_size BIGINT DEFAULT 0,

                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                        downloads INTEGER DEFAULT 0,

                        views INTEGER DEFAULT 0

                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS processed_updates (

                        update_id BIGINT PRIMARY KEY,

                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

                    )
                """)

        logger.info("Database initialized successfully")

    finally:

        con.close()


# ==========================================
# TELEGRAM API
# ==========================================

def telegram(method, data=None):

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    response = requests.post(

        f"{API}/{method}",

        json=data or {},

        timeout=REQUEST_TIMEOUT

    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):

        raise RuntimeError(
            result.get("description", "Telegram API error")
        )

    return result["result"]


# ==========================================
# SEND MESSAGE
# ==========================================

def send_message(chat_id, message):

    return telegram("sendMessage", {

        "chat_id": chat_id,

        "text": message,

        "disable_web_page_preview": True

    })


# ==========================================
# WEBHOOK CONFIGURATION
# ==========================================

def configure_webhook():

    if not all([
        BOT_TOKEN,
        WEBHOOK_SECRET,
        SITE_URL
    ]):

        logger.error(
            "Missing BOT_TOKEN, WEBHOOK_SECRET or RENDER_EXTERNAL_URL"
        )

        return False

    try:

        webhook_url = SITE_URL + "/telegram-webhook"

        result = telegram("setWebhook", {

            "url": webhook_url,

            "secret_token": WEBHOOK_SECRET,

            "allowed_updates": ["message"]

        })

        logger.info(
            "Webhook configured successfully: %s",
            result
        )

        return True

    except Exception:

        logger.exception("Webhook configuration failed")

        return False


# ==========================================
# FILE DETECTION
# ==========================================

def get_file_object(message):

    for key in (
        "document",
        "video",
        "audio",
        "animation",
        "voice"
    ):

        if key in message:

            return message[key]

    if "photo" in message:

        return message["photo"][-1]

    return None


# ==========================================
# FILE STORAGE
# ==========================================

def save_file(file_obj):

    file_id = file_obj["file_id"]

    file_name = file_obj.get("file_name")

    if not file_name:

        mime = file_obj.get("mime_type", "")

        extension = (
            mimetypes.guess_extension(mime) or ""
        )

        file_name = (
            f"AMR_{uuid.uuid4().hex[:10]}{extension}"
        )

    mime_type = (

        file_obj.get("mime_type")

        or mimetypes.guess_type(file_name)[0]

        or "application/octet-stream"

    )

    file_size = file_obj.get("file_size", 0)

    if file_size > MAX_DOWNLOAD_SIZE:

        return None, (
            "File exceeds the 20 MB limit "
            "of the standard Telegram Bot API."
        )

    token = secrets.token_urlsafe(24)

    con = db()

    try:

        with con:

            with con.cursor() as cur:

                cur.execute("""

                    INSERT INTO files (

                        token,
                        file_id,
                        file_name,
                        mime_type,
                        file_size

                    )

                    VALUES (%s, %s, %s, %s, %s)

                """, (

                    token,
                    file_id,
                    file_name,
                    mime_type,
                    file_size

                ))

    finally:

        con.close()

    return token, None


# ==========================================
# GET FILE
# ==========================================

def get_file(token):

    con = db()

    try:

        with con.cursor() as cur:

            cur.execute("""

                SELECT

                    token,
                    file_id,
                    file_name,
                    mime_type,
                    file_size,
                    downloads,
                    views

                FROM files

                WHERE token = %s

            """, (token,))

            return cur.fetchone()

    finally:

        con.close()


# ==========================================
# MARK UPDATE AS PROCESSED
# ==========================================

def mark_update_processed(update_id):

    con = db()

    try:

        with con:

            with con.cursor() as cur:

                cur.execute("""

                    INSERT INTO processed_updates (update_id)

                    VALUES (%s)

                    ON CONFLICT (update_id) DO NOTHING

                """, (update_id,))

    finally:

        con.close()


# ==========================================
# CHECK DUPLICATE UPDATE
# ==========================================

def is_update_processed(update_id):

    con = db()

    try:

        with con.cursor() as cur:

            cur.execute("""

                SELECT 1

                FROM processed_updates

                WHERE update_id = %s

            """, (update_id,))

            return cur.fetchone() is not None

    finally:

        con.close()


# ==========================================
# START COMMAND HANDLER
# ==========================================

def handle_start(chat_id, user_id, payload):

    # FILE LINK ACCESS

    if payload.startswith("f_"):

        token = payload[2:]

        result = get_file(token)

        if not result:

            send_message(
                chat_id,
                "Sorry! This file does not exist."
            )

            return

        webpage = f"{SITE_URL}/file/{token}"

        send_message(

            chat_id,

            "YOUR FILE IS READY!\n\n"

            "Click below to open the file webpage:\n\n"

            f"{webpage}"

        )

        return

    # ADMIN START

    if user_id == ADMIN_ID:

        send_message(

            chat_id,

            "WELCOME TO AMR CLOUD ADMIN!\n\n"

            "Send a file to upload.\n\n"

            "AVAILABLE COMMANDS:\n\n"

            "/start - Admin menu\n"

            "/stats - File statistics\n"

            "/delete TOKEN - Delete file"

        )

        return

    # NORMAL USER START

    send_message(

        chat_id,

        "WELCOME TO AMR CLOUD!\n\n"

        "This is an admin-managed file platform.\n\n"

        "Open a valid file link to access your file."

    )


# ==========================================
# ADMIN STATISTICS
# ==========================================

def admin_stats(chat_id):

    con = db()

    try:

        with con.cursor() as cur:

            cur.execute("""

                SELECT

                    COUNT(*),

                    COALESCE(SUM(downloads), 0),

                    COALESCE(SUM(views), 0)

                FROM files

            """)

            total, downloads, views = cur.fetchone()

    finally:

        con.close()

    send_message(

        chat_id,

        "AMR CLOUD STATISTICS\n\n"

        f"Total Files: {total}\n"

        f"Total Views: {views}\n"

        f"Total Downloads: {downloads}"

    )


# ==========================================
# ADMIN DELETE
# ==========================================

def admin_delete(chat_id, token):

    con = db()

    try:

        with con:

            with con.cursor() as cur:

                cur.execute("""

                    DELETE FROM files

                    WHERE token = %s

                    RETURNING id

                """, (token,))

                result = cur.fetchone()

    finally:

        con.close()

    if result:

        send_message(
            chat_id,
            "File deleted successfully."
        )

    else:

        send_message(
            chat_id,
            "File not found."
        )


# ==========================================
# TELEGRAM WEBHOOK
# ==========================================

@app.route(
    "/telegram-webhook",
    methods=["POST"]
)

def webhook():

    incoming_secret = request.headers.get(

        "X-Telegram-Bot-Api-Secret-Token",
        ""

    )

    if not WEBHOOK_SECRET or not secrets.compare_digest(

        incoming_secret,
        WEBHOOK_SECRET

    ):

        abort(403)

    update = request.get_json(silent=True) or {}

    update_id = update.get("update_id")

    message = update.get("message")

    if update_id is None or not message:

        return jsonify({"ok": True})

    # DUPLICATE CHECK

    if is_update_processed(update_id):

        return jsonify({"ok": True})

    try:

        chat_id = message["chat"]["id"]

        user_id = message.get(
            "from", {}
        ).get("id")

        text = message.get("text", "").strip()

        chat_type = message.get(
            "chat", {}
        ).get("type")

        # ==================================
        # ONLY PRIVATE CHAT
        # ==================================

        if chat_type != "private":

            mark_update_processed(update_id)

            return jsonify({"ok": True})

        # ==================================
        # START
        # ==================================

        if text.startswith("/start"):

            parts = text.split(maxsplit=1)

            payload = (
                parts[1]
                if len(parts) > 1
                else ""
            )

            handle_start(
                chat_id,
                user_id,
                payload
            )

            mark_update_processed(update_id)

            return jsonify({"ok": True})

        # ==================================
        # ADMIN SECURITY
        # ==================================

        if user_id != ADMIN_ID:

            send_message(

                chat_id,

                "AMR CLOUD is an admin-managed platform."

            )

            mark_update_processed(update_id)

            return jsonify({"ok": True})

        # ==================================
        # STATISTICS
        # ==================================

        if text == "/stats":

            admin_stats(chat_id)

            mark_update_processed(update_id)

            return jsonify({"ok": True})

        # ==================================
        # DELETE
        # ==================================

        if text.startswith("/delete "):

            token = text.split(
                maxsplit=1
            )[1].strip()

            admin_delete(
                chat_id,
                token
            )

            mark_update_processed(update_id)

            return jsonify({"ok": True})

        # ==================================
        # FILE UPLOAD
        # ==================================

        file_obj = get_file_object(message)

        if file_obj:

            token, error = save_file(file_obj)

            if error:

                send_message(
                    chat_id,
                    error
                )

            else:

                deep_link = (

                    f"https://t.me/{BOT_USERNAME}"

                    f"?start=f_{token}"

                )

                send_message(

                    chat_id,

                    "FILE UPLOADED SUCCESSFULLY!\n\n"

                    f"File: {file_obj.get('file_name', 'Media File')}\n\n"

                    "SHARE LINK:\n\n"

                    f"{deep_link}\n\n"

                    f"File ID: {token}\n\n"

                    f"Delete command:\n"

                    f"/delete {token}"

                )

            mark_update_processed(update_id)

            return jsonify({"ok": True})

        # ==================================
        # UNKNOWN MESSAGE
        # ==================================

        send_message(

            chat_id,

            "AMR CLOUD ADMIN PANEL\n\n"

            "Send a file to upload.\n\n"

            "Commands:\n"

            "/start\n"

            "/stats\n"

            "/delete TOKEN"

        )

        mark_update_processed(update_id)

        return jsonify({"ok": True})

    except Exception:

        logger.exception(
            "Webhook processing failed"
        )

        abort(500)


# ==========================================
# HOME PAGE
# ==========================================

@app.route("/")

def home():

    return render_template("index.html")


# ==========================================
# FILE WEBPAGE
# ==========================================

@app.route("/file/<token>")

def file_page(token):

    result = get_file(token)

    if not result:

        abort(404)

    con = db()

    try:

        with con:

            with con.cursor() as cur:

                cur.execute("""

                    UPDATE files

                    SET views = views + 1

                    WHERE token = %s

                """, (token,))

    finally:

        con.close()

    return render_template(

        "file.html",

        token=result[0],

        file_name=result[2],

        mime_type=result[3],

        file_size=result[4] or 0

    )


# ==========================================
# FILE DOWNLOAD / PREVIEW
# ==========================================

@app.route("/download/<token>")

def download_file(token):

    preview = (
        request.args.get("preview") == "1"
    )

    result = get_file(token)

    if not result:

        abort(404)

    file_id = result[1]

    file_name = result[2]

    mime_type = (
        result[3] or "application/octet-stream"
    )

    file_size = result[4] or 0

    if file_size > MAX_DOWNLOAD_SIZE:

        abort(413)

    # GET TELEGRAM FILE PATH

    telegram_file = telegram(

        "getFile",

        {"file_id": file_id}

    )

    file_path = telegram_file["file_path"]

    file_url = (

        f"https://api.telegram.org/file/bot"

        f"{BOT_TOKEN}/{file_path}"

    )

    upstream = requests.get(

        file_url,

        stream=True,

        timeout=(10, 60)

    )

    if upstream.status_code != 200:

        upstream.close()

        abort(502)

    # COUNT DOWNLOADS

    if not preview:

        con = db()

        try:

            with con:

                with con.cursor() as cur:

                    cur.execute("""

                        UPDATE files

                        SET downloads = downloads + 1

                        WHERE token = %s

                    """, (token,))

        finally:

            con.close()

    # SAFE FILE NAME

    safe_name = (
        file_name.replace("\r", "")
        .replace("\n", "")
        .replace('"', "'")
    )

    disposition = (
        "inline" if preview else "attachment"
    )

    headers = {

        "Content-Disposition":

        f'{disposition}; filename="download"; '

        f"filename*=UTF-8''{quote(safe_name)}",

        "X-Content-Type-Options": "nosniff",

        "Cache-Control": "private, no-store"

    }

    def generate():

        try:

            for chunk in upstream.iter_content(

                chunk_size=64 * 1024

            ):

                if chunk:

                    yield chunk

        finally:

            upstream.close()

    return Response(

        generate(),

        content_type=mime_type,

        headers=headers,

        direct_passthrough=True

    )


# ==========================================
# HEALTH CHECK
# ==========================================

@app.route("/health")

def health():

    try:

        con = db()

        con.close()

        return jsonify({

            "status": "online",

            "service": "AMR CLOUD",

            "database": "connected"

        })

    except Exception:

        logger.exception(
            "Health check failed"
        )

        return jsonify({

            "status": "error",

            "service": "AMR CLOUD",

            "database": "disconnected"

        }), 503


# ==========================================
# MANUAL WEBHOOK SETUP
# ==========================================

@app.route("/setup-webhook")

def setup_webhook():

    supplied_secret = request.args.get(
        "secret", ""
    )

    if not UPDATE_SECRET or not secrets.compare_digest(
        supplied_secret,
        UPDATE_SECRET
    ):

        abort(403)

    if configure_webhook():

        return jsonify({

            "status": "success",

            "message": "Webhook configured successfully"

        })

    return jsonify({

        "status": "error",

        "message": "Webhook configuration failed"

    }), 500


# ==========================================
# STARTUP
# ==========================================

try:

    init_db()

    logger.info("AMR CLOUD database ready")

except Exception:

    logger.exception(
        "Database initialization failed"
    )

    raise


if __name__ == "__main__":

    configure_webhook()

    app.run(

        host="0.0.0.0",

        port=int(os.getenv("PORT", 5000))

                )
