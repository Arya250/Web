import os
import logging
import threading
import asyncio

from concurrent.futures import TimeoutError as FutureTimeoutError

from flask import Flask, request, jsonify

from telegram import Update
from telegram.ext import Application

import database as db
import handlers


# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

logger = logging.getLogger(__name__)


# ==================================================
# CONFIGURATION
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")

WEBHOOK_PATH = os.getenv(
    "WEBHOOK_PATH",
    "/telegram-webhook"
)

if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

WEBHOOK_URL = RENDER_EXTERNAL_URL + WEBHOOK_PATH

PORT = int(os.getenv("PORT", "10000"))

STARTUP_TIMEOUT = 90

WEBHOOK_TIMEOUT = 60


# ==================================================
# FLASK APPLICATION
# ==================================================

app = Flask(__name__)

bot_application = None

bot_ready = threading.Event()

startup_lock = threading.Lock()

startup_error = None


# ==================================================
# ASYNCIO EVENT LOOP
# ==================================================

bot_loop = None

loop_thread = None


def start_event_loop():
    """
    Start one permanent asyncio event loop
    for the Telegram application.
    """

    global bot_loop, loop_thread

    if bot_loop is not None and bot_loop.is_running():
        return

    bot_loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(bot_loop)

        logger.info(
            "Telegram asyncio event loop started."
        )

        bot_loop.run_forever()

    loop_thread = threading.Thread(
        target=run_loop,
        name="TelegramEventLoop",
        daemon=True
    )

    loop_thread.start()

    # Wait until the event loop is running.
    import time

    deadline = time.time() + 10

    while not bot_loop.is_running():
        if time.time() > deadline:
            raise RuntimeError(
                "Telegram event loop failed to start."
            )

        time.sleep(0.05)


def run_async(coroutine, timeout=60):
    """
    Execute coroutine on the permanent Telegram loop.
    """

    if bot_loop is None or not bot_loop.is_running():
        raise RuntimeError(
            "Telegram event loop is not running."
        )

    future = asyncio.run_coroutine_threadsafe(
        coroutine,
        bot_loop
    )

    return future.result(timeout=timeout)


# ==================================================
# CREATE TELEGRAM APPLICATION
# ==================================================

def create_bot_application():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    register_function = getattr(
        handlers,
        "register_handlers",
        None
    )

    if not callable(register_function):
        raise RuntimeError(
            "handlers.py must contain "
            "register_handlers(application)."
        )

    register_function(application)

    logger.info(
        "Telegram handlers registered."
    )

    return application


# ==================================================
# TELEGRAM STARTUP
# ==================================================

async def start_telegram_application():

    global bot_application

    await bot_application.initialize()

    logger.info(
        "Telegram application initialized."
    )

    await bot_application.start()

    logger.info(
        "Telegram application started."
    )

    await bot_application.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES
    )

    logger.info(
        "Telegram webhook configured successfully."
    )


# ==================================================
# INITIALIZE BOT
# ==================================================

def initialize_bot():

    global bot_application
    global startup_error

    if bot_ready.is_set():
        return True

    with startup_lock:

        if bot_ready.is_set():
            return True

        try:

            logger.info(
                "Initializing MongoDB..."
            )

            if not db.initialize_database():

                raise RuntimeError(
                    "MongoDB initialization failed."
                )

            if not RENDER_EXTERNAL_URL:

                raise RuntimeError(
                    "RENDER_EXTERNAL_URL is missing."
                )

            logger.info(
                "Initializing Telegram application..."
            )

            bot_application = create_bot_application()

            logger.info(
                "Starting Telegram event loop..."
            )

            start_event_loop()

            logger.info(
                "Starting Telegram application..."
            )

            run_async(
                start_telegram_application(),
                timeout=STARTUP_TIMEOUT
            )

            startup_error = None

            bot_ready.set()

            logger.info(
                "DESI CLOUD bot is ready."
            )

            return True

        except Exception as exc:

            startup_error = str(exc)

            logger.exception(
                "DESI CLOUD startup failed."
            )

            bot_ready.clear()

            return False


# ==================================================
# HEALTH CHECK
# ==================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({

        "project": "DESI CLOUD",

        "status": (
            "online"
            if bot_ready.is_set()
            else "starting"
        ),

        "database": db.is_database_ready(),

        "bot_ready": bot_ready.is_set()

    })


@app.route("/health", methods=["GET"])
def health():

    healthy = bot_ready.is_set()

    return jsonify({

        "project": "DESI CLOUD",

        "status": (
            "healthy"
            if healthy
            else "unhealthy"
        ),

        "database": db.is_database_ready(),

        "bot_ready": healthy,

        "error": startup_error

    }), 200 if healthy else 503


# ==================================================
# TELEGRAM WEBHOOK
# ==================================================

@app.route(
    WEBHOOK_PATH,
    methods=["POST"]
)
def telegram_webhook():

    if not bot_ready.is_set():

        return jsonify({

            "ok": False,

            "error": "Bot is not ready."

        }), 503

    # Verify Telegram webhook secret.
    if WEBHOOK_SECRET:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if received_secret != WEBHOOK_SECRET:

            return jsonify({

                "ok": False,

                "error": "Unauthorized."

            }), 403

    update_id = None

    try:

        payload = request.get_json(
            force=True
        )

        if not isinstance(payload, dict):

            return jsonify({

                "ok": False,

                "error": "Invalid JSON payload."

            }), 400

        update_id = payload.get("update_id")

        if update_id is None:

            return jsonify({

                "ok": False,

                "error": "Missing update_id."

            }), 400

        # Save update in MongoDB.
        db.save_webhook_update(
            update_id,
            payload
        )

        async def process_update():

            update = Update.de_json(
                payload,
                bot_application.bot
            )

            await bot_application.process_update(
                update
            )

        # Process on the SAME permanent event loop.
        run_async(
            process_update(),
            timeout=WEBHOOK_TIMEOUT
        )

        # Mark update completed.
        db.complete_webhook_update(
            update_id
        )

        return jsonify({

            "ok": True,

            "update_id": update_id

        }), 200

    except FutureTimeoutError:

        logger.exception(
            "Telegram update processing timed out."
        )

        if update_id is not None:

            try:

                db.retry_webhook_update(
                    update_id,
                    "Webhook processing timed out."
                )

            except Exception:

                logger.exception(
                    "Failed to schedule webhook retry."
                )

        return jsonify({

            "ok": False,

            "error": "Processing timeout."

        }), 500

    except Exception as exc:

        logger.exception(
            "Webhook processing failed."
        )

        if update_id is not None:

            try:

                db.retry_webhook_update(
                    update_id,
                    str(exc)
                )

            except Exception:

                logger.exception(
                    "Failed to schedule webhook retry."
                )

        return jsonify({

            "ok": False,

            "error": "Update processing failed."

        }), 500


# ==================================================
# STARTUP MIDDLEWARE
# ==================================================

@app.before_request
def ensure_started():

    if not bot_ready.is_set():

        success = initialize_bot()

        if not success:

            return jsonify({

                "ok": False,

                "status": "starting",

                "error": startup_error

            }), 503

    return None


# ==================================================
# MAIN
# ==================================================

if __name__ == "__main__":

    initialize_bot()

    app.run(

        host="0.0.0.0",

        port=PORT,

        debug=False

    )
