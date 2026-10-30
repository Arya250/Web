import os
import uuid
import secrets
import mimetypes
import logging
import requests
import psycopg2

from urllib.parse import quote
from flask import (
    Flask, request, jsonify, render_template,
    Response, abort
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL", "")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

SITE_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024


# ==========================================
# DATABASE
# ==========================================

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    return psycopg2.connect(DATABASE_URL)


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
                        file_size BIGINT,
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

    finally:
        con.close()

    logger.info("Database initialized successfully")


# ==========================================
# TELEGRAM API
# ==========================================

def telegram(method, data=None):

    response = requests.post(
        f"{API}/{method}",
        json=data or {},
        timeout=30
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Telegram API error")
        )

    return result["result"]


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

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing")
        return

    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET missing")
        return

    if not SITE_URL:
        logger.error("RENDER_EXTERNAL_URL missing")
        return

    webhook_url = SITE_URL + "/telegram-webhook"

    try:

        result = telegram("setWebhook", {
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message"]
        })

        if result:
            logger.info("Webhook configured successfully")

    except Exception:
        logger.exception("Webhook configuration failed")


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
# SAVE FILE
# ==========================================

def save_file(file_obj):

    file_id = file_obj["file_id"]

    file_name = file_obj.get("file_name")

    if not file_name:

        if file_obj.get("mime_type"):
            extension = mimetypes.guess_extension(
                file_obj["mime_type"]
            ) or ""

            file_name = f"AMR_{uuid.uuid4().hex[:8]}{extension}"

        else:
            file_name = f"AMR_{uuid.uuid4().hex[:8]}.jpg"

    mime_type = (
        file_obj.get("mime_type")
        or mimetypes.guess_type(file_name)[0]
        or "application/octet-stream"
    )

    file_size = file_obj.get("file_size", 0)

    if file_size > MAX_DOWNLOAD_SIZE:

        return None, (
            "File is larger than 20 MB. "
            "Please upload a smaller file."
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
# DATABASE FILE LOOKUP
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
# FILE START LINK HANDLER
# ==========================================

def handle_file_start(chat_id, payload):

    if not payload.startswith("f_"):

        send_message(
            chat_id,
            "Welcome to AMR Cloud!\n\n"
            "Open a valid file link to access your file."
        )

        return

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
        "Open the webpage below to preview or download:\n\n"
        f"{webpage}"
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

                deleted = cur.fetchone()

    finally:
        con.close()

    if deleted:

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

@app.route("/telegram-webhook", methods=["POST"])
def webhook():

    incoming_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if not secrets.compare_digest(
        incoming_secret,
        WEBHOOK_SECRET
    ):
        abort(403)

    update = request.get_json(silent=True) or {}

    update_id = update.get("update_id")

    message = update.get("message")

    if not message or update_id is None:
        return jsonify({"ok": True})

    # Prevent duplicate processing

    con = db()

    try:

        with con:

            with con.cursor() as cur:

                cur.execute("""
                    INSERT INTO processed_updates (update_id)
                    VALUES (%s)
                    ON CONFLICT (update_id) DO NOTHING
                    RETURNING update_id
                """, (update_id,))

                inserted = cur.fetchone()

        if not inserted:
            return jsonify({"ok": True})

    finally:
        con.close()

    try:

        chat_id = message["chat"]["id"]

        user_id = message.get("from", {}).get("id")

        text = message.get("text", "").strip()

        # ==================================
        # START COMMAND
        # ==================================

        if text.startswith("/start"):

            parts = text.split(maxsplit=1)

            payload = parts[1] if len(parts) > 1 else ""

            # File links work for EVERY user.
            handle_file_start(chat_id, payload)

            return jsonify({"ok": True})

        # ==================================
        # ADMIN SECURITY
        # ==================================

        if user_id != ADMIN_ID:

            send_message(
                chat_id,
                "AMR Cloud is an admin-managed file platform."
            )

            return jsonify({"ok": True})

        # ==================================
        # ADMIN COMMANDS
        # ==================================

        if text == "/stats":

            admin_stats(chat_id)

            return jsonify({"ok": True})

        if text.startswith("/delete "):

            token = text.split(maxsplit=1)[1].strip()

            admin_delete(chat_id, token)

            return jsonify({"ok": True})

        # ==================================
        # ADMIN FILE UPLOAD
        # ==================================

        file_obj = get_file_object(message)

        if file_obj:

            token, error = save_file(file_obj)

            if error:

                send_message(chat_id, error)

            else:

                deep_link = (
                    f"https://t.me/{BOT_USERNAME}"
                    f"?start=f_{token}"
                )

                send_message(
                    chat_id,
                    "FILE UPLOADED SUCCESSFULLY!\n\n"
                    f"File: {file_obj.get('file_name', 'Media file')}\n\n"
                    "SHARE LINK:\n"
                    f"{deep_link}\n\n"
                    f"File ID: {token}\n\n"
                    f"Delete command:\n/delete {token}"
                )

            return jsonify({"ok": True})

        # ==================================
        # UNKNOWN ADMIN MESSAGE
        # ==================================

        send_message(
            chat_id,
            "AMR CLOUD ADMIN\n\n"
            "Send a file to upload.\n\n"
            "Commands:\n"
            "/start - Start bot\n"
            "/stats - Statistics\n"
            "/delete TOKEN - Delete file"
        )

        return jsonify({"ok": True})

    except Exception:

        logger.exception("Webhook processing failed")

        # Allow Telegram to retry failed processing.
        abort(500)


# ==========================================
# WEBSITE HOME
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
# DOWNLOAD AND PREVIEW
# ==========================================

@app.route("/download/<token>")
def download_file(token):

    preview = request.args.get("preview") == "1"

    result = get_file(token)

    if not result:
        abort(404)

    file_id = result[1]
    file_name = result[2]
    mime_type = result[3] or "application/octet-stream"
    file_size = result[4] or 0

    if file_size > MAX_DOWNLOAD_SIZE:
        abort(413)

    # Get Telegram file path

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

    # Count downloads

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

    safe_name = file_name.replace("\r", "").replace("\n", "")

    disposition = (
        "inline" if preview else "attachment"
    )

    headers = {

        "Content-Disposition":
            f'{disposition}; filename="file"; '
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

        logger.exception("Health check failed")

        return jsonify({
            "status": "error",
            "service": "AMR CLOUD",
            "database": "disconnected"
        }), 503


# ==========================================
# STARTUP
# ==========================================

init_db()

configure_webhook()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000))
    )

---

2. GitHub par kya update karna hai?

Sirf "bot.py" replace karo.

Baaki files:

amr-cloud/
│
├── bot.py
├── requirements.txt
├── Procfile
├── .env.example
│
└── templates/
    ├── index.html
    └── file.html

Tumhara "requirements.txt":

Flask==3.1.2
gunicorn==23.0.0
requests==2.32.5
psycopg2-binary==2.9.10
python-dotenv==1.1.1

3. Procfile

web: gunicorn bot:app --workers 1 --threads 4 --timeout 120

4. Render Environment Variables

Ye variables check karna:

Variable| Required
BOT_TOKEN| Yes
BOT_USERNAME| Yes
ADMIN_ID| Yes
DATABASE_URL| Yes
WEBHOOK_SECRET| Yes
RENDER_EXTERNAL_URL| Yes
UPDATE_SECRET| Not currently used

"RENDER_EXTERNAL_URL" mein apne Render service ka actual URL dena.

---

5. Deployment ke baad testing

Tumhari website ka URL agar "https://web-0454.onrender.com" hai, toh:

Website test:

"https://web-0454.onrender.com/"

Health check:

"https://web-0454.onrender.com/health"

Expected response:

{
  "status": "online",
  "service": "AMR CLOUD",
  "database": "connected"
}

Telegram test:

1. Bot par "/start" bhejo.
2. PDF upload karo.
3. Bot sharing link dega.
4. Link private Telegram channel mein post karo.
5. Kisi doosre Telegram account se link open karo.
6. START dabao.
7. Website link milna chahiye.
8. Website par PDF preview aur download test karo.

---

⚠️ Important technical limitations

- Standard Telegram Bot API se download limit 20 MB hai.
- Render free service sleep kar sakti hai.
- Render free PostgreSQL ki storage aur expiry limitations hain.
- Actual advertisements tabhi aayenge jab approved ad network integrate hoga.
- Processed update records database mein badhte rahenge; production mein unki periodic cleanup zaroori hai.
- Is version mein admin-only upload aur bot-based deletion hai; public admin dashboard abhi implement nahi hua hai.

Abhi pehle "bot.py" replace karke deploy karo. Agar Render logs mein "Webhook configured successfully" aata hai, toh webhook registration successful hai.
