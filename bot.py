import os
import json
import uuid
import asyncio
import logging
import threading
from datetime import datetime
from functools import wraps

import psycopg2
import psycopg2.extras

from flask import Flask, request, jsonify, redirect
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "")

WEBHOOK_PATH = "/telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID missing")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL missing")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

bot_application = None
bot_loop = None
bot_ready = threading.Event()


# =========================================================
# DATABASE CONNECTION
# =========================================================

def db_execute(query, params=None, fetchone=False, fetchall=False):

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

    # Separate table avoids the old admin_states schema conflict.
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
        SELECT value FROM settings
        WHERE key = %s
    """, (key,), fetchone=True)

    return row["value"] if row else default


# =========================================================
# USER MANAGEMENT
# =========================================================

def save_user(user):

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
        SELECT user_id FROM users
    """, fetchall=True)


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):

    return int(user_id) == ADMIN_ID


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
        ],

        [
            InlineKeyboardButton(
                "📢 Updates",
                url="https://t.me/"
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

    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⬅️ Back",
            callback_data="admin"
        )
    ]])


# =========================================================
# FORCE JOIN
# =========================================================

def get_channels():

    rows = db_execute("""
        SELECT channel FROM force_channels
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

        except Exception as error:

            logger.warning(
                "Force join check failed for %s: %s",
                channel,
                error
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
            "✅ Joined - Verify",
            callback_data="verify_join"
        )
    ])

    return InlineKeyboardMarkup(buttons)


# =========================================================
# FILE FUNCTIONS
# =========================================================

def save_file(file_id, file_type, file_name, caption, user_id):

    token = uuid.uuid4().hex[:20]

    db_execute("""
        INSERT INTO files
        (token, file_id, file_type, file_name, caption, uploaded_by)
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
        SELECT * FROM files
        WHERE token = %s
    """, (token,), fetchone=True)


def get_file_list():

    return db_execute("""
        SELECT * FROM files
        ORDER BY id DESC
        LIMIT 30
    """, fetchall=True)


def file_link(token):

    return f"https://t.me/{get_bot_username()}?start=file_{token}"


_bot_username = None


def get_bot_username():

    return _bot_username


# =========================================================
# START COMMAND
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    save_user(user)

    if context.args:

        argument = context.args[0]

        if argument.startswith("file_"):

            token = argument.replace("file_", "", 1)

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
                "🔒 To access AMR CLOUD, please join our channels first.",
                reply_markup=join_keyboard(channels)
            )

            return

    message = get_setting(
        "start_message",
        "☁️ Welcome to AMR CLOUD!\n\nYour files, securely accessible."
    )

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard()
    )


# =========================================================
# SEND FILE
# =========================================================

async def send_file(update, context, token):

    user_id = update.effective_user.id

    channels = get_channels()

    if channels:

        joined = await check_join(user_id, context)

        if not joined:

            message = (
                "🔒 Please join our channels before accessing this file."
            )

            if update.callback_query:

                await update.callback_query.message.reply_text(
                    message,
                    reply_markup=join_keyboard(channels)
                )

            elif update.message:

                await update.message.reply_text(
                    message,
                    reply_markup=join_keyboard(channels)
                )

            return

    file = get_file(token)

    if not file:

        if update.message:

            await update.message.reply_text(
                "❌ File not found or expired."
            )

        return

    db_execute("""
        UPDATE files
        SET views = views + 1
        WHERE token = %s
    """, (token,))

    caption = file["caption"] or file["file_name"] or "AMR CLOUD"

    try:

        if file["file_type"] == "document":

            await context.bot.send_document(
                chat_id=user_id,
                document=file["file_id"],
                caption=caption
            )

        elif file["file_type"] == "video":

            await context.bot.send_video(
                chat_id=user_id,
                video=file["file_id"],
                caption=caption
            )

        elif file["file_type"] == "photo":

            await context.bot.send_photo(
                chat_id=user_id,
                photo=file["file_id"],
                caption=caption
            )

        else:

            await context.bot.send_message(
                chat_id=user_id,
                text="Unsupported file type."
            )

    except Exception:

        logger.exception("File delivery failed")

        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Unable to send this file."
        )


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_command(update, context):

    if not is_admin(update.effective_user.id):

        await update.message.reply_text("⛔ Access denied.")
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

    await query.answer()

    user_id = query.from_user.id
    action = query.data

    if action == "verify_join":

        if await check_join(user_id, context):

            await query.message.reply_text(
                "✅ Verification successful!\n\nSend /start again."
            )

        else:

            await query.message.reply_text(
                "❌ You have not joined all required channels yet."
            )

        return

    if action == "my_files":

        await query.message.reply_text(
            "📁 Your files are available through their original links."
        )

        return

    if action == "star_message":

        message = get_setting(
            "star_message",
            "⭐ Welcome to AMR CLOUD!\n\nThank you for using our service."
        )

        await query.message.reply_text(message)

        return

    if not is_admin(user_id):

        await query.answer(
            "⛔ Admin access only.",
            show_alert=True
        )

        return

    if action == "admin":

        clear_state(ADMIN_ID)

        await query.message.reply_text(
            "⚙️ ADMIN PANEL",
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

        for file in files:

            name = file["file_name"] or "Unnamed File"

            keyboard = InlineKeyboardMarkup([

                [
                    InlineKeyboardButton(
                        "🔗 Share Link",
                        callback_data=f"share_{file['token']}"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "🗑 Delete",
                        callback_data=f"delete_{file['token']}"
                    )
                ]

            ])

            await query.message.reply_text(
                f"📁 {name}\n"
                f"👁 Views: {file['views']}\n"
                f"🆔 {file['token']}",
                reply_markup=keyboard
            )

    elif action.startswith("share_"):

        token = action.replace("share_", "", 1)

        link = file_link(token)

        await query.message.reply_text(
            f"🔗 Your file link:\n\n{link}",
            reply_markup=back_keyboard()
        )

    elif action.startswith("delete_"):

        token = action.replace("delete_", "", 1)

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
            "📢 Send the message you want to broadcast.\n\n"
            "Send /cancel to cancel."
        )

    elif action == "start_message":

        set_state(ADMIN_ID, "start_message")

        await query.message.reply_text(
            "⚙️ Send your new Start Message."
        )

    elif action == "set_star_message":

        set_state(ADMIN_ID, "star_message")

        await query.message.reply_text(
            "⭐ Send your new Star Message."
        )

    elif action == "force_join":

        channels = get_channels()

        text = "🔐 FORCE JOIN CHANNELS\n\n"

        if channels:

            text += "\n".join(channels)

        else:

            text += "No channels added."

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
            text,
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
            "Send the channel username you want to remove."
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

        text = (
            "📊 AMR CLOUD STATISTICS\n\n"
            f"👥 Users: {users['total']}\n"
            f"📁 Files: {files['total']}\n"
            f"👁 Total Views: {views['total']}"
        )

        await query.message.reply_text(
            text,
            reply_markup=back_keyboard()
        )

    elif action == "close":

        await query.message.delete()


# =========================================================
# MESSAGE HANDLER
# =========================================================

async def message_handler(update, context):

    user = update.effective_user

    if not user:
        return

    if user.id != ADMIN_ID:

        save_user(user)
        return

    state, data = get_state(ADMIN_ID)

    if not state:
        return

    message = update.message

    if not message:
        return

    if message.text == "/cancel":

        clear_state(ADMIN_ID)

        await message.reply_text(
            "❌ Operation cancelled.",
            reply_markup=admin_keyboard()
        )

        return

    # -----------------------------------------------------
    # FILE UPLOAD
    # -----------------------------------------------------

    if state == "upload":

        file_id = None
        file_type = None
        file_name = None
        caption = message.caption or ""

        if message.document:

            file_id = message.document.file_id
            file_type = "document"
            file_name = message.document.file_name

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
                "❌ Please send a document, video or photo."
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

        link = file_link(token)

        await message.reply_text(
            "✅ FILE UPLOADED SUCCESSFULLY!\n\n"
            f"📁 Name: {file_name}\n"
            f"🔗 Share Link:\n{link}",
            reply_markup=admin_keyboard()
        )

    # -----------------------------------------------------
    # START MESSAGE
    # -----------------------------------------------------

    elif state == "start_message":

        set_setting("start_message", message.text or message.caption or "")

        clear_state(ADMIN_ID)

        await message.reply_text(
            "✅ Start Message updated.",
            reply_markup=admin_keyboard()
        )

    # -----------------------------------------------------
    # STAR MESSAGE
    # -----------------------------------------------------

    elif state == "star_message":

        set_setting("star_message", message.text or message.caption or "")

        clear_state(ADMIN_ID)

        await message.reply_text(
            "⭐ Star Message updated.",
            reply_markup=admin_keyboard()
        )

    # -----------------------------------------------------
    # FORCE JOIN ADD
    # -----------------------------------------------------

    elif state == "add_channel":

        channel = (message.text or "").strip()

        if not channel.startswith("@"):

            await message.reply_text(
                "❌ Please use a public channel username, e.g. @MyChannel"
            )

            return

        try:

            chat = await context.bot.get_chat(channel)

            await context.bot.get_chat_member(
                chat.id,
                context.bot.id
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

        except Exception:

            await message.reply_text(
                "❌ Unable to access channel. Make sure the bot is an admin."
            )

    # -----------------------------------------------------
    # FORCE JOIN REMOVE
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

            except Exception:

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

    logger.exception(
        "Telegram update error",
        exc_info=context.error
    )


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

def run_bot():

    global bot_application
    global bot_loop
    global _bot_username

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)

    bot_loop = loop

    async def initialize():

        global _bot_username

        bot_application = Application.builder().token(
            BOT_TOKEN
        ).build()

        bot_application.add_handler(
            CommandHandler("start", start)
        )

        bot_application.add_handler(
            CommandHandler("admin", admin_command)
        )

        bot_application.add_handler(
            CallbackQueryHandler(callback_handler)
        )

        bot_application.add_handler(
            MessageHandler(
                filters.ALL & ~filters.COMMAND,
                message_handler
            )
        )

        bot_application.add_error_handler(error_handler)

        await bot_application.initialize()

        await bot_application.start()

        me = await bot_application.bot.get_me()

        _bot_username = me.username

        webhook_url = (
            RENDER_EXTERNAL_URL + WEBHOOK_PATH
        )

        await bot_application.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False
        )

        globals()["bot_application"] = bot_application

        bot_ready.set()

        logger.info(
            "Bot ready: @%s",
            me.username
        )

    loop.run_until_complete(initialize())

    loop.run_forever()


# =========================================================
# FLASK WEBHOOK
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online",
        "service": "AMR CLOUD"
    })


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "bot_ready": bot_ready.is_set(),
        "database": "configured"
    })


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():

    if not bot_ready.is_set():

        return jsonify({
            "error": "Bot not ready"
        }), 503

    if WEBHOOK_SECRET:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if received_secret != WEBHOOK_SECRET:

            return jsonify({
                "error": "Unauthorized"
            }), 403

    try:

        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            bot_application.bot
        )

        bot_application.update_queue.put_nowait(update)

        return jsonify({
            "ok": True
        })

    except Exception:

        logger.exception("Webhook processing failed")

        return jsonify({
            "error": "Webhook error"
        }), 500


# =========================================================
# STARTUP
# =========================================================

init_db()

bot_thread = threading.Thread(
    target=run_bot,
    daemon=True
)

bot_thread.start()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
        )
