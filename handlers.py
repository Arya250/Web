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
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

import database as db

from config import (
    ADMIN_ID,
    BOT_USERNAME,
    FILES_PER_PAGE,
    ADMIN_FILES_PER_PAGE,
    USERS_PER_PAGE,
    BROADCAST_DELAY
)

from keyboards import (
    main_keyboard,
    admin_keyboard,
    file_keyboard,
    pagination_keyboard,
    force_join_keyboard,
    delete_confirmation_keyboard,
    admin_delete_confirmation_keyboard,
    force_channel_keyboard,
    back_keyboard
)

from utils import (
    identify_file,
    generate_file_token,
    generate_share_link,
    generate_website_link,
    format_size,
    format_date,
    human_file_type,
    escape_html,
    is_valid_channel,
    extract_start_token
)


logger = logging.getLogger(__name__)


# ==========================================
# HELPERS
# ==========================================

def is_admin(user_id):
    return int(user_id) == int(ADMIN_ID)


async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except Exception:
        try:
            await query.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        except Exception:
            logger.exception("Could not edit or send message")


async def send_file_to_user(bot, chat_id, file):
    file_id = file["file_id"]
    caption = file.get("caption") or ""

    file_type = file.get("file_type")

    methods = {
        "document": bot.send_document,
        "video": bot.send_video,
        "photo": bot.send_photo,
        "audio": bot.send_audio,
        "voice": bot.send_voice,
        "video_note": bot.send_video_note,
        "animation": bot.send_animation,
        "sticker": bot.send_sticker
    }

    method = methods.get(file_type)

    if not method:
        raise ValueError("Unsupported file type")

    if file_type in ("sticker", "video_note"):
        return await method(
            chat_id=chat_id,
            **{
                "sticker" if file_type == "sticker"
                else "video_note": file_id
            }
        )

    return await method(
        chat_id=chat_id,
        **{
            {
                "document": "document",
                "video": "video",
                "photo": "photo",
                "audio": "audio",
                "voice": "voice",
                "animation": "animation"
            }[file_type]: file_id
        },
        caption=caption[:1024] if caption else None
    )


async def check_force_join(user_id, context):
    enabled = db.get_setting("force_join_enabled", False)

    if not enabled:
        return True

    channels = db.get_force_channels()

    if not channels:
        return True

    for channel in channels:
        chat_id = channel.get("chat_id")

        if not chat_id:
            continue

        try:
            member = await context.bot.get_chat_member(
                chat_id=chat_id,
                user_id=user_id
            )

            if member.status in ("left", "kicked"):
                return False

        except Exception:
            logger.warning(
                "Force join check failed for channel %s",
                chat_id,
                exc_info=True
            )
            return False

    return True


async def show_force_join(update, context):
    channels = db.get_force_channels()

    text = (
        "🔐 <b>Channel Subscription Required</b>\n\n"
        "Please join the required channel(s), "
        "then press Verify Joined."
    )

    if update.callback_query:
        await safe_edit(
            update.callback_query,
            text,
            force_join_keyboard(channels),
            "HTML"
        )
    else:
        await update.effective_message.reply_text(
            text,
            reply_markup=force_join_keyboard(channels),
            parse_mode="HTML"
        )


async def send_main_menu(update, context):
    user = update.effective_user

    text = (
        "☁️ <b>WELCOME TO DESI CLOUD</b>\n\n"
        "Your personal Telegram cloud storage.\n\n"
        "📁 Store your files\n"
        "🔗 Generate sharing links\n"
        "📥 Access your saved files\n\n"
        "Choose an option below."
    )

    if update.callback_query:
        await safe_edit(
            update.callback_query,
            text,
            main_keyboard(),
            "HTML"
        )
    else:
        await update.effective_message.reply_text(
            text,
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )


# ==========================================
# START COMMAND
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    db.register_user(user)

    # Handle shared file deep links.
    token = extract_start_token(update.effective_message.text)

    if token:
        if not await check_force_join(user.id, context):
            await show_force_join(update, context)
            context.user_data["pending_token"] = token
            return

        await deliver_shared_file(
            update,
            context,
            token
        )
        return

    if not await check_force_join(user.id, context):
        await show_force_join(update, context)
        return

    custom_message = db.get_setting(
        "start_message",
        "Welcome to DESI CLOUD!"
    )

    if is_admin(user.id):
        text = (
            f"{escape_html(custom_message)}\n\n"
            "👑 <b>Administrator Access</b>"
        )
    else:
        text = escape_html(custom_message)

    await update.effective_message.reply_text(
        text,
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )


# ==========================================
# SHARED FILE DELIVERY
# ==========================================

async def deliver_shared_file(update, context, token):
    user_id = update.effective_user.id

    file = db.get_file_by_token(token)

    if not file:
        await update.effective_message.reply_text(
            "❌ This file does not exist or has been deleted."
        )
        return

    if not file.get("sharing_enabled") or not file.get("is_public"):
        await update.effective_message.reply_text(
            "🔒 This sharing link is disabled."
        )
        return

    try:
        await send_file_to_user(
            context.bot,
            update.effective_chat.id,
            file
        )

        db.increment_file_views(file["id"])
        db.increment_file_downloads(file["id"])

    except Exception:
        logger.exception("Shared file delivery failed")

        await update.effective_message.reply_text(
            "❌ Unable to send this file right now."
        )


# ==========================================
# HELP / ABOUT
# ==========================================

async def help_command(update, context):
    text = db.get_setting(
        "help_message",
        "Send a file to upload it to your cloud."
    )

    await update.effective_message.reply_text(
        escape_html(text),
        reply_markup=back_keyboard(),
        parse_mode="HTML"
    )


async def about_command(update, context):
    text = db.get_setting(
        "about_message",
        "DESI CLOUD - Telegram File Storage."
    )

    await update.effective_message.reply_text(
        escape_html(text),
        reply_markup=back_keyboard(),
        parse_mode="HTML"
    )


# ==========================================
# FILE UPLOAD
# ==========================================

async def upload_command(update, context):
    if not await check_force_join(
        update.effective_user.id,
        context
    ):
        await show_force_join(update, context)
        return

    context.user_data["state"] = "upload"

    await update.effective_message.reply_text(
        "📤 <b>Upload File</b>\n\n"
        "Send the document, photo, video, audio or supported media.\n\n"
        "Maximum file size: "
        f"<b>{format_size(__import__('config').MAX_FILE_SIZE)}</b>",
        parse_mode="HTML"
    )


async def upload_file(update, context):
    message = update.effective_message
    user = update.effective_user

    file_data = identify_file(message)

    if not file_data:
        return False

    if file_data.get("error") == "FILE_TOO_LARGE":
        await message.reply_text(
            "❌ File exceeds the configured upload size limit."
        )
        return True

    token = generate_file_token()

    try:
        saved = db.save_file(
            token=token,
            file_id=file_data["file_id"],
            file_type=file_data["file_type"],
            file_name=file_data["file_name"],
            file_size=file_data["file_size"],
            mime_type=file_data["mime_type"],
            caption=file_data["caption"],
            uploaded_by=user.id
        )

        context.user_data.pop("state", None)

        await message.reply_text(
            "✅ <b>File uploaded successfully!</b>\n\n"
            f"📁 Name: {escape_html(saved['file_name'])}\n"
            f"📦 Size: {format_size(saved['file_size'])}\n"
            f"🆔 File ID: <code>{saved['id']}</code>",
            reply_markup=file_keyboard(saved["id"]),
            parse_mode="HTML"
        )

    except Exception:
        logger.exception("File upload database operation failed")

        await message.reply_text(
            "❌ Upload could not be saved. Please try again."
        )

    return True


# ==========================================
# MY FILES
# ==========================================

async def show_my_files(query, user_id, page=0):
    page = max(0, int(page))

    total = db.count_user_files(user_id)

    if total == 0:
        await safe_edit(
            query,
            "📁 <b>My Files</b>\n\n"
            "You haven't uploaded any files yet.",
            main_keyboard(),
            "HTML"
        )
        return

    files_list = db.get_user_files(
        user_id,
        page,
        FILES_PER_PAGE
    )

    lines = [
        "📁 <b>MY CLOUD FILES</b>\n",
        f"Total files: <b>{total}</b>\n"
    ]

    buttons = []

    for file in files_list:
        file_id = file["id"]
        name = escape_html(file["file_name"])

        lines.append(
            f"📄 <b>{name}</b>\n"
            f"   ID: <code>{file_id}</code> | "
            f"{format_size(file.get('file_size', 0))}"
        )

        buttons.append([
            InlineKeyboardButton(
                f"📂 {file['file_name'][:35]}",
                callback_data=f"file:details:{file_id}"
            )
        ])

    has_previous = page > 0
    has_next = (page + 1) * FILES_PER_PAGE < total

    navigation = pagination_keyboard(
        "myfiles",
        page,
        has_previous,
        has_next
    )

    buttons.extend(navigation.inline_keyboard)

    await safe_edit(
        query,
        "\n\n".join(lines),
        InlineKeyboardMarkup(buttons),
        "HTML"
    )


async def show_file_details(query, file_id, user_id):
    file = db.get_user_file(file_id, user_id)

    if not file:
        await query.answer(
            "File not found.",
            show_alert=True
        )
        return

    text = (
        "📁 <b>FILE DETAILS</b>\n\n"
        f"📄 Name: {escape_html(file['file_name'])}\n"
        f"🆔 ID: <code>{file['id']}</code>\n"
        f"📦 Size: {format_size(file.get('file_size', 0))}\n"
        f"📂 Type: {human_file_type(file['file_type'])}\n"
        f"👁 Views: {file.get('views', 0)}\n"
        f"📥 Downloads: {file.get('downloads', 0)}\n"
        f"🔗 Sharing: "
        f"{'Enabled' if file.get('sharing_enabled') else 'Disabled'}\n"
        f"📅 Uploaded: {format_date(file.get('created_at'))}"
    )

    await safe_edit(
        query,
        text,
        file_keyboard(file_id),
        "HTML"
    )


# ==========================================
# GENERATE LINK
# ==========================================

async def generate_link_command(update, context):
    context.user_data["state"] = "generate"

    await update.effective_message.reply_text(
        "🔗 <b>Generate Sharing Link</b>\n\n"
        "Choose a file from My Files to generate its link.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📁 My Files",
                    callback_data="myfiles:0"
                )
            ]
        ]),
        parse_mode="HTML"
    )


async def generate_file_link(query, file_id, user_id):
    file = db.get_user_file(file_id, user_id)

    if not file:
        await query.answer(
            "File not found.",
            show_alert=True
        )
        return

    if not file.get("sharing_enabled"):
        await query.answer(
            "Sharing is disabled for this file.",
            show_alert=True
        )
        return

    db.set_file_sharing(file_id, user_id, True)

    token = file["token"]

    bot_link = generate_share_link(token)
    website_link = generate_website_link(token)

    text = (
        "🔗 <b>YOUR FILE LINK</b>\n\n"
        f"📄 {escape_html(file['file_name'])}\n\n"
        f"🤖 Telegram Link:\n<code>{bot_link}</code>\n"
    )

    if website_link:
        text += (
            f"\n🌐 Website Link:\n"
            f"<code>{website_link}</code>\n"
        )

    await safe_edit(
        query,
        text,
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📤 Share Telegram Link",
                    url=bot_link
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=f"file:details:{file_id}"
                )
            ]
        ]),
        "HTML"
    )


# ==========================================
# FILE CALLBACKS
# ==========================================

async def handle_file_callback(query, parts, user_id, context):
    action = parts[1]
    file_id = int(parts[2])

    file = db.get_user_file(file_id, user_id)

    if not file:
        await query.answer(
            "File not found or access denied.",
            show_alert=True
        )
        return

    if action == "details":
        await show_file_details(
            query,
            file_id,
            user_id
        )

    elif action == "get":
        try:
            await send_file_to_user(
                context.bot,
                user_id,
                file
            )

            db.increment_file_downloads(file_id)

            await query.answer("File sent.")

        except Exception:
            logger.exception("File retrieval failed")
            await query.answer(
                "Unable to send file.",
                show_alert=True
            )

    elif action == "share":
        await generate_file_link(
            query,
            file_id,
            user_id
        )

    elif action == "sharing":
        enabled = not file.get("sharing_enabled", False)

        db.set_file_sharing(
            file_id,
            user_id,
            enabled
        )

        await query.answer(
            "Sharing enabled." if enabled else "Sharing disabled."
        )

        await show_file_details(
            query,
            file_id,
            user_id
        )

    elif action == "rename":
        context.user_data["state"] = "rename"
        context.user_data["rename_file_id"] = file_id

        await safe_edit(
            query,
            "✏️ <b>Rename File</b>\n\n"
            "Send the new file name.",
            None,
            "HTML"
        )

    elif action == "delete":
        await safe_edit(
            query,
            "⚠️ Are you sure you want to delete this file?",
            delete_confirmation_keyboard(file_id)
        )

    elif action == "confirmdelete":
        if db.delete_file(file_id, user_id):
            await safe_edit(
                query,
                "✅ File deleted successfully.",
                main_keyboard()
            )
        else:
            await query.answer(
                "Could not delete file.",
                show_alert=True
            )

    elif action == "canceldelete":
        await show_file_details(
            query,
            file_id,
            user_id
        )


# ==========================================
# ADMIN DASHBOARD
# ==========================================

async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "⛔ Access denied."
        )
        return

    await update.effective_message.reply_text(
        "👑 <b>DESI CLOUD ADMIN PANEL</b>\n\n"
        "Select an administration option.",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )


async def show_admin_statistics(query):
    stats = db.get_statistics()

    text = (
        "📊 <b>DESI CLOUD STATISTICS</b>\n\n"
        f"👥 Users: <b>{stats['total_users']}</b>\n"
        f"📁 Files: <b>{stats['total_files']}</b>\n"
        f"👁 Views: <b>{stats['total_views']}</b>\n"
        f"📥 Downloads: <b>{stats['total_downloads']}</b>\n"
        f"💾 Storage: <b>{format_size(stats['total_storage'])}</b>\n"
        f"🔗 Shared Files: <b>{stats['shared_files']}</b>\n"
        f"🔒 Disabled Files: <b>{stats['disabled_files']}</b>\n"
        f"📢 Force Join Channels: <b>{stats['total_channels']}</b>"
    )

    await safe_edit(
        query,
        text,
        back_keyboard("admin:home"),
        "HTML"
    )


async def show_admin_files(query, page=0):
    page = max(0, int(page))

    total = db.count_all_files()

    if total == 0:
        await safe_edit(
            query,
            "📁 No files found.",
            back_keyboard("admin:home")
        )
        return

    files_list = db.get_all_files(
        page,
        ADMIN_FILES_PER_PAGE
    )

    buttons = []

    lines = [
        "📁 <b>ALL CLOUD FILES</b>\n",
        f"Total: <b>{total}</b>\n"
    ]

    for file in files_list:
        file_id = file["id"]

        lines.append(
            f"📄 {escape_html(file['file_name'])}\n"
            f"ID: <code>{file_id}</code> | "
            f"Owner: <code>{file['uploaded_by']}</code>"
        )

        buttons.append([
            InlineKeyboardButton(
                f"🗑 Delete #{file_id}",
                callback_data=f"admin:delete:{file_id}"
            )
        ])

    navigation = pagination_keyboard(
        "admin:files",
        page,
        page > 0,
        (page + 1) * ADMIN_FILES_PER_PAGE < total
    )

    buttons.extend(navigation.inline_keyboard)

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Admin Menu",
            callback_data="admin:home"
        )
    ])

    await safe_edit(
        query,
        "\n\n".join(lines),
        InlineKeyboardMarkup(buttons),
        "HTML"
    )


async def show_admin_users(query, page=0):
    page = max(0, int(page))

    total = db.count_users()

    user_list = db.get_users_paginated(
        page,
        USERS_PER_PAGE
    )

    lines = [
        "👥 <b>REGISTERED USERS</b>\n",
        f"Total users: <b>{total}</b>\n"
    ]

    for user in user_list:
        lines.append(
            f"👤 {escape_html(user.get('first_name', 'Unknown'))}\n"
            f"ID: <code>{user['telegram_id']}</code>"
        )

    navigation = pagination_keyboard(
        "admin:users",
        page,
        page > 0,
        (page + 1) * USERS_PER_PAGE < total
    )

    await safe_edit(
        query,
        "\n\n".join(lines),
        navigation,
        "HTML"
    )


# ==========================================
# ADMIN FORCE JOIN
# ==========================================

async def show_force_channels_admin(query):
    channels = db.get_force_channels()

    enabled = db.get_setting(
        "force_join_enabled",
        False
    )

    text = (
        "🔐 <b>FORCE JOIN SETTINGS</b>\n\n"
        f"Status: <b>{'Enabled' if enabled else 'Disabled'}</b>\n"
        f"Channels: <b>{len(channels)}</b>\n\n"
        "Manage required channels below."
    )

    buttons = []

    for channel in channels:
        buttons.append([
            InlineKeyboardButton(
                f"🗑 {channel.get('channel_title') or channel.get('channel')}",
                callback_data=f"admin:removeforce:{channel['id']}"
            )
        ])

    buttons.extend([
        [
            InlineKeyboardButton(
                "➕ Add Channel",
                callback_data="admin:addforce"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Toggle Force Join",
                callback_data="admin:toggleforce"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Admin Menu",
                callback_data="admin:home"
            )
        ]
    ])

    await safe_edit(
        query,
        text,
        InlineKeyboardMarkup(buttons),
        "HTML"
    )


# ==========================================
# ADMIN CALLBACK ROUTER
# ==========================================

async def handle_admin_callback(query, context, parts):
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer(
            "Access denied.",
            show_alert=True
        )
        return

    action = parts[1]

    if action == "home":
        await safe_edit(
            query,
            "👑 <b>DESI CLOUD ADMIN PANEL</b>",
            admin_keyboard(),
            "HTML"
        )

    elif action == "close":
        context.user_data.clear()
        await safe_edit(query, "Admin panel closed.")

    elif action == "stats":
        await show_admin_statistics(query)

    elif action == "files":
        page = int(parts[2]) if len(parts) > 2 else 0
        await show_admin_files(query, page)

    elif action == "users":
        page = int(parts[2]) if len(parts) > 2 else 0
        await show_admin_users(query, page)

    elif action == "delete":
        file_id = int(parts[2])

        await safe_edit(
            query,
            f"⚠️ Delete file #{file_id}?",
            admin_delete_confirmation_keyboard(file_id)
        )

    elif action == "confirmdelete":
        file_id = int(parts[2])

        if db.admin_delete_file(file_id):
            await safe_edit(
                query,
                "✅ File deleted.",
                back_keyboard("admin:files:0")
            )
        else:
            await query.answer(
                "File not found.",
                show_alert=True
            )

    elif action == "force":
        await show_force_channels_admin(query)

    elif action == "toggleforce":
        enabled = db.get_setting(
            "force_join_enabled",
            False
        )

        db.set_setting(
            "force_join_enabled",
            not enabled
        )

        await show_force_channels_admin(query)

    elif action == "addforce":
        context.user_data["state"] = "add_force_channel"

        await safe_edit(
            query,
            "➕ <b>Add Force Join Channel</b>\n\n"
            "Send the public channel username.\n"
            "Example: <code>@YourChannel</code>",
            None,
            "HTML"
        )

    elif action == "removeforce":
        channel_id = int(parts[2])

        if db.remove_force_channel(channel_id):
            await query.answer("Channel removed.")
        else:
            await query.answer(
                "Channel not found.",
                show_alert=True
            )

        await show_force_channels_admin(query)

    elif action == "upload":
        context.user_data["state"] = "upload"

        await safe_edit(
            query,
            "📤 Send the file you want to upload.",
            None
        )

    elif action == "broadcast":
        context.user_data["state"] = "broadcast"

        await safe_edit(
            query,
            "📢 <b>Broadcast Message</b>\n\n"
            "Send the message you want to broadcast.\n"
            "Send /cancel to cancel.",
            None,
            "HTML"
        )

    elif action == "startmsg":
        context.user_data["state"] = "start_message"

        await safe_edit(
            query,
            "⚙️ Send the new start message.\n"
            "Send /cancel to cancel.",
            None
        )

    elif action == "starmsg":
        context.user_data["state"] = "star_message"

        await safe_edit(
            query,
            "⭐ Send the new star message.\n"
            "Send /cancel to cancel.",
            None
        )

    elif action == "settings":
        website = db.get_setting("website_enabled", True)
        force = db.get_setting("force_join_enabled", False)

        text = (
            "🛠 <b>BOT SETTINGS</b>\n\n"
            f"🌐 Website: {'Enabled' if website else 'Disabled'}\n"
            f"🔐 Force Join: {'Enabled' if force else 'Disabled'}"
        )

        await safe_edit(
            query,
            text,
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🌐 Toggle Website",
                        callback_data="admin:togglewebsite"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Admin Menu",
                        callback_data="admin:home"
                    )
                ]
            ]),
            "HTML"
        )

    elif action == "togglewebsite":
        enabled = db.get_setting("website_enabled", True)

        db.set_setting("website_enabled", not enabled)

        await query.answer(
            "Website setting updated."
        )

        website = db.get_setting("website_enabled", True)

        await safe_edit(
            query,
            f"🌐 Website: {'Enabled' if website else 'Disabled'}",
            back_keyboard("admin:settings")
        )


# ==========================================
# VERIFY FORCE JOIN
# ==========================================

async def verify_join(query, context):
    if await check_force_join(
        query.from_user.id,
        context
    ):
        await query.answer(
            "Verification successful!"
        )

        token = context.user_data.pop(
            "pending_token",
            None
        )

        if token:
            await deliver_shared_file(
                query,
                context,
                token
            )
        else:
            await safe_edit(
                query,
                "✅ <b>Verification successful!</b>\n\n"
                "Welcome to DESI CLOUD.",
                main_keyboard(),
                "HTML"
            )

    else:
        await query.answer(
            "Please join all required channels first.",
            show_alert=True
        )


# ==========================================
# BROADCAST
# ==========================================

async def perform_broadcast(message, context):
    user_records = db.get_all_user_ids()

    success = 0
    failed = 0

    status = await message.reply_text(
        "📢 Starting broadcast..."
    )

    for record in user_records:
        user_id = record["telegram_id"]

        try:
            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id
            )

            success += 1

        except Exception:
            failed += 1

        await asyncio.sleep(BROADCAST_DELAY)

    await status.edit_text(
        "📢 <b>Broadcast Completed</b>\n\n"
        f"✅ Successful: {success}\n"
        f"❌ Failed: {failed}",
        parse_mode="HTML"
    )


# ==========================================
# TEXT / MEDIA STATE ROUTER
# ==========================================

async def handle_user_message(update, context):
    message = update.effective_message
    user = update.effective_user

    db.register_user(user)

    state = context.user_data.get("state")

    if state == "upload":
        await upload_file(update, context)
        return

    if state == "rename":
        file_id = context.user_data.get("rename_file_id")

        new_name = (message.text or "").strip()

        if not new_name:
            await message.reply_text(
                "Please send a valid file name."
            )
            return

        if len(new_name) > 150:
            await message.reply_text(
                "File name is too long. Maximum 150 characters."
            )
            return

        if db.rename_file(
            file_id,
            user.id,
            new_name
        ):
            context.user_data.clear()

            await message.reply_text(
                "✅ File renamed successfully."
            )
        else:
            await message.reply_text(
                "❌ Could not rename this file."
            )

        return

    if state == "add_force_channel" and is_admin(user.id):
        channel = (message.text or "").strip()

        if not is_valid_channel(channel):
            await message.reply_text(
                "❌ Invalid channel username.\n"
                "Example: @YourChannel"
            )
            return

        try:
            chat = await context.bot.get_chat(channel)

            db.add_force_channel(
                chat_id=chat.id,
                channel_username=channel,
                channel_title=chat.title
            )

            context.user_data.pop("state", None)

            await message.reply_text(
                "✅ Force join channel added."
            )

        except Exception:
            logger.exception("Could not add force join channel")

            await message.reply_text(
                "❌ Could not access this channel.\n"
                "Make sure the bot is an administrator there."
            )

        return

    if state == "start_message" and is_admin(user.id):
        db.set_setting(
            "start_message",
            message.text or message.caption or ""
        )

        context.user_data.pop("state", None)

        await message.reply_text(
            "✅ Start message updated."
        )
        return

    if state == "star_message" and is_admin(user.id):
        db.set_setting(
            "star_message",
            message.text or message.caption or ""
        )

        context.user_data.pop("state", None)

        await message.reply_text(
            "✅ Star message updated."
        )
        return

    if state == "broadcast" and is_admin(user.id):
        if message.text and message.text.startswith("/"):
            await message.reply_text(
                "Broadcast cancelled."
            )
            context.user_data.pop("state", None)
            return

        context.user_data.pop("state", None)

        await perform_broadcast(
            message,
            context
        )
        return

    # Allow automatic file upload without pressing Upload.
    if identify_file(message):
        await upload_file(update, context)
        return

    if message.text:
        await message.reply_text(
            "Choose an option from the menu.",
            reply_markup=main_keyboard()
        )


# ==========================================
# CANCEL
# ==========================================

async def cancel(update, context):
    context.user_data.clear()

    await update.effective_message.reply_text(
        "❌ Operation cancelled.",
        reply_markup=main_keyboard()
    )


# ==========================================
# CALLBACK ROUTER
# ==========================================

async def callback_router(update, context):
    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    parts = data.split(":")

    user_id = query.from_user.id

    try:
        if data == "home":
            await query.answer()
            await send_main_menu(update, context)
            return

        if data == "verify_join":
            await verify_join(query, context)
            return

        if data == "upload":
            await query.answer()
            context.user_data["state"] = "upload"

            await safe_edit(
                query,
                "📤 Send the file you want to upload."
            )
            return

        if data == "generate":
            await query.answer()
            await safe_edit(
                query,
                "🔗 Select a file from My Files.",
                InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "📁 My Files",
                            callback_data="myfiles:0"
                        )
                    ]
                ])
            )
            return

        if data == "about":
            await query.answer()

            await safe_edit(
                query,
                escape_html(
                    db.get_setting("about_message", "")
                ),
                back_keyboard(),
                "HTML"
            )
            return

        if data == "help":
            await query.answer()

            await safe_edit(
                query,
                escape_html(
                    db.get_setting("help_message", "")
                ),
                back_keyboard(),
                "HTML"
            )
            return

        if data == "star":
            await query.answer()

            star_message = db.get_setting(
                "star_message",
                ""
            )

            await safe_edit(
                query,
                escape_html(
                    star_message or "⭐ Thank you for using DESI CLOUD!"
                ),
                back_keyboard(),
                "HTML"
            )
            return

        if data.startswith("myfiles:"):
            await query.answer()

            page = int(parts[1])

            await show_my_files(
                query,
                user_id,
                page
            )
            return

        if parts[0] == "file":
            await query.answer()

            await handle_file_callback(
                query,
                parts,
                user_id,
                context
            )
            return

        if parts[0] == "admin":
            await query.answer()

            await handle_admin_callback(
                query,
                context,
                parts
            )
            return

        await query.answer()

    except Exception:
        logger.exception(
            "Callback handling error: %s",
            data
        )

        try:
            await query.answer(
                "Something went wrong.",
                show_alert=True
            )
        except Exception:
            pass


# ==========================================
# REGISTER HANDLERS
# ==========================================

def register_handlers(application: Application):

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("about", about_command)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CommandHandler("upload", upload_command)
    )

    application.add_handler(
        CommandHandler("generate", generate_link_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_user_message
        )
    )

    logger.info("Telegram handlers registered.")
