import os
import json
import uuid
import asyncio
import logging
import threading
import re

import psycopg2
import psycopg2.extras

from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.error import TelegramError

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).strip().rstrip("/")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

WEBHOOK_PATH = "/telegram-webhook"

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is missing")

if WEBHOOK_SECRET and not re.fullmatch(
    r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET
):
    raise RuntimeError("Invalid WEBHOOK_SECRET format")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("AMR_CLOUD")


# =========================================================
# GLOBAL VARIABLES
# =========================================================

app = Flask(__name__)

bot_application = None
bot_loop = None
bot_ready = threading.Event()

bot_username = None

startup_error = None


# =========================================================
# DATABASE
# =========================================================

def db_execute(
    query,
    params=None,
    fetchone=False,
    fetchall=False
):

    connection = psycopg2.connect(
        DATABASE_URL,
        connect_timeout=15
    )

    try:

        with connection:

            with connection.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cursor:

                cursor.execute(query, params or ())

                if fetchone:
                    return cursor.fetchone()

                if fetchall:
                    return cursor.fetchall()

                return None

    finally:

        connection.close()


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    logger.info("Initializing database...")

    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS files (
            id SERIAL PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_name TEXT,
            caption TEXT,
            views INTEGER DEFAULT 0,
            uploaded_by BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS force_channels (
            id SERIAL PRIMARY KEY,
            channel TEXT UNIQUE NOT NULL
        )
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Dedicated table avoids conflicts with the old admin_states table.

    db_execute("""
        CREATE TABLE IF NOT EXISTS amr_admin_states (
            admin_id BIGINT PRIMARY KEY,
            state TEXT,
            data JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db_execute("""
        ALTER TABLE amr_admin_states
        ADD COLUMN IF NOT EXISTS data JSONB
        NOT NULL DEFAULT '{}'::jsonb
    """)

    db_execute("""
        CREATE TABLE IF NOT EXISTS processed_updates (
            update_id BIGINT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    logger.info("Database initialized successfully")


# =========================================================
# ADMIN STATE MANAGEMENT
# =========================================================

def set_state(admin_id, state, data=None):

    db_execute("""
        INSERT INTO amr_admin_states
        (admin_id, state, data, updated_at)

        VALUES (%s, %s, %s::jsonb, CURRENT_TIMESTAMP)

        ON CONFLICT (admin_id)

        DO UPDATE SET
            state = EXCLUDED.state,
            data = EXCLUDED.data,
            updated_at = CURRENT_TIMESTAMP
    """, (
        int(admin_id),
        state,
        json.dumps(data or {})
    ))


def get_state(admin_id):

    row = db_execute("""
        SELECT state, data
        FROM amr_admin_states
        WHERE admin_id = %s
    """, (int(admin_id),), fetchone=True)

    if not row:
        return None, {}

    return row["state"], row["data"] or {}


def clear_state(admin_id):

    db_execute("""
        DELETE FROM amr_admin_states
        WHERE admin_id = %s
    """, (int(admin_id),))


# =========================================================
# SETTINGS
# =========================================================

def set_setting(key, value):

    db_execute("""
        INSERT INTO settings (key, value)
        VALUES (%s, %s)

        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value
    """, (key, str(value)))


def get_setting(key, default=""):

    row = db_execute("""
        SELECT value
        FROM settings
        WHERE key = %s
    """, (key,), fetchone=True)

    return row["value"] if row else default


# =========================================================
# USERS
# =========================================================

def save_user(user):

    if not user:
        return

    db_execute("""
        INSERT INTO users
        (user_id, username, first_name)

        VALUES (%s, %s, %s)

        ON CONFLICT (user_id)

        DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
    """, (
        user.id,
        user.username,
        user.first_name
    ))


def get_users():

    return db_execute("""
        SELECT user_id
        FROM users
    """, fetchall=True)


# =========================================================
# ADMIN
# =========================================================

def is_admin(user_id):

    return user_id is not None and int(user_id) == ADMIN_ID


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📁 My Files",
                callback_data="my_files"
            )
        ],

        [
            InlineKeyboardButton(
                "⭐ Star Message",
                callback_data="star_message"
            )
        ]

    ])


def admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📤 Upload File",
                callback_data="upload"
            ),

            InlineKeyboardButton(
                "📁 Manage Files",
                callback_data="files"
            )
        ],

        [
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="broadcast"
            ),

            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="stats"
            )
        ],

        [
            InlineKeyboardButton(
                "⚙️ Start Message",
                callback_data="start_message"
            ),

            InlineKeyboardButton(
                "⭐ Star Message",
                callback_data="set_star_message"
            )
        ],

        [
            InlineKeyboardButton(
                "🔐 Force Join",
                callback_data="force_join"
            )
        ],

        [
            InlineKeyboardButton(
                "❌ Close",
                callback_data="close"
            )
        ]

    ])


def back_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin"
            )
        ]

    ])


# =========================================================
# FORCE JOIN
# =========================================================

def get_channels():

    rows = db_execute("""
        SELECT channel
        FROM force_channels
        ORDER BY id
    """, fetchall=True)

    return [row["channel"] for row in rows]


async def check_join(user_id, context):

    channels = get_channels()

    for channel in channels:

        try:

            member = await context.bot.get_chat_member(
                chat_id=channel,
                user_id=user_id
            )

            if member.status in ("left", "kicked"):

                return False

        except TelegramError:

            logger.exception(
                "Force join check failed: %s",
                channel
            )

            return False

    return True


def join_keyboard(channels):

    buttons = []

    for channel in channels:

        username = channel.replace("@", "")

        buttons.append([

            InlineKeyboardButton(
                "📢 Join Channel",
                url=f"https://t.me/{username}"
            )

        ])

    buttons.append([

        InlineKeyboardButton(
            "✅ Verify Membership",
            callback_data="verify_join"
        )

    ])

    return InlineKeyboardMarkup(buttons)


# =========================================================
# FILE MANAGEMENT
# =========================================================

def save_file(
    file_id,
    file_type,
    file_name,
    caption,
    user_id
):

    token = uuid.uuid4().hex[:20]

    db_execute("""
        INSERT INTO files
        (
            token,
            file_id,
            file_type,
            file_name,
            caption,
            uploaded_by
        )

        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        token,
        file_id,
        file_type,
        file_name,
        caption,
        user_id
    ))

    return token


def get_file(token):

    return db_execute("""
        SELECT *
        FROM files
        WHERE token = %s
    """, (token,), fetchone=True)


def get_file_list():

    return db_execute("""
        SELECT *
        FROM files
        ORDER BY id DESC
        LIMIT 30
    """, fetchall=True)


def file_link(token):

    if not bot_username:
        raise RuntimeError("Bot username is not ready")

    return (
        f"https://t.me/{bot_username}"
        f"?start=file_{token}"
    )


# =========================================================
# SEND FILE
# =========================================================

async def send_file(update, context, token):

    user = update.effective_user

    if not user:
        return

    user_id = user.id

    channels = get_channels()

    if channels:

        joined = await check_join(user_id, context)

        if not joined:

            await context.bot.send_message(
                chat_id=user_id,
                text="🔒 Join our channels before accessing this file.",
                reply_markup=join_keyboard(channels)
            )

            return

    file_data = get_file(token)

    if not file_data:

        await context.bot.send_message(
            chat_id=user_id,
            text="❌ File not found or expired."
        )

        return

    caption = (
        file_data["caption"]
        or file_data["file_name"]
        or "AMR CLOUD"
    )

    try:

        if file_data["file_type"] == "document":

            await context.bot.send_document(
                chat_id=user_id,
                document=file_data["file_id"],
                caption=caption
            )

        elif file_data["file_type"] == "video":

            await context.bot.send_video(
                chat_id=user_id,
                video=file_data["file_id"],
                caption=caption
            )

        elif file_data["file_type"] == "photo":

            await context.bot.send_photo(
                chat_id=user_id,
                photo=file_data["file_id"],
                caption=caption
            )

        else:

            await context.bot.send_message(
                chat_id=user_id,
                text="Unsupported file type."
            )

            return

        db_execute("""
            UPDATE files
            SET views = views + 1
            WHERE token = %s
        """, (token,))

    except TelegramError:

        logger.exception("File delivery failed")

        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Unable to send this file."
        )


# =========================================================
# START COMMAND
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    save_user(user)

    if context.args:

        argument = context.args[0]

        if argument.startswith("file_"):

            token = argument[len("file_"):]

            await send_file(
                update,
                context,
                token
            )

            return

    channels = get_channels()

    if channels:

        joined = await check_join(user.id, context)

        if not joined:

            await update.message.reply_text(
                "🔒 Please join all required channels first.",
                reply_markup=join_keyboard(channels)
            )

            return

    message = get_setting(
        "start_message",
        "☁️ Welcome to AMR CLOUD!\n\n"
        "Your files, securely accessible."
    )

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard()
    )


# =========================================================
# ADMIN COMMAND
# =========================================================

async def admin_command(update, context):

    user = update.effective_user

    if not is_admin(user.id):

        await update.message.reply_text(
            "⛔ Access denied."
        )

        return

    clear_state(ADMIN_ID)

    await update.message.reply_text(
        "⚙️ AMR CLOUD ADMIN PANEL",
        reply_markup=admin_keyboard()
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(update, context):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user_id = query.from_user.id
    action = query.data

    # -----------------------------------------------------
    # USER ACTIONS
    # -----------------------------------------------------

    if action == "verify_join":

        if await check_join(user_id, context):

            await query.message.reply_text(
                "✅ Membership verified!\n\nSend /start again."
            )

        else:

            await query.message.reply_text(
                "❌ You have not joined all required channels."
            )

        return

    if action == "my_files":

        await query.message.reply_text(
            "📁 Use your saved file links to access files."
        )

        return

    if action == "star_message":

        message = get_setting(
            "star_message",
            "⭐ Welcome to AMR CLOUD!\n\n"
            "Thank you for using our service."
        )

        await query.message.reply_text(message)

        return

    # -----------------------------------------------------
    # ADMIN PROTECTION
    # -----------------------------------------------------

    if not is_admin(user_id):

        return

    # -----------------------------------------------------
    # ADMIN PANEL
    # -----------------------------------------------------

    if action == "admin":

        clear_state(ADMIN_ID)

        await query.message.reply_text(
            "⚙️ AMR CLOUD ADMIN PANEL",
            reply_markup=admin_keyboard()
        )

    elif action == "upload":

        set_state(ADMIN_ID, "upload")

        await query.message.reply_text(
            "📤 Send a document, video or photo.\n\n"
            "Send /cancel to cancel.",
            reply_markup=back_keyboard()
        )

    elif action == "files":

        files = get_file_list()

        if not files:

            await query.message.reply_text(
                "📁 No files uploaded yet.",
                reply_markup=back_keyboard()
            )

            return

        for item in files:

            name = item["file_name"] or "Unnamed File"

            keyboard = InlineKeyboardMarkup([

                [
                    InlineKeyboardButton(
                        "🔗 Share Link",
                        callback_data=f"share_{item['token']}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "🗑 Delete",
                        callback_data=f"delete_{item['token']}"
                    )
                ]

            ])

            await query.message.reply_text(
                f"📁 {name}\n"
                f"👁 Views: {item['views']}\n"
                f"🆔 {item['token']}",
                reply_markup=keyboard
            )

    elif action.startswith("share_"):

        token = action[len("share_"):]

        if not get_file(token):

            await query.message.reply_text(
                "❌ File not found."
            )

            return

        await query.message.reply_text(
            f"🔗 File Link:\n\n{file_link(token)}",
            reply_markup=back_keyboard()
        )

    elif action.startswith("delete_"):

        token = action[len("delete_"):]

        db_execute("""
            DELETE FROM files
            WHERE token = %s
        """, (token,))

        await query.message.reply_text(
            "✅ File deleted.",
            reply_markup=back_keyboard()
        )

    elif action == "broadcast":

        set_state(ADMIN_ID, "broadcast")

        await query.message.reply_text(
            "📢 Send the message to broadcast.\n\n"
            "Send /cancel to cancel."
        )

    elif action == "start_message":

        set_state(ADMIN_ID, "start_message")

        await query.message.reply_text(
            "⚙️ Send the new Start Message."
        )

    elif action == "set_star_message":

        set_state(ADMIN_ID, "star_message")

        await query.message.reply_text(
            "⭐ Send the new Star Message."
        )

    elif action == "force_join":

        channels = get_channels()

        channel_text = (
            "\n".join(channels)
            if channels
            else "No channels added."
        )

        keyboard = InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "➕ Add Channel",
                    callback_data="add_channel"
                )
            ],

            [
                InlineKeyboardButton(
                    "➖ Remove Channel",
                    callback_data="remove_channel"
                )
            ],

            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="admin"
                )
            ]

        ])

        await query.message.reply_text(
            "🔐 FORCE JOIN CHANNELS\n\n" + channel_text,
            reply_markup=keyboard
        )

    elif action == "add_channel":

        set_state(ADMIN_ID, "add_channel")

        await query.message.reply_text(
            "Send the public channel username.\n\n"
            "Example: @MyChannel"
        )

    elif action == "remove_channel":

        set_state(ADMIN_ID, "remove_channel")

        await query.message.reply_text(
            "Send the channel username to remove."
        )

    elif action == "stats":

        users = db_execute(
            "SELECT COUNT(*) AS total FROM users",
            fetchone=True
        )

        files = db_execute(
            "SELECT COUNT(*) AS total FROM files",
            fetchone=True
        )

        views = db_execute(
            "SELECT COALESCE(SUM(views), 0) AS total FROM files",
            fetchone=True
        )

        await query.message.reply_text(
            "📊 AMR CLOUD STATISTICS\n\n"
            f"👥 Users: {users['total']}\n"
            f"📁 Files: {files['total']}\n"
            f"👁 Views: {views['total']}",
            reply_markup=back_keyboard()
        )

    elif action == "close":

        await query.message.delete()


# =========================================================
# ADMIN MESSAGE HANDLER
# =========================================================

async def message_handler(update, context):

    user = update.effective_user

    if not user:
        return

    if not is_admin(user.id):

        save_user(user)
        return

    message = update.effective_message

    if not message:
        return

    state, data = get_state(ADMIN_ID)

    if not state:
        return

    if message.text and message.text.strip() == "/cancel":

        clear_state(ADMIN_ID)

        await message.reply_text(
            "❌ Operation cancelled.",
            reply_markup=admin_keyboard()
        )

        return

    # -----------------------------------------------------
    # UPLOAD FILE
    # -----------------------------------------------------

    if state == "upload":

        file_id = None
        file_type = None
        file_name = None

        caption = message.caption or ""

        if message.document:

            file_id = message.document.file_id
            file_type = "document"
            file_name = message.document.file_name or "Document"

        elif message.video:

            file_id = message.video.file_id
            file_type = "video"
            file_name = message.video.file_name or "Video"

        elif message.photo:

            file_id = message.photo[-1].file_id
            file_type = "photo"
            file_name = "Photo"

        else:

            await message.reply_text(
                "❌ Send a document, video or photo."
            )

            return

        token = save_file(
            file_id,
            file_type,
            file_name,
            caption,
            ADMIN_ID
        )

        clear_state(ADMIN_ID)

        await message.reply_text(
            "✅ FILE UPLOADED SUCCESSFULLY!\n\n"
            f"📁 Name: {file_name}\n\n"
            f"🔗 Share Link:\n{file_link(token)}",
            reply_markup=admin_keyboard()
        )

    # -----------------------------------------------------
    # START MESSAGE
    # -----------------------------------------------------

    elif state == "start_message":

        text = message.text or message.caption

        if not text:

            await message.reply_text(
                "Please send a text message."
            )

            return

        set_setting("start_message", text)

        clear_state(ADMIN_ID)

        await message.reply_text(
            "✅ Start Message updated.",
            reply_markup=admin_keyboard()
        )

    # -----------------------------------------------------
    # STAR MESSAGE
    # -----------------------------------------------------

    elif state == "star_message":

        text = message.text or message.caption

        if not text:

            await message.reply_text(
                "Please send a text message."
            )

            return

        set_setting("star_message", text)

        clear_state(ADMIN_ID)

        await message.reply_text(
            "⭐ Star Message updated.",
            reply_markup=admin_keyboard()
        )

    # -----------------------------------------------------
    # ADD FORCE JOIN CHANNEL
    # -----------------------------------------------------

    elif state == "add_channel":

        channel = (message.text or "").strip()

        if not channel.startswith("@"):

            await message.reply_text(
                "❌ Enter a public username like @MyChannel."
            )

            return

        try:

            chat = await context.bot.get_chat(channel)

            await context.bot.get_chat_member(
                chat_id=chat.id,
                user_id=context.bot.id
            )

            db_execute("""
                INSERT INTO force_channels (channel)
                VALUES (%s)

                ON CONFLICT (channel) DO NOTHING
            """, (channel,))

            clear_state(ADMIN_ID)

            await message.reply_text(
                "✅ Force Join channel added.",
                reply_markup=admin_keyboard()
            )

        except TelegramError:

            logger.exception("Unable to add force join channel")

            await message.reply_text(
                "❌ Bot cannot access this channel. "
                "Make the bot an administrator and try again."
            )

    # -----------------------------------------------------
    # REMOVE FORCE JOIN CHANNEL
    # -----------------------------------------------------

    elif state == "remove_channel":

        channel = (message.text or "").strip()

        db_execute("""
            DELETE FROM force_channels
            WHERE channel = %s
        """, (channel,))

        clear_state(ADMIN_ID)

        await message.reply_text(
            "✅ Channel removed.",
            reply_markup=admin_keyboard()
        )

    # -----------------------------------------------------
    # BROADCAST
    # -----------------------------------------------------

    elif state == "broadcast":

        clear_state(ADMIN_ID)

        users = get_users()

        sent = 0
        failed = 0

        await message.reply_text(
            f"📢 Broadcasting to {len(users)} users..."
        )

        for row in users:

            try:

                await message.copy(
                    chat_id=row["user_id"]
                )

                sent += 1

            except TelegramError:

                failed += 1

            await asyncio.sleep(0.05)

        await message.reply_text(
            "✅ BROADCAST COMPLETED\n\n"
            f"✔️ Sent: {sent}\n"
            f"❌ Failed: {failed}",
            reply_markup=admin_keyboard()
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    logger.error(
        "Telegram handler error",
        exc_info=context.error
    )


# =========================================================
# BOT INITIALIZATION
# =========================================================

def run_bot():

    global bot_application
    global bot_loop
    global bot_username
    global startup_error

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    bot_loop = loop

    application = Application.builder().token(
        BOT_TOKEN
    ).build()

    bot_application = application

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            message_handler
        )
    )

    application.add_error_handler(error_handler)

    async def initialize():

        global bot_username

        await application.initialize()

        await application.start()

        me = await application.bot.get_me()

        bot_username = me.username

        webhook_url = (
            RENDER_EXTERNAL_URL + WEBHOOK_PATH
        )

        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False
        )

        logger.info(
            "Webhook configured: %s",
            webhook_url
        )

        logger.info(
            "Bot is ready: @%s",
            bot_username
        )

        bot_ready.set()

    try:

        loop.run_until_complete(initialize())

        loop.run_forever()

    except Exception as error:

        startup_error = str(error)

        bot_ready.clear()

        logger.exception("Bot startup/runtime failed")

    finally:

        bot_ready.clear()

        try:

            if application.running:

                loop.run_until_complete(
                    application.stop()
                )

            loop.run_until_complete(
                application.shutdown()
            )

        except Exception:

            logger.exception("Bot shutdown error")

        loop.close()


# =========================================================
# FLASK ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "service": "AMR CLOUD",
        "status": "online",
        "bot_ready": bot_ready.is_set()
    })


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "service": "AMR CLOUD",
        "bot_ready": bot_ready.is_set(),
        "startup_error": startup_error
    })


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():

    if not bot_ready.is_set():

        return jsonify({
            "ok": False,
            "error": "Bot is not ready"
        }), 503

    if not bot_application or not bot_loop:

        return jsonify({
            "ok": False,
            "error": "Bot application unavailable"
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

        data = request.get_json(silent=True)

        if not isinstance(data, dict):

            return jsonify({
                "ok": False,
                "error": "Invalid JSON"
            }), 400

        update = Update.de_json(
            data,
            bot_application.bot
        )

        # IMPORTANT:
        # The Flask request thread and Telegram event loop
        # are different threads. Never directly call
        # asyncio.Queue.put_nowait() from Flask.
        #
        # run_coroutine_threadsafe safely schedules the
        # update on the bot's event loop.

        future = asyncio.run_coroutine_threadsafe(
            bot_application.update_queue.put(update),
            bot_loop
        )

        # Wait briefly to make sure the update was queued.
        future.result(timeout=10)

        return jsonify({
            "ok": True
        }), 200

    except Exception:

        logger.exception("Webhook update failed")

        return jsonify({
            "ok": False,
            "error": "Webhook processing failed"
        }), 500


# =========================================================
# STARTUP
# =========================================================

init_db()

bot_thread = threading.Thread(
    target=run_bot,
    name="telegram-bot",
    daemon=True
)

bot_thread.start()


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
        )
