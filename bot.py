import os
import json
import time
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
# FLASK APP
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


runtime = BotRuntime()


# ==========================================
# TEMPLATE HELPERS
# ==========================================

@app.context_processor
def inject_globals():
    return {
        "bot_username": BOT_USERNAME,
        "website_url": RENDER_EXTERNAL_URL
    }


# ==========================================
# WEBSITE HOME
# ==========================================

@app.route("/")
def homepage():

    if not db.db:
        return render_template(
            "index.html",
            stats={
                "total_files": 0,
                "total_users": 0,
                "total_views": 0,
                "total_downloads": 0
            }
        )

    stats = db.get_public_statistics()

    return render_template(
        "index.html",
        stats=stats
    )


# ==========================================
# DASHBOARD
# ==========================================

@app.route("/dashboard")
def dashboard():

    if not db.db:
        abort(503)

    stats = db.get_public_statistics()

    recent_files = list(
        db.files.find(
            {
                "is_public": True,
                "sharing_enabled": True
            },
            {
                "file_id": 0,
                "_id": 0
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


# ==========================================
# PUBLIC FILE PAGE
# ==========================================

@app.route("/file/<token>")
def shared_file(token):

    if not db.db:
        abort(503)

    file = db.get_public_file(token)

    if not file:
        abort(404)

    db.increment_file_views(file["id"])

    safe_file = {
        "file_name": file.get("file_name", "Unknown"),
        "file_type": file.get("file_type", "document"),
        "file_size": file.get("file_size", 0),
        "mime_type": file.get("mime_type", ""),
        "views": file.get("views", 0) + 1,
        "downloads": file.get("downloads", 0),
        "created_at": file.get("created_at")
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
        {"Content-Type": "text/plain"}
    )


# ==========================================
# HEALTH CHECK
# ==========================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "database": bool(db.db),
        "bot_ready": runtime.ready.is_set(),
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat()
    })


@app.route("/status")
def status():

    return jsonify({
        "project": "DESI CLOUD",
        "database": "MongoDB Atlas",
        "bot_username": BOT_USERNAME,
        "bot_ready": runtime.ready.is_set(),
        "started": runtime.started,
        "error": runtime.start_error
    })


# ==========================================
# WEBHOOK ENDPOINT
# ==========================================

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():

    if not runtime.ready.is_set():
        return jsonify({
            "ok": False,
            "error": "Bot is starting"
        }), 503

    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if supplied_secret != WEBHOOK_SECRET:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 403

    try:
        payload = request.get_json(force=True)

        if not isinstance(payload, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid update"
            }), 400

        update_id = payload.get("update_id")

        if update_id is None:
            return jsonify({
                "ok": False,
                "error": "Missing update_id"
            }), 400

        db.save_webhook_update(
            update_id,
            payload
        )

        return jsonify({
            "ok": True
        }), 200

    except Exception:
        logger.exception("Webhook storage failed")

        return jsonify({
            "ok": False,
            "error": "Webhook processing error"
        }), 500


# ==========================================
# TELEGRAM UPDATE WORKER
# ==========================================

async def process_updates():

    while not runtime.stop_event.is_set():

        try:
            item = db.claim_webhook_update()

            if not item:
                await asyncio.sleep(0.5)
                continue

            try:
                payload = item["payload"]

                update = Update.de_json(
                    payload,
                    runtime.application.bot
                )

                await runtime.application.process_update(
                    update
                )

                db.complete_webhook_update(
                    item["update_id"]
                )

            except Exception as error:

                logger.exception(
                    "Telegram update processing failed"
                )

                db.retry_webhook_update(
                    item["update_id"],
                    error
                )

                # Avoid repeatedly processing a broken update.
                await asyncio.sleep(2)

        except Exception:
            logger.exception("Update worker error")
            await asyncio.sleep(2)


# ==========================================
# TELEGRAM STARTUP
# ==========================================

async def startup_telegram():

    application = Application.builder().token(
        BOT_TOKEN
    ).build()

    register_handlers(application)

    await application.initialize()

    await application.start()

    runtime.application = application

    bot_info = await application.bot.get_me()

    logger.info(
        "Telegram bot connected: @%s",
        bot_info.username
    )

    if WEBHOOK_URL:

        await application.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False
        )

        logger.info(
            "Telegram webhook configured."
        )

    else:
        logger.warning(
            "RENDER_EXTERNAL_URL missing; webhook not configured."
        )

    runtime.ready.set()

    logger.info("DESI CLOUD is ready.")

    await process_updates()


async def shutdown_telegram():

    runtime.ready.clear()

    if runtime.application:

        try:
            if runtime.application.running:
                await runtime.application.stop()

            await runtime.application.shutdown()

        except Exception:
            logger.exception(
                "Telegram shutdown error"
            )


# ==========================================
# EVENT LOOP THREAD
# ==========================================

def telegram_thread():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    runtime.loop = loop

    try:
        db.initialize_database()

        runtime.started = True

        loop.run_until_complete(
            startup_telegram()
        )

    except Exception as error:

        runtime.start_error = str(error)

        logger.exception(
            "Telegram runtime startup failed"
        )

    finally:

        runtime.ready.clear()

        try:
            loop.run_until_complete(
                shutdown_telegram()
            )
        except Exception:
            pass

        loop.close()


# ==========================================
# START BACKGROUND RUNTIME
# ==========================================

def start_runtime():

    if runtime.thread and runtime.thread.is_alive():
        return

    runtime.thread = threading.Thread(
        target=telegram_thread,
        name="desi-cloud-telegram",
        daemon=True
    )

    runtime.thread.start()


# ==========================================
# ERROR PAGES
# ==========================================

@app.errorhandler(404)
def page_not_found(error):

    return render_template(
        "404.html"
    ), 404


@app.errorhandler(500)
def internal_error(error):

    logger.exception(
        "Website internal server error"
    )

    return (
        "Internal server error",
        500
    )


# ==========================================
# START RUNTIME ON IMPORT
# ==========================================

try:

    validate_config()

    start_runtime()

except Exception:

    logger.exception(
        "Configuration validation failed"
    )


# ==========================================
# LOCAL DEVELOPMENT
# ==========================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", "5000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
