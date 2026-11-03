import logging
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from telegram.error import (
    TelegramError,
    RetryAfter,
    Forbidden
)

import database as db

from config import (
    ADMIN_ID,
    BOT_USERNAME,
    FILES_PER_PAGE,
    ADMIN_FILES_PER_PAGE,
    USERS_PER_PAGE,
    generate_file_token,
    BROADCAST_DELAY
)

from keyboards import (
    main_keyboard,
    admin_keyboard,
    file_keyboard,
    pagination_keyboard,
    force_join_keyboard,
    delete_confirmation_keyboard,
    admin_delete_confirmation_keyboard
)

from utils import (
    escape_html,
    format_size,
    format_date,
    generate_share_link,
    generate_website_link,
    identify_file,
    extract_start_token,
    human_file_type,
    is_valid_channel
)


logger = logging.getLogger(__name__)


# ==========================================
# AUTHORIZATION
# ==========================================

def is_admin(user_id):

    return user_id == ADMIN_ID


def admin_required(func):

    async def wrapper(update, context):

        user = update.effective_user

        if not user or not is_admin(user.id):

            if update.callback_query:

                await update.callback_query.answer(
                    "Access denied.",
                    show_alert=True
                )

            elif update.effective_message:

                await update.effective_message.reply_text(
                    "⛔ You are not authorized."
                )

            return

        return await func(update, context)

    return wrapper


# ==========================================
# FORCE JOIN
# ==========================================

async def check_force_join(bot, user_id):

    enabled = (
        db.get_setting(
            "force_join_enabled",
            "false"
        ) == "true"
    )

    if not enabled:
        return True

    channels = db.get_force_channels()

    for channel in channels:

        try:

            member = await bot.get_chat_member(
                chat_id=channel["channel"],
                user_id=user_id
            )

            if member.status in (
                "left",
                "kicked"
            ):

                return False

        except TelegramError:

            logger.exception(
                "Force Join membership check failed."
            )

            return False

    return True


async def send_force_join(message):

    channels = db.get_force_channels()

    await message.reply_text(

        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "🔐 JOIN REQUIRED\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Please join our channels to access "
        "DESI CLOUD.",

        reply_markup=force_join_keyboard(
            channels
        )

    )


async def verify_join(update, context):

    query = update.callback_query

    await query.answer()

    joined = await check_force_join(
        context.bot,
        query.from_user.id
    )

    if joined:

        await query.edit_message_text(
            "✅ Verification successful!\n\n"
            "You can now access DESI CLOUD.",
            reply_markup=main_keyboard()
        )

    else:

        await query.answer(
            "Please join all required channels first.",
            show_alert=True
        )


# ==========================================
# START COMMAND
# ==========================================

async def start(update, context):

    user = update.effective_user

    db.register_user(
        user.id,
        user.username,
        user.first_name
    )

    joined = await check_force_join(
        context.bot,
        user.id
    )

    if not joined:

        await send_force_join(
            update.effective_message
        )

        return

    # Shared file deep-link handling.

    token = extract_start_token(
        update.effective_message.text
    )

    if token:

        await deliver_shared_file(
            update,
            context,
            token
        )

        return

    message = db.get_setting(
        "start_message",
        "Welcome to DESI CLOUD!"
    )

    await update.effective_message.reply_text(
        message,
        reply_markup=main_keyboard()
    )


# ==========================================
# SHARED FILE DELIVERY
# ==========================================

async def deliver_shared_file(
    update,
    context,
    token
):

    file = db.get_file_by_token(token)

    if not file:

        await update.effective_message.reply_text(
            "❌ This file link is invalid, disabled "
            "or no longer available."
        )

        return

    user_id = update.effective_user.id

    if file["uploaded_by"] != user_id:

        if not file["is_public"]:

            await update.effective_message.reply_text(
                "⛔ You do not have permission "
                "to access this file."
            )

            return

    try:

        await send_file_to_user(
            context.bot,
            user_id,
            file
        )

        db.increment_file_views(
            file["id"]
        )

    except TelegramError:

        logger.exception(
            "Shared file delivery failed."
        )

        await update.effective_message.reply_text(
            "Unable to deliver this file right now."
        )


# ==========================================
# FILE DELIVERY
# ==========================================

async def send_file_to_user(
    bot,
    user_id,
    file
):

    file_id = file["file_id"]

    file_type = file["file_type"]

    caption = (
        f"☁️ <b>DESI CLOUD</b>\n\n"
        f"📁 {escape_html(file['file_name'])}\n"
        f"📦 {format_size(file['file_size'])}"
    )

    if file_type == "document":

        await bot.send_document(
            user_id,
            file_id,
            caption=caption,
            parse_mode="HTML"
        )

    elif file_type == "video":

        await bot.send_video(
            user_id,
            file_id,
            caption=caption,
            parse_mode="HTML"
        )

    elif file_type == "photo":

        await bot.send_photo(
            user_id,
            file_id,
            caption=caption,
            parse_mode="HTML"
        )

    elif file_type == "audio":

        await bot.send_audio(
            user_id,
            file_id,
            caption=caption,
            parse_mode="HTML"
        )

    elif file_type == "voice":

        await bot.send_voice(
            user_id,
            file_id,
            caption=caption
        )

    elif file_type == "video_note":

        await bot.send_video_note(
            user_id,
            file_id
        )

    elif file_type == "animation":

        await bot.send_animation(
            user_id,
            file_id,
            caption=caption,
            parse_mode="HTML"
        )

    elif file_type == "sticker":

        await bot.send_sticker(
            user_id,
            file_id
        )

    else:

        await bot.send_document(
            user_id,
            file_id,
            caption=caption
        )


# ==========================================
# UPLOAD SYSTEM
# ==========================================

async def upload_file(update, context):

    message = update.effective_message

    user = update.effective_user

    joined = await check_force_join(
        context.bot,
        user.id
    )

    if not joined:

        await send_force_join(message)

        return

    data = identify_file(message)

    if not data:

        return

    if data.get("error") == "FILE_TOO_LARGE":

        await message.reply_text(
            "❌ File exceeds the configured size limit."
        )

        return

    token = generate_file_token()

    try:

        saved = db.save_file(

            token=token,

            file_id=data["file_id"],

            file_type=data["file_type"],

            file_name=data["file_name"],

            file_size=data["file_size"],

            mime_type=data["mime_type"],

            caption=data["caption"],

            uploaded_by=user.id

        )

    except Exception:

        logger.exception(
            "File metadata could not be saved."
        )

        await message.reply_text(
            "❌ Upload registration failed. "
            "Please try again."
        )

        return

    share_link = generate_share_link(
        token
    )

    await message.reply_text(

        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "☁️ DESI CLOUD\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"

        "✅ File uploaded successfully!\n\n"

        f"📁 {escape_html(saved['file_name'])}\n"

        f"📦 {format_size(saved['file_size'])}\n"

        f"🆔 File ID: {saved['id']}\n\n"

        "🔗 Your sharing link:\n"

        f"<code>{escape_html(share_link)}</code>",

        parse_mode="HTML",

        reply_markup=InlineKeyboardMarkup([

            [

                InlineKeyboardButton(
                    "🔗 Open Share Link",
                    url=share_link
                )

            ],

            [

                InlineKeyboardButton(
                    "📁 My Files",
                    callback_data="myfiles:0"
                )

            ]

        ])

    )


# ==========================================
# MY FILES
# ==========================================

async def show_my_files(
    query,
    user_id,
    page=0
):

    files = db.get_user_files(
        user_id,
        page,
        FILES_PER_PAGE
    )

    total = db.count_user_files(
        user_id
    )

    if not files and page > 0:

        await query.answer(
            "No more files.",
            show_alert=True
        )

        return

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "📁 MY CLOUD FILES\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )

    buttons = []

    for file in files:

        name = escape_html(
            file["file_name"]
        )

        text += (

            f"📄 <b>{name}</b>\n"

            f"Type: {human_file_type(file['file_type'])}\n"

            f"Size: {format_size(file['file_size'])}\n"

            f"Views: {file['views']}\n"

            f"Uploaded: {format_date(file['created_at'])}\n"

            f"Sharing: "
            f"{'Enabled' if file['sharing_enabled'] else 'Disabled'}\n\n"

        )

        buttons.append([

            InlineKeyboardButton(

                f"📁 {file['file_name'][:30]}",

                callback_data=f"file:open:{file['id']}"

            )

        ])

    navigation = []

    if page > 0:

        navigation.append(
            InlineKeyboardButton(
                "⬅️ Previous",
                callback_data=f"myfiles:{page - 1}"
            )
        )

    if (page + 1) * FILES_PER_PAGE < total:

        navigation.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"myfiles:{page + 1}"
            )
        )

    if navigation:

        buttons.append(navigation)

    buttons.append([

        InlineKeyboardButton(
            "🏠 Main Menu",
            callback_data="home"
        )

    ])

    await query.edit_message_text(

        text or "No files uploaded yet.",

        parse_mode="HTML",

        reply_markup=InlineKeyboardMarkup(
            buttons
        )

    )


async def show_file_details(
    query,
    file_id,
    user_id
):

    file = db.get_user_file(
        file_id,
        user_id
    )

    if not file:

        await query.answer(
            "File not found or access denied.",
            show_alert=True
        )

        return

    text = (

        "☁️ <b>FILE DETAILS</b>\n\n"

        f"📁 {escape_html(file['file_name'])}\n"

        f"📦 {format_size(file['file_size'])}\n"

        f"📄 {human_file_type(file['file_type'])}\n"

        f"👁 Views: {file['views']}\n"

        f"📅 {format_date(file['created_at'])}\n"

        f"🔗 Sharing: "
        f"{'Enabled' if file['sharing_enabled'] else 'Disabled'}"

    )

    await query.edit_message_text(

        text,

        parse_mode="HTML",

        reply_markup=file_keyboard(file_id)

    )


# ==========================================
# FILE CALLBACKS
# ==========================================

async def handle_file_callback(
    query,
    context,
    parts
):

    action = parts[1]

    file_id = int(parts[2])

    user_id = query.from_user.id

    file = db.get_user_file(
        file_id,
        user_id
    )

    if not file:

        await query.answer(
            "File not found or access denied.",
            show_alert=True
        )

        return

    if action == "open":

        await show_file_details(
            query,
            file_id,
            user_id
        )

        return

    if action == "get":

        await query.answer(
            "Sending file..."
        )

        await send_file_to_user(
            context.bot,
            user_id,
            file
        )

        db.increment_file_views(
            file_id
        )

        return

    if action == "share":

        if not file["sharing_enabled"]:

            await query.answer(
                "Sharing is disabled.",
                show_alert=True
            )

            return

        link = generate_share_link(
            file["token"]
        )

        await query.message.reply_text(
            f"🔗 Your sharing link:\n\n{link}"
        )

        await query.answer()

        return

    if action == "sharing":

        enabled = not file["sharing_enabled"]

        db.set_file_sharing(
            file_id,
            user_id,
            enabled
        )

        await query.answer(
            "Sharing enabled." if enabled
            else "Sharing disabled."
        )

        file = db.get_user_file(
            file_id,
            user_id
        )

        await show_file_details(
            query,
            file_id,
            user_id
        )

        return

    if action == "rename":

        db.set_admin_state(
            user_id,
            "rename_file",
            {"file_id": file_id}
        )

        await query.message.reply_text(
            "✏️ Send the new file name."
        )

        await query.answer()

        return

    if action == "delete":

        await query.edit_message_text(

            "⚠️ Are you sure you want to delete this file?",

            reply_markup=delete_confirmation_keyboard(
                file_id
            )

        )

        return

    if action == "confirmdelete":

        result = db.delete_file(
            file_id,
            user_id
        )

        if result:

            await query.edit_message_text(
                "✅ File deleted successfully."
            )

        else:

            await query.answer(
                "File could not be deleted.",
                show_alert=True
            )

        return

    if action == "canceldelete":

        await query.edit_message_text(
            "❌ Deletion cancelled."
        )

        return


# ==========================================
# USER TEXT INPUT
# ==========================================

async def handle_user_text(update, context):

    user_id = update.effective_user.id

    state = db.get_admin_state(
        user_id
    )

    if not state:
        return

    if state["state"] == "rename_file":

        new_name = update.effective_message.text.strip()

        if not new_name or len(new_name) > 200:

            await update.effective_message.reply_text(
                "Please enter a valid file name."
            )

            return

        file_id = state["data"]["file_id"]

        result = db.rename_file(
            file_id,
            user_id,
            new_name
        )

        db.clear_admin_state(
            user_id
        )

        if result:

            await update.effective_message.reply_text(
                "✅ File renamed successfully."
            )

        else:

            await update.effective_message.reply_text(
                "File could not be renamed."
            )


# ==========================================
# MAIN CALLBACK ROUTER
# ==========================================

async def callback_router(update, context):

    query = update.callback_query

    data = query.data or ""

    await query.answer()

    if data == "home":

        await query.edit_message_text(
            "☁️ DESI CLOUD MAIN MENU",
            reply_markup=main_keyboard()
        )

        return

    if data == "upload":

        await query.message.reply_text(
            "📤 Send a document, video, photo, audio "
            "or another supported file."
        )

        return

    if data.startswith("myfiles:"):

        page = int(data.split(":")[1])

        await show_my_files(
            query,
            query.from_user.id,
            page
        )

        return

    if data.startswith("file:"):

        await handle_file_callback(
            query,
            context,
            data.split(":")
        )

        return

    if data == "star":

        message = db.get_setting(
            "star_message",
            "⭐ Thank you!"
        )

        await query.message.reply_text(
            message
        )

        return

    if data == "about":

        await query.message.reply_text(
            db.get_setting(
                "about_message",
                "About DESI CLOUD"
            )
        )

        return

    if data == "help":

        await query.message.reply_text(
            db.get_setting(
                "help_message",
                "DESI CLOUD Help"
            )
        )

        return

    if data == "generate":

        await query.message.reply_text(
            "📁 Open My Files and select a file "
            "to generate its sharing link."
        )

        return

    if data == "verify_join":

        await verify_join(
            update,
            context
        )

        return

    if data.startswith("admin:"):

        if not is_admin(query.from_user.id):

            await query.answer(
                "Access denied.",
                show_alert=True
            )

            return

        await admin_callback_router(
            update,
            context
        )

        return


# ==========================================
# ADMIN PANEL
# ==========================================

@admin_required
async def admin_command(update, context):

    await update.effective_message.reply_text(

        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "☁️ DESI CLOUD\n"
        "ADMIN DASHBOARD\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯",

        reply_markup=admin_keyboard()

    )


async def admin_callback_router(update, context):

    query = update.callback_query

    data = query.data

    if not is_admin(query.from_user.id):

        await query.answer(
            "Unauthorized.",
            show_alert=True
        )

        return

    if data == "admin:close":

        await query.message.delete()

        return

    if data == "admin:stats":

        stats = db.get_statistics()

        text = (

            "📊 <b>DESI CLOUD STATISTICS</b>\n\n"

            f"👥 Users: {stats['total_users']}\n"

            f"📁 Files: {stats['total_files']}\n"

            f"👁 Views: {stats['total_views']}\n"

            f"💾 Storage Metadata: "
            f"{format_size(stats['total_storage'])}\n"

            f"🔗 Shared Files: {stats['shared_files']}\n"

            f"🔒 Disabled: {stats['disabled_files']}\n"

            f"📢 Channels: {stats['total_channels']}"

        )

        await query.edit_message_text(

            text,

            parse_mode="HTML",

            reply_markup=InlineKeyboardMarkup([

                [

                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin:home"
                    )

                ]

            ])

        )

        return

    if data == "admin:home":

        await query.edit_message_text(

            "☁️ DESI CLOUD ADMIN DASHBOARD",

            reply_markup=admin_keyboard()

        )

        return

    if data.startswith("admin:files:"):

        page = int(data.split(":")[2])

        files = db.get_all_files(
            page,
            ADMIN_FILES_PER_PAGE
        )

        buttons = []

        text = "📁 <b>ALL FILES</b>\n\n"

        for file in files:

            text += (

                f"🆔 {file['id']}\n"

                f"📄 {escape_html(file['file_name'])}\n"

                f"👤 Owner: {file['uploaded_by']}\n"

                f"👁 Views: {file['views']}\n\n"

            )

            buttons.append([

                InlineKeyboardButton(

                    f"Manage #{file['id']}",

                    callback_data=f"admin:manage:{file['id']}"

                )

            ])

        nav = []

        if page > 0:

            nav.append(
                InlineKeyboardButton(
                    "⬅️ Previous",
                    callback_data=f"admin:files:{page - 1}"
                )
            )

        if len(files) == ADMIN_FILES_PER_PAGE:

            nav.append(
                InlineKeyboardButton(
                    "Next ➡️",
                    callback_data=f"admin:files:{page + 1}"
                )
            )

        if nav:
            buttons.append(nav)

        buttons.append([

            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin:home"
            )

        ])

        await query.edit_message_text(

            text or "No files found.",

            parse_mode="HTML",

            reply_markup=InlineKeyboardMarkup(
                buttons
            )

        )

        return

    if data.startswith("admin:manage:"):

        file_id = int(data.split(":")[2])

        file = db.get_file_by_id(
            file_id
        )

        if not file:

            await query.answer(
                "File not found.",
                show_alert=True
            )

            return

        text = (

            "📁 <b>ADMIN FILE MANAGEMENT</b>\n\n"

            f"ID: {file['id']}\n"

            f"Name: {escape_html(file['file_name'])}\n"

            f"Owner: {file['uploaded_by']}\n"

            f"Type: {file['file_type']}\n"

            f"Size: {format_size(file['file_size'])}\n"

            f"Views: {file['views']}\n"

            f"Sharing: {file['sharing_enabled']}"

        )

        buttons = [

            [

                InlineKeyboardButton(
                    "🔗 Generate Link",
                    callback_data=f"admin:share:{file_id}"
                )

            ],

            [

                InlineKeyboardButton(
                    "🔒 Toggle Sharing",
                    callback_data=f"admin:sharing:{file_id}"
                )

            ],

            [

                InlineKeyboardButton(
                    "🗑 Delete",
                    callback_data=f"admin:delete:{file_id}"
                )

            ],

            [

                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="admin:files:0"
                )

            ]

        ]

        await query.edit_message_text(

            text,

            parse_mode="HTML",

            reply_markup=InlineKeyboardMarkup(buttons)

        )

        return

    if data.startswith("admin:share:"):

        file_id = int(data.split(":")[2])

        file = db.get_file_by_id(
            file_id
        )

        if not file:

            await query.answer(
                "File not found.",
                show_alert=True
            )

            return

        link = generate_share_link(
            file["token"]
        )

        await query.message.reply_text(
            f"🔗 {link}"
        )

        return

    if data.startswith("admin:sharing:"):

        file_id = int(data.split(":")[2])

        file = db.get_file_by_id(
            file_id
        )

        if not file:
            return

        enabled = not file["sharing_enabled"]

        with db.get_db() as cursor:

            cursor.execute(
                """
                UPDATE files

                SET sharing_enabled = %s

                WHERE id = %s
                """,

                (enabled, file_id)
            )

        await query.answer(
            "Sharing updated."
        )

        await admin_callback_router(
            update,
            context
        )

        return

    if data.startswith("admin:delete:"):

        file_id = int(data.split(":")[2])

        await query.edit_message_text(

            "⚠️ Confirm permanent file deletion.",

            reply_markup=admin_delete_confirmation_keyboard(
                file_id
            )

        )

        return

    if data.startswith("admin:confirmdelete:"):

        file_id = int(data.split(":")[2])

        result = db.admin_delete_file(
            file_id
        )

        await query.edit_message_text(

            "✅ File deleted."
            if result else
            "File not found.",

            reply_markup=admin_keyboard()

        )

        return

    if data.startswith("admin:users:"):

        page = int(data.split(":")[2])

        users = db.get_users_paginated(
            page,
            USERS_PER_PAGE
        )

        text = "👥 <b>REGISTERED USERS</b>\n\n"

        for user in users:

            text += (

                f"🆔 {user['user_id']}\n"

                f"👤 {escape_html(user['first_name'])}\n"

                f"Username: "
                f"@{escape_html(user['username'])}\n\n"

            )

        buttons = []

        nav = []

        if page > 0:

            nav.append(
                InlineKeyboardButton(
                    "⬅️ Previous",
                    callback_data=f"admin:users:{page - 1}"
                )
            )

        if len(users) == USERS_PER_PAGE:

            nav.append(
                InlineKeyboardButton(
                    "Next ➡️",
                    callback_data=f"admin:users:{page + 1}"
                )
            )

        if nav:
            buttons.append(nav)

        buttons.append([

            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin:home"
            )

        ])

        await query.edit_message_text(

            text or "No users.",

            parse_mode="HTML",

            reply_markup=InlineKeyboardMarkup(buttons)

        )

        return

    if data == "admin:force":

        channels = db.get_force_channels()

        enabled = (
            db.get_setting(
                "force_join_enabled",
                "false"
            ) == "true"
        )

        text = (

            "🔐 <b>FORCE JOIN SETTINGS</b>\n\n"

            f"Status: {'Enabled' if enabled else 'Disabled'}\n\n"

        )

        buttons = [

            [

                InlineKeyboardButton(
                    "➕ Add Channel",
                    callback_data="admin:addchannel"
                )

            ],

            [

                InlineKeyboardButton(
                    "🔄 Toggle Force Join",
                    callback_data="admin:toggleforce"
                )

            ]

        ]

        for channel in channels:

            buttons.append([

                InlineKeyboardButton(

                    f"❌ Remove {channel['channel']}",

                    callback_data=f"admin:removechannel:{channel['id']}"

                )

            ])

            text += f"📢 {escape_html(channel['channel'])}\n"

        buttons.append([

            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin:home"
            )

        ])

        await query.edit_message_text(

            text,

            parse_mode="HTML",

            reply_markup=InlineKeyboardMarkup(buttons)

        )

        return

    if data == "admin:toggleforce":

        current = db.get_setting(
            "force_join_enabled",
            "false"
        )

        new_value = (
            "false" if current == "true"
            else "true"
        )

        db.set_setting(
            "force_join_enabled",
            new_value
        )

        await query.answer(
            "Force Join setting updated."
        )

        await admin_callback_router(
            update,
            context
        )

        return

    if data == "admin:addchannel":

        db.set_admin_state(
            ADMIN_ID,
            "add_channel"
        )

        await query.message.reply_text(
            "Send the public channel username, "
            "for example @DesiCloudUpdates"
        )

        return

    if data.startswith("admin:removechannel:"):

        channel_id = int(data.split(":")[2])

        db.remove_force_channel(
            channel_id
        )

        await query.answer(
            "Channel removed."
        )

        await admin_callback_router(
            update,
            context
        )

        return

    if data == "admin:startmsg":

        db.set_admin_state(
            ADMIN_ID,
            "set_start_message"
        )

        await query.message.reply_text(
            "Send the new start message."
        )

        return

    if data == "admin:starmsg":

        db.set_admin_state(
            ADMIN_ID,
            "set_star_message"
        )

        await query.message.reply_text(
            "Send the new star message."
        )

        return

    if data == "admin:settings":

        await query.edit_message_text(

            "🛠 BOT SETTINGS\n\n"
            "Configure your messages and "
            "Force Join settings from this panel.",

            reply_markup=InlineKeyboardMarkup([

                [

                    InlineKeyboardButton(
                        "Start Message",
                        callback_data="admin:startmsg"
                    ),

                    InlineKeyboardButton(
                        "Star Message",
                        callback_data="admin:starmsg"
                    )

                ],

                [

                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin:home"
                    )

                ]

            ])

        )

        return

    if data == "admin:broadcast":

        db.set_admin_state(
            ADMIN_ID,
            "broadcast"
        )

        await query.message.reply_text(

            "📢 Send the announcement or media "
            "message you want to broadcast.\n\n"

            "A confirmation will be requested "
            "before sending."

        )

        return


# ==========================================
# ADMIN TEXT INPUT
# ==========================================

async def handle_admin_input(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    state = db.get_admin_state(
        ADMIN_ID
    )

    if not state:
        return

    current_state = state["state"]

    message = update.effective_message

    if current_state == "set_start_message":

        db.set_setting(
            "start_message",
            message.text or message.caption or ""
        )

        db.clear_admin_state(
            ADMIN_ID
        )

        await message.reply_text(
            "✅ Start message updated."
        )

        return

    if current_state == "set_star_message":

        db.set_setting(
            "star_message",
            message.text or message.caption or ""
        )

        db.clear_admin_state(
            ADMIN_ID
        )

        await message.reply_text(
            "✅ Star message updated."
        )

        return

    if current_state == "add_channel":

        channel = message.text.strip()

        if not is_valid_channel(channel):

            await message.reply_text(
                "❌ Invalid public channel username."
            )

            return

        try:

            chat = await context.bot.get_chat(
                channel
            )

            bot_member = await context.bot.get_chat_member(
                chat.id,
                context.bot.id
            )

            if bot_member.status != "administrator":

                await message.reply_text(
                    "❌ Please make the bot an administrator "
                    "in this channel first."
                )

                return

            db.add_force_channel(
                channel
            )

            db.clear_admin_state(
                ADMIN_ID
            )

            await message.reply_text(
                "✅ Channel added successfully."
            )

        except TelegramError:

            await message.reply_text(
                "❌ Cannot access this channel. "
                "Check its username and bot permissions."
            )

        return

    if current_state == "broadcast":

        db.set_admin_state(
            ADMIN_ID,
            "broadcast_confirm",
            {
                "chat_id": message.chat_id,
                "message_id": message.message_id
            }
        )

        await message.reply_text(

            "⚠️ Confirm broadcast?\n\n"

            "The message will be copied to all "
            "registered users.",

            reply_markup=InlineKeyboardMarkup([

                [

                    InlineKeyboardButton(
                        "✅ Confirm",
                        callback_data="admin:broadcast_confirm"
                    ),

                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="admin:broadcast_cancel"
                    )

                ]

            ])

        )

        return


# ==========================================
# BROADCAST
# ==========================================

async def perform_broadcast(update, context):

    query = update.callback_query

    if not is_admin(query.from_user.id):

        await query.answer(
            "Unauthorized.",
            show_alert=True
        )

        return

    state = db.get_admin_state(
        ADMIN_ID
    )

    if not state or state["state"] != "broadcast_confirm":

        await query.answer(
            "Broadcast session expired.",
            show_alert=True
        )

        return

    if query.data == "admin:broadcast_cancel":

        db.clear_admin_state(
            ADMIN_ID
        )

        await query.edit_message_text(
            "❌ Broadcast cancelled."
        )

        return

    if query.data != "admin:broadcast_confirm":
        return

    payload = state["data"]

    chat_id = payload["chat_id"]

    message_id = payload["message_id"]

    users = db.get_users_paginated(
        0,
        1000000
    )

    await query.edit_message_text(
        "📢 Broadcast started..."
    )

    success = 0

    failed = 0

    for user in users:

        try:

            await context.bot.copy_message(

                chat_id=user["user_id"],

                from_chat_id=chat_id,

                message_id=message_id

            )

            success += 1

        except RetryAfter as error:

            await asyncio.sleep(
                error.retry_after + 1
            )

            try:

                await context.bot.copy_message(

                    chat_id=user["user_id"],

                    from_chat_id=chat_id,

                    message_id=message_id

                )

                success += 1

            except TelegramError:

                failed += 1

        except Forbidden:

            failed += 1

        except TelegramError:

            failed += 1

        await asyncio.sleep(
            BROADCAST_DELAY
        )

    db.clear_admin_state(
        ADMIN_ID
    )

    await context.bot.send_message(

        chat_id=ADMIN_ID,

        text=(

            "✅ <b>BROADCAST COMPLETED</b>\n\n"

            f"👥 Total users: {len(users)}\n"

            f"✅ Successful: {success}\n"

            f"❌ Failed: {failed}"

        ),

        parse_mode="HTML"

    )


# ==========================================
# HANDLER REGISTRATION
# ==========================================

def register_handlers(application: Application):

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            perform_broadcast,
            pattern=r"^admin:broadcast_(confirm|cancel)$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    application.add_handler(

        MessageHandler(

            filters.ALL & ~filters.COMMAND,

            handle_admin_input,

            block=False

        ),

        group=0

    )

    application.add_handler(

        MessageHandler(

            (
                filters.Document.ALL
                | filters.VIDEO
                | filters.PHOTO
                | filters.AUDIO
                | filters.VOICE
                | filters.VIDEO_NOTE
                | filters.ANIMATION
                | filters.Sticker.ALL
            ),

            upload_file

        ),

        group=1

    )

    application.add_handler(

        MessageHandler(

            filters.TEXT & ~filters.COMMAND,

            handle_user_text

        ),

        group=2

    )

    logger.info(
        "Telegram handlers registered."
    )
