import os
import json
import logging
import threading

from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import (
    Application,
)

import database as db
import handlers


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH",
    "/telegram-webhook"
)

if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

WEBHOOK_URL = RENDER_EXTERNAL_URL + WEBHOOK_PATH

PORT = int(os.getenv("PORT", "10000"))


# --------------------------------------------------
# FLASK
# --------------------------------------------------

app = Flask(__name__)

bot_application = None

bot_ready = threading.Event()
startup_lock = threading.Lock()

startup_error = None


# --------------------------------------------------
# TELEGRAM APPLICATION
# --------------------------------------------------

def create_bot_application():
    global bot_application

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # handlers.py must provide this function.
    register_function = getattr(
        handlers,
        "register_handlers",
        None
    )

    if not callable(register_function):
        raise RuntimeError(
            "handlers.py must define register_handlers(application)."
        )

    register_function(application)

    return application


# --------------------------------------------------
# STARTUP
# --------------------------------------------------

def initialize_bot():
    global bot_application, startup_error

    with startup_lock:
        if bot_ready.is_set():
            return True

        try:
            logger.info("Initializing MongoDB...")

            if not db.initialize_database():
                raise RuntimeError(
                    "MongoDB initialization failed."
                )

            logger.info("Initializing Telegram application...")

            bot_application = create_bot_application()

            bot_application.initialize_sync = None

            logger.info("Starting Telegram application...")

            # Initialize and start the Telegram application.
            import asyncio

            async def start_application():
                await bot_application.initialize()
                await bot_application.start()

                await bot_application.bot.set_webhook(
                    url=WEBHOOK_URL,
                    secret_token=WEBHOOK_SECRET or None,
                    allowed_updates=Update.ALL_TYPES
                )

            asyncio.run(start_application())

            bot_ready.set()
            startup_error = None

            logger.info("DESI CLOUD bot is ready.")

            return True

        except Exception as exc:
            startup_error = str(exc)

            logger.exception(
                "DESI CLOUD startup failed."
            )

            return False


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "project": "DESI CLOUD",
        "status": "online" if bot_ready.is_set() else "starting",
        "database": db.is_database_ready(),
        "bot_ready": bot_ready.is_set()
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy" if bot_ready.is_set() else "starting",
        "database": db.is_database_ready(),
        "bot_ready": bot_ready.is_set(),
        "error": startup_error
    }), 200 if bot_ready.is_set() else 503


# --------------------------------------------------
# TELEGRAM WEBHOOK
# --------------------------------------------------

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():

    if not bot_ready.is_set():
        return jsonify({
            "ok": False,
            "error": "Bot is not ready."
        }), 503

    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if received_secret != WEBHOOK_SECRET:
            return jsonify({
                "ok": False,
                "error": "Unauthorized"
            }), 403

    try:
        payload = request.get_json(force=True)

        if not isinstance(payload, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid update payload."
            }), 400

        update_id = payload.get("update_id")

        if update_id is None:
            return jsonify({
                "ok": False,
                "error": "Missing update_id."
            }), 400

        db.save_webhook_update(
            update_id,
            payload
        )

        import asyncio

        async def process_update():
            update = Update.de_json(
                payload,
                bot_application.bot
            )

            await bot_application.process_update(update)

        # Process Telegram update.
        asyncio.run(process_update())

        db.complete_webhook_update(update_id)

        return jsonify({"ok": True}), 200

    except Exception as exc:
        logger.exception("Webhook processing failed.")

        try:
            if "update_id" in locals():
                db.retry_webhook_update(
                    update_id,
                    str(exc)
                )
        except Exception:
            logger.exception("Could not schedule webhook retry.")

        return jsonify({
            "ok": False,
            "error": "Update processing failed."
        }), 500


# --------------------------------------------------
# STARTUP HOOK
# --------------------------------------------------

@app.before_request
def ensure_started():
    if not bot_ready.is_set():
        initialize_bot()


# --------------------------------------------------
# MAIN
# --------------------------------------------------

if __name__ == "__main__":
    initialize_bot()

    app.run(
        host="0.0.0.0",
        port=PORT
        )
