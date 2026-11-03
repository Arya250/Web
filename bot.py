import os
import asyncio
import logging
import threading

from datetime import datetime, timezone

from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    abort
)

from telegram import Update
from telegram.ext import Application

import database as db

from config import (
    BOT_TOKEN,
    BOT_USERNAME,
    WEBHOOK_SECRET,
    WEBHOOK_PATH,
    WEBHOOK_URL,
    RENDER_EXTERNAL_URL,
    validate_config
)

from handlers import register_handlers


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger("DESI_CLOUD")


# ==========================================
# FLASK
# ==========================================

app = Flask(__name__)

app.config["JSON_SORT_KEYS"] = False


# ==========================================
# RUNTIME
# ==========================================

class BotRuntime:

    def __init__(self):

        self.application = None

        self.loop = None

        self.ready = threading.Event()

        self.started = False

        self.start_error = None

        self.stop_event = threading.Event()

        self.thread = None

        self.lock = threading.RLock()


runtime = BotRuntime()


# ==========================================
# TEMPLATE GLOBALS
# ==========================================

@app.context_processor
def inject_globals():

    return {
        "bot_username": BOT_USERNAME,
        "website_url": RENDER_EXTERNAL_URL
    }


# ==========================================
# HOMEPAGE
# ==========================================

@app.route("/")
def homepage():

    default_stats = {
        "total_files": 0,
        "total_users": 0,
        "total_views": 0,
        "total_downloads": 0
    }

    try:

        if db.db is None:
            stats = default_stats

        else:
            stats = db.get_public_statistics()

    except Exception:

        logger.exception(
            "Homepage statistics failed."
        )

        stats = default_stats

    return render_template(
        "index.html",
        stats=stats
    )


# ==========================================
# DASHBOARD
# ==========================================

@app.route("/dashboard")
def dashboard():

    if db.db is None:
        abort(503)

    try:

        stats = db.get_public_statistics()

        recent_files = list(
            db.files.find(
                {
                    "is_public": True,
                    "sharing_enabled": True
                },
                {
                    "file_id": 0,
                    "_id": 0,
                    "uploaded_by": 0
                }
            )
            .sort("created_at", -1)
            .limit(10)
        )

        return render_template(
            "dashboard.html",
            stats=stats,
            recent_files=recent_files
        )

    except Exception:

        logger.exception(
            "Dashboard loading failed."
        )

        abort(500)


# ==========================================
# PUBLIC FILE
# ==========================================

@app.route("/file/<token>")
def shared_file(token):

    if db.db is None:
        abort(503)

    try:

        file = db.get_public_file(token)

        if not file:
            abort(404)

        db.increment_file_views(file["id"])

        safe_file = {
            "file_name": file.get(
                "file_name",
                "Unknown"
            ),

            "file_type": file.get(
                "file_type",
                "document"
            ),

            "file_size": file.get(
                "file_size",
                0
            ),

            "mime_type": file.get(
                "mime_type",
                ""
            ),

            "views": file.get(
                "views",
                0
            ) + 1,

            "downloads": file.get(
                "downloads",
                0
            ),

            "created_at": file.get(
                "created_at"
            )
        }

        telegram_link = (
            f"https://t.me/{BOT_USERNAME}?start=file_{token}"
            if BOT_USERNAME else None
        )

        return render_template(
            "file.html",
            file=safe_file,
            telegram_link=telegram_link,
            token=token
        )

    except Exception:

        logger.exception(
            "Shared file page failed."
        )

        abort(500)


# ==========================================
# ROBOTS
# ==========================================

@app.route("/robots.txt")
def robots():

    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /dashboard\n",
        200,
        {
            "Content-Type": "text/plain"
        }
    )


# ==========================================
# HEALTH CHECK
# ==========================================

@app.route("/health")
def health():

    database_ready = db.is_database_ready()

    bot_ready = runtime.ready.is_set()

    healthy = database_ready and bot_ready

    response = {
        "status": "ok" if healthy else "degraded",

        "database": database_ready,

        "bot_ready": bot_ready,

        "started": runtime.started,

        "startup_error": runtime.start_error,

        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    }

    return jsonify(response), (
        200 if healthy else 503
    )


# ==========================================
# STATUS
# ==========================================

@app.route("/status")
def status():

    return jsonify({

        "project": "DESI CLOUD",

        "database": (
            "connected"
            if db.is_database_ready()
            else "disconnected"
        ),

        "bot_username": BOT_USERNAME,

        "bot_ready": runtime.ready.is_set(),

        "started": runtime.started,

        "error": runtime.start_error

    }), 200


# ==========================================
# WEBHOOK
# ==========================================

@app.route(
    WEBHOOK_PATH,
    methods=["POST"]
)
def telegram_webhook():

    # ======================================
    # SECRET VALIDATION
    # ======================================

    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if (
        not WEBHOOK_SECRET
        or supplied_secret != WEBHOOK_SECRET
    ):

        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 403

    # ======================================
    # DATABASE CHECK
    # ======================================

    if db.db is None:

        logger.error(
            "Webhook received while database is unavailable."
        )

        return jsonify({
            "ok": False,
            "error": "Database unavailable"
        }), 503

    # ======================================
    # JSON VALIDATION
    # ======================================

    try:

        payload = request.get_json(
            silent=True
        )

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

        # ==================================
        # QUEUE UPDATE
        # ==================================

        inserted = db.save_webhook_update(
            update_id,
            payload
        )

        return jsonify({

            "ok": True,

            "queued": inserted,

            "duplicate": not inserted

        }), 200

    except Exception:

        logger.exception(
            "Webhook queue insertion failed."
        )

        return jsonify({
            "ok": False,
            "error": "Webhook processing failed"
        }), 500


# ==========================================
# TELEGRAM UPDATE WORKER
# ==========================================

async def process_updates():

    logger.info(
        "Telegram update worker started."
    )

    while not runtime.stop_event.is_set():

        try:

            # MongoDB calls are synchronous.
            # Run them outside the asyncio event loop.

            item = await asyncio.to_thread(
                db.claim_webhook_update
            )

            if not item:

                await asyncio.sleep(0.5)

                continue

            update_id = item["update_id"]

            try:

                payload = item["payload"]

                update = Update.de_json(
                    payload,
                    runtime.application.bot
                )

                await runtime.application.process_update(
                    update
                )

                await asyncio.to_thread(
                    db.complete_webhook_update,
                    update_id
                )

                logger.info(
                    "Update %s processed successfully.",
                    update_id
                )

            except Exception as error:

                logger.exception(
                    "Update %s processing failed.",
                    update_id
                )

                await asyncio.to_thread(
                    db.retry_webhook_update,
                    update_id,
                    error
                )

        except Exception:

            logger.exception(
                "Telegram update worker error."
            )

            await asyncio.sleep(2)


# ==========================================
# TELEGRAM STARTUP
# ==========================================

async def startup_telegram():

    logger.info(
        "Initializing Telegram application..."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    register_handlers(application)

    await application.initialize()

    await application.start()

    runtime.application = application

    # ======================================
    # VERIFY BOT
    # ======================================

    bot_info = await application.bot.get_me()

    logger.info(
        "Telegram bot connected: @%s",
        bot_info.username
    )

    # ======================================
    # MARK READY
    # ======================================

    runtime.ready.set()

    runtime.started = True

    runtime.start_error = None

    logger.info(
        "Telegram application is ready."
    )

    # ======================================
    # CONFIGURE WEBHOOK
    # ======================================

    webhook_url = WEBHOOK_URL

    if not webhook_url and RENDER_EXTERNAL_URL:

        webhook_url = (
            RENDER_EXTERNAL_URL.rstrip("/")
            + WEBHOOK_PATH
        )

    if webhook_url:

        await application.bot.set_webhook(

            url=webhook_url,

            secret_token=WEBHOOK_SECRET,

            allowed_updates=Update.ALL_TYPES,

            drop_pending_updates=False

        )

        logger.info(
            "Telegram webhook configured successfully."
        )

    else:

        logger.warning(
            "Webhook URL missing. Webhook not configured."
        )

    logger.info(
        "DESI CLOUD is ready."
    )

    # ======================================
    # PROCESS QUEUE
    # ======================================

    await process_updates()


# ==========================================
# TELEGRAM SHUTDOWN
# ==========================================

async def shutdown_telegram():

    runtime.ready.clear()

    application = runtime.application

    if application is None:
        return

    try:

        if application.running:

            await application.stop()

        await application.shutdown()

        logger.info(
            "Telegram application stopped."
        )

    except Exception:

        logger.exception(
            "Telegram shutdown failed."
        )

    finally:

        runtime.application = None


# ==========================================
# EVENT LOOP THREAD
# ==========================================

def telegram_thread():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    runtime.loop = loop

    try:

        # ==================================
        # CONFIGURATION
        # ==================================

        validate_config()

        # ==================================
        # DATABASE
        # ==================================

        db.initialize_database()

        logger.info(
            "Database initialization completed."
        )

        # ==================================
        # TELEGRAM
        # ==================================

        loop.run_until_complete(
            startup_telegram()
        )

    except Exception as error:

        runtime.ready.clear()

        runtime.started = False

        runtime.start_error = (
            type(error).__name__
            + ": "
            + str(error)
        )

        logger.exception(
            "DESI CLOUD runtime startup failed."
        )

    finally:

        runtime.ready.clear()

        try:

            if loop.is_running():

                loop.run_until_complete(
                    shutdown_telegram()
                )

            elif runtime.application:

                loop.run_until_complete(
                    shutdown_telegram()
                )

        except Exception:

            logger.exception(
                "Runtime shutdown error."
            )

        try:

            loop.close()

        except Exception:
            pass

        runtime.loop = None

        runtime.started = False

        db.close_database()


# ==========================================
# START RUNTIME
# ==========================================

def start_runtime():

    with runtime.lock:

        if (
            runtime.thread is not None
            and runtime.thread.is_alive()
        ):

            return

        runtime.stop_event.clear()

        runtime.start_error = None

        runtime.started = False

        runtime.ready.clear()

        runtime.thread = threading.Thread(

            target=telegram_thread,

            name="desi-cloud-telegram",

            daemon=True

        )

        runtime.thread.start()

        logger.info(
            "DESI CLOUD background runtime started."
        )


# ==========================================
# ERROR HANDLERS
# ==========================================

@app.errorhandler(404)
def page_not_found(error):

    return render_template(
        "404.html"
    ), 404


@app.errorhandler(500)
def internal_error(error):

    logger.exception(
        "Website internal server error."
    )

    return (
        "Internal server error",
        500
    )


# ==========================================
# STARTUP
# ==========================================

start_runtime()


# ==========================================
# LOCAL DEVELOPMENT
# ==========================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False,

        use_reloader=False

        )
