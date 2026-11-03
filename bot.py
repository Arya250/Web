import os
import hmac
import json
import time
import asyncio
import logging
import threading
import atexit

from datetime import datetime, timezone
from functools import wraps

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    abort
)

from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook").strip()

PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is missing")

if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET environment variable is missing")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("DESI_CLOUD")


# ============================================================
# FLASK APPLICATION
# ============================================================

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static"
)

app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        sslmode="require"
    )


def initialize_queue_database():
    """
    Creates the durable webhook queue.
    Existing records are not deleted.
    """

    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS webhook_updates (
                    update_id BIGINT PRIMARY KEY,
                    payload JSONB NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_webhook_updates_status
                ON webhook_updates(status, created_at)
            """)

            # Recover updates interrupted by a previous restart.
            cursor.execute("""
                UPDATE webhook_updates
                SET status = 'pending',
                    updated_at = NOW()
                WHERE status = 'processing'
                AND updated_at < NOW() - INTERVAL '5 minutes'
            """)

        connection.commit()

        logger.info("Webhook queue database initialized")

    finally:
        connection.close()


def initialize_project_database():
    """
    Initializes the existing database.py connection pool.
    """

    try:
        import database

        initializer = getattr(
            database,
            "initialize_database_pool",
            None
        )

        if initializer:
            initializer()
            logger.info("Project database pool initialized")
        else:
            logger.warning(
                "initialize_database_pool() not found in database.py"
            )

    except Exception:
        logger.exception("Project database initialization failed")
        raise


# ============================================================
# WEBHOOK QUEUE
# ============================================================

def save_webhook_update(update_id, payload):

    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            cursor.execute("""
                INSERT INTO webhook_updates (
                    update_id,
                    payload,
                    status
                )
                VALUES (%s, %s, 'pending')
                ON CONFLICT (update_id) DO NOTHING
            """, (
                update_id,
                json.dumps(payload)
            ))

        connection.commit()

    finally:
        connection.close()


def claim_webhook_update():

    connection = get_connection()

    try:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            cursor.execute("""
                SELECT update_id, payload
                FROM webhook_updates
                WHERE status = 'pending'
                AND attempts < 10
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """)

            row = cursor.fetchone()

            if not row:
                connection.commit()
                return None

            cursor.execute("""
                UPDATE webhook_updates
                SET status = 'processing',
                    attempts = attempts + 1,
                    updated_at = NOW()
                WHERE update_id = %s
            """, (row["update_id"],))

        connection.commit()

        return dict(row)

    finally:
        connection.close()


def mark_webhook_done(update_id):

    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            cursor.execute("""
                UPDATE webhook_updates
                SET status = 'done',
                    last_error = NULL,
                    updated_at = NOW()
                WHERE update_id = %s
            """, (update_id,))

        connection.commit()

    finally:
        connection.close()


def mark_webhook_failed(update_id, error):

    connection = get_connection()

    try:
        with connection.cursor() as cursor:

            cursor.execute("""
                UPDATE webhook_updates
                SET status = CASE
                    WHEN attempts >= 10 THEN 'failed'
                    ELSE 'pending'
                END,
                last_error = %s,
                updated_at = NOW()
                WHERE update_id = %s
            """, (
                str(error)[:4000],
                update_id
            ))

        connection.commit()

    finally:
        connection.close()


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

telegram_application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


async def telegram_error_handler(update, context):

    logger.error(
        "Telegram handler error: %s",
        context.error,
        exc_info=context.error
    )


async def process_queued_updates():

    while True:

        try:

            item = await asyncio.to_thread(
                claim_webhook_update
            )

            if item is None:
                await asyncio.sleep(1)
                continue

            update_id = item["update_id"]
            payload = item["payload"]

            if isinstance(payload, str):
                payload = json.loads(payload)

            try:

                telegram_update = Update.de_json(
                    payload,
                    telegram_application.bot
                )

                await telegram_application.process_update(
                    telegram_update
                )

                await asyncio.to_thread(
                    mark_webhook_done,
                    update_id
                )

                logger.info(
                    "Telegram update processed: %s",
                    update_id
                )

            except Exception as error:

                logger.exception(
                    "Update processing failed: %s",
                    update_id
                )

                await asyncio.to_thread(
                    mark_webhook_failed,
                    update_id,
                    error
                )

        except Exception:

            logger.exception("Webhook queue worker error")

            await asyncio.sleep(3)


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def startup_telegram():

    logger.info("Initializing DESI CLOUD Telegram bot")

    initialize_project_database()

    await asyncio.to_thread(
        initialize_queue_database
    )

    # Register all Telegram commands and callbacks.
    from handlers import register_handlers

    register_handlers(telegram_application)

    telegram_application.add_error_handler(
        telegram_error_handler
    )

    await telegram_application.initialize()

    await telegram_application.start()

    webhook_url = (
        RENDER_EXTERNAL_URL + WEBHOOK_PATH
    )

    await telegram_application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )

    logger.info(
        "Telegram webhook configured: %s",
        webhook_url
    )

    asyncio.create_task(
        process_queued_updates()
    )

    logger.info("DESI CLOUD Telegram bot is ready")


# ============================================================
# BACKGROUND EVENT LOOP
# ============================================================

class TelegramRuntime:

    def __init__(self):

        self.loop = None
        self.thread = None
        self.ready = threading.Event()
        self.failed = False
        self.error = None

    def start(self):

        self.thread = threading.Thread(
            target=self.run,
            name="telegram-runtime",
            daemon=True
        )

        self.thread.start()

    def run(self):

        self.loop = asyncio.new_event_loop()

        asyncio.set_event_loop(self.loop)

        try:

            self.loop.run_until_complete(
                startup_telegram()
            )

            self.ready.set()

            self.loop.run_forever()

        except Exception as error:

            self.failed = True
            self.error = str(error)

            logger.exception(
                "Telegram runtime startup failed"
            )

        finally:

            if self.loop and not self.loop.is_closed():

                pending = asyncio.all_tasks(self.loop)

                for task in pending:
                    task.cancel()

                self.loop.run_until_complete(
                    asyncio.gather(
                        *pending,
                        return_exceptions=True
                    )
                )

                self.loop.close()

    def stop(self):

        if self.loop and self.loop.is_running():

            async def shutdown():

                try:
                    if telegram_application.running:
                        await telegram_application.stop()

                    await telegram_application.shutdown()

                except Exception:
                    logger.exception(
                        "Telegram shutdown error"
                    )

                self.loop.stop()

            asyncio.run_coroutine_threadsafe(
                shutdown(),
                self.loop
            )


runtime = TelegramRuntime()

runtime.start()


# ============================================================
# SECURITY
# ============================================================

def verify_webhook_secret():

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    return hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    )


# ============================================================
# WEBSITE STATISTICS
# ============================================================

def get_public_statistics():

    stats = {
        "users": 0,
        "files": 0,
        "views": 0,
        "downloads": 0,
        "shared_files": 0
    }

    connection = get_connection()

    try:

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM users) AS users,

                    (SELECT COUNT(*) FROM files) AS files,

                    (SELECT COALESCE(SUM(views), 0)
                     FROM files) AS views,

                    (SELECT COALESCE(SUM(downloads), 0)
                     FROM files) AS downloads,

                    (SELECT COUNT(*)
                     FROM files
                     WHERE is_public = TRUE
                     AND sharing_enabled = TRUE)
                     AS shared_files
            """)

            result = cursor.fetchone()

            if result:
                stats.update(dict(result))

    finally:
        connection.close()

    return stats


# ============================================================
# WEBSITE ROUTES
# ============================================================

@app.route("/")
def home():

    try:

        stats = get_public_statistics()

        return render_template(
            "index.html",
            stats=stats,
            bot_username=BOT_USERNAME,
            current_year=datetime.now(
                timezone.utc
            ).year
        )

    except Exception:

        logger.exception("Homepage error")

        return render_template(
            "index.html",
            stats={
                "users": 0,
                "files": 0,
                "views": 0,
                "downloads": 0,
                "shared_files": 0
            },
            bot_username=BOT_USERNAME,
            current_year=datetime.now(
                timezone.utc
            ).year
        )


@app.route("/dashboard")
def dashboard():

    try:

        stats = get_public_statistics()

        return render_template(
            "dashboard.html",
            stats=stats,
            bot_username=BOT_USERNAME,
            current_year=datetime.now(
                timezone.utc
            ).year
        )

    except Exception:

        logger.exception("Dashboard error")

        abort(500)


@app.route("/file/<token>")
def shared_file(token):

    connection = get_connection()

    try:

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            cursor.execute("""
                SELECT
                    token,
                    file_name,
                    file_type,
                    file_size,
                    mime_type,
                    views,
                    downloads,
                    created_at,
                    is_public,
                    sharing_enabled

                FROM files

                WHERE token = %s
                AND is_public = TRUE
                AND sharing_enabled = TRUE

                LIMIT 1
            """, (token,))

            file = cursor.fetchone()

            if not file:
                abort(404)

            cursor.execute("""
                UPDATE files
                SET views = views + 1
                WHERE token = %s
            """, (token,))

        connection.commit()

        file = dict(file)

        size = file.get("file_size") or 0

        if size < 1024:
            formatted_size = f"{size} B"

        elif size < 1024 * 1024:
            formatted_size = f"{size / 1024:.2f} KB"

        elif size < 1024 * 1024 * 1024:
            formatted_size = f"{size / (1024 * 1024):.2f} MB"

        else:
            formatted_size = (
                f"{size / (1024 * 1024 * 1024):.2f} GB"
            )

        file["formatted_size"] = formatted_size

        return render_template(
            "file.html",
            file=file,
            bot_username=BOT_USERNAME,
            current_year=datetime.now(
                timezone.utc
            ).year
        )

    finally:
        connection.close()


@app.errorhandler(404)
def page_not_found(error):

    return render_template(
        "404.html",
        current_year=datetime.now(
            timezone.utc
        ).year
    ), 404


@app.errorhandler(500)
def internal_server_error(error):

    logger.error("Website internal server error")

    return "Internal server error", 500


# ============================================================
# TELEGRAM WEBHOOK ROUTE
# ============================================================

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():

    if not verify_webhook_secret():

        logger.warning("Invalid Telegram webhook secret")

        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 403

    if not runtime.ready.is_set():

        return jsonify({
            "ok": False,
            "error": "Bot is starting"
        }), 503

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):

        return jsonify({
            "ok": False,
            "error": "Invalid JSON"
        }), 400

    update_id = payload.get("update_id")

    if update_id is None:

        return jsonify({
            "ok": False,
            "error": "Missing update_id"
        }), 400

    try:

        save_webhook_update(
            int(update_id),
            payload
        )

        return jsonify({
            "ok": True,
            "message": "Update queued"
        }), 200

    except Exception:

        logger.exception(
            "Failed to save Telegram update"
        )

        return jsonify({
            "ok": False,
            "error": "Database unavailable"
        }), 503


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    if runtime.ready.is_set():

        return jsonify({
            "status": "healthy",
            "service": "DESI CLOUD",
            "telegram": "running",
            "database": "connected"
        }), 200

    return jsonify({
        "status": "starting",
        "service": "DESI CLOUD",
        "telegram": "not_ready",
        "error": runtime.error
    }), 503


@app.route("/robots.txt")
def robots():

    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /telegram-webhook\n"
    ), 200, {
        "Content-Type": "text/plain"
    }


# ============================================================
# ROOT STATUS
# ============================================================

@app.route("/status")
def status():

    return jsonify({
        "project": "DESI CLOUD",
        "version": "1.0.0",
        "status": (
            "running"
            if runtime.ready.is_set()
            else "starting"
        )
    })


# ============================================================
# SHUTDOWN
# ============================================================

@atexit.register
def shutdown_runtime():

    try:
        runtime.stop()

    except Exception:
        logger.exception("Runtime shutdown failed")


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
