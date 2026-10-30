import os
import uuid
import secrets
import mimetypes
import requests
import psycopg2

from flask import (
    Flask, request, jsonify, render_template,
    Response, abort, redirect, url_for
)

from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "")

SITE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024


# ---------------- DATABASE ----------------

def db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    con = db()

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

    con.close()


# ---------------- TELEGRAM ----------------

def telegram(method, data=None):

    response = requests.post(
        f"{API}/{method}",
        json=data or {},
        timeout=30
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise Exception(result.get("description", "Telegram API error"))

    return result["result"]


def send_message(chat_id, message):

    telegram("sendMessage", {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True
    })


def get_file_object(message):

    for key in ("document", "video", "audio"):

        if key in message:
            return message[key]

    if "photo" in message:

        return message["photo"][-1]

    if "animation" in message:
        return message["animation"]

    return None


def save_file(file_obj):

    file_id = file_obj["file_id"]

    file_name = (
        file_obj.get("file_name")
        or f"file_{uuid.uuid4().hex[:8]}"
    )

    mime_type = (
        file_obj.get("mime_type")
        or mimetypes.guess_type(file_name)[0]
        or "application/octet-stream"
    )

    file_size = file_obj.get("file_size", 0)

    if file_size > MAX_DOWNLOAD_SIZE:
        return None, "File exceeds the 20 MB download limit of the standard Telegram Bot API."

    token = secrets.token_urlsafe(24)

    con = db()

    with con:
        with con.cursor() as cur:

            cur.execute("""
                INSERT INTO files
                (token, file_id, file_name, mime_type, file_size)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                token,
                file_id,
                file_name,
                mime_type,
                file_size
            ))

    con.close()

    return token, None


# ---------------- WEBHOOK ----------------

@app.route("/telegram-webhook", methods=["POST"])
def webhook():

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:
        abort(403)

    update = request.json or {}

    message = update.get("message", {})

    if not message:
        return jsonify({"ok": True})

    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")

    text = message.get("text", "")

    if user_id != ADMIN_ID:

        if text.startswith("/start"):
            send_message(
                chat_id,
                "Welcome to AMR Cloud!\nUse the file link shared by the admin."
            )

        return jsonify({"ok": True})

    # ADMIN COMMANDS

    if text == "/start":

        send_message(
            chat_id,
            "Welcome to AMR Cloud Admin!\n\n"
            "Send me a file to upload.\n\n"
            "/stats - View statistics"
        )

        return jsonify({"ok": True})

    if text == "/stats":

        con = db()

        with con:
            with con.cursor() as cur:

                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(downloads), 0),
                    COALESCE(SUM(views), 0)
                    FROM files
                """)

                total, downloads, views = cur.fetchone()

        con.close()

        send_message(
            chat_id,
            f"AMR CLOUD STATISTICS\n\n"
            f"Files: {total}\n"
            f"Views: {views}\n"
            f"Downloads: {downloads}"
        )

        return jsonify({"ok": True})

    # FILE UPLOAD

    file_obj = get_file_object(message)

    if file_obj:

        token, error = save_file(file_obj)

        if error:

            send_message(chat_id, error)

        else:

            bot_username = os.getenv("BOT_USERNAME")

            deep_link = (
                f"https://t.me/{bot_username}?start=f_{token}"
            )

            send_message(
                chat_id,
                "FILE UPLOADED SUCCESSFULLY!\n\n"
                f"File: {file_obj.get('file_name', 'Media file')}\n\n"
                f"SHARE LINK:\n{deep_link}"
            )

        return jsonify({"ok": True})

    # START LINK

    if text.startswith("/start f_"):

        token = text.split("f_", 1)[1].split()[0]

        con = db()

        with con:
            with con.cursor() as cur:

                cur.execute(
                    "SELECT token FROM files WHERE token = %s",
                    (token,)
                )

                result = cur.fetchone()

        con.close()

        if result:

            send_message(
                chat_id,
                "Your file is ready!\n\n"
                f"{SITE_URL}/file/{token}"
            )

        else:

            send_message(chat_id, "File not found.")

        return jsonify({"ok": True})

    send_message(
        chat_id,
        "Send a document, video, image or audio file."
    )

    return jsonify({"ok": True})


# ---------------- WEBSITE ----------------

@app.route("/")
def home():

    return render_template("index.html")


@app.route("/file/<token>")
def file_page(token):

    con = db()

    with con:
        with con.cursor() as cur:

            cur.execute("""
                UPDATE files
                SET views = views + 1
                WHERE token = %s
                RETURNING file_name, file_size, mime_type
            """, (token,))

            result = cur.fetchone()

    con.close()

    if not result:
        abort(404)

    return render_template(
        "file.html",
        token=token,
        file_name=result[0],
        file_size=result[1],
        mime_type=result[2]
    )


# ---------------- DOWNLOAD / PREVIEW ----------------

@app.route("/download/<token>")
def download_file(token):

    preview = request.args.get("preview") == "1"

    con = db()

    with con:
        with con.cursor() as cur:

            cur.execute("""
                SELECT file_id, file_name, mime_type, file_size
                FROM files
                WHERE token = %s
            """, (token,))

            result = cur.fetchone()

    con.close()

    if not result:
        abort(404)

    file_id, file_name, mime_type, file_size = result

    if file_size and file_size > MAX_DOWNLOAD_SIZE:
        abort(413)

    # Get current Telegram download path

    telegram_file = telegram("getFile", {
        "file_id": file_id
    })

    file_path = telegram_file["file_path"]

    file_url = (
        f"https://api.telegram.org/file/bot"
        f"{BOT_TOKEN}/{file_path}"
    )

    upstream = requests.get(
        file_url,
        stream=True,
        timeout=60
    )

    if upstream.status_code != 200:
        upstream.close()
        abort(502)

    disposition = "inline" if preview else "attachment"

    def generate():

        try:
            for chunk in upstream.iter_content(64 * 1024):

                if chunk:
                    yield chunk

        finally:
            upstream.close()

    headers = {
        "Content-Disposition":
            f'{disposition}; filename="{file_name}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store"
    }

    if not preview:

        con = db()

        with con:
            with con.cursor() as cur:

                cur.execute("""
                    UPDATE files
                    SET downloads = downloads + 1
                    WHERE token = %s
                """, (token,))

        con.close()

    return Response(
        generate(),
        content_type=mime_type,
        headers=headers,
        direct_passthrough=True
    )


# ---------------- ADMIN DELETE ----------------

@app.route("/admin/delete/<token>", methods=["POST"])
def delete_file():

    # Additional dashboard authentication can be added here.
    abort(404)


# ---------------- HEALTH CHECK ----------------

@app.route("/health")
def health():
    return jsonify({
        "status": "online",
        "service": "AMR Cloud"
    })


# ---------------- STARTUP ----------------

init_db()

if __name__ == "__main__":
    app.run()
