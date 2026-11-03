from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ==========================================
# MAIN USER MENU
# ==========================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📁 My Files",
                callback_data="myfiles:0"
            ),
            InlineKeyboardButton(
                "📤 Upload File",
                callback_data="upload"
            )
        ],
        [
            InlineKeyboardButton(
                "🔗 Generate Link",
                callback_data="generate"
            )
        ],
        [
            InlineKeyboardButton(
                "⭐ Star Message",
                callback_data="star"
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ About",
                callback_data="about"
            ),
            InlineKeyboardButton(
                "⚙️ Help",
                callback_data="help"
            )
        ]
    ])


# ==========================================
# ADMIN MENU
# ==========================================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📤 Upload File",
                callback_data="admin:upload"
            ),
            InlineKeyboardButton(
                "📁 Manage Files",
                callback_data="admin:files:0"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 Users",
                callback_data="admin:users:0"
            ),
            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="admin:stats"
            )
        ],
        [
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="admin:broadcast"
            ),
            InlineKeyboardButton(
                "🔐 Force Join",
                callback_data="admin:force"
            )
        ],
        [
            InlineKeyboardButton(
                "⚙️ Start Message",
                callback_data="admin:startmsg"
            ),
            InlineKeyboardButton(
                "⭐ Star Message",
                callback_data="admin:starmsg"
            )
        ],
        [
            InlineKeyboardButton(
                "🛠 Bot Settings",
                callback_data="admin:settings"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Close Panel",
                callback_data="admin:close"
            )
        ]
    ])


# ==========================================
# FILE MANAGEMENT MENU
# ==========================================

def file_keyboard(file_id):
    file_id = int(file_id)

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 Share",
                callback_data=f"file:share:{file_id}"
            ),
            InlineKeyboardButton(
                "📥 Get File",
                callback_data=f"file:get:{file_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Rename",
                callback_data=f"file:rename:{file_id}"
            ),
            InlineKeyboardButton(
                "🔒 Sharing",
                callback_data=f"file:sharing:{file_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 Delete",
                callback_data=f"file:delete:{file_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back to My Files",
                callback_data="myfiles:0"
            )
        ]
    ])


# ==========================================
# PAGINATION
# ==========================================

def pagination_keyboard(
    prefix,
    page,
    has_previous,
    has_next
):
    buttons = []
    row = []

    if has_previous:
        row.append(
            InlineKeyboardButton(
                "⬅️ Previous",
                callback_data=f"{prefix}:{page - 1}"
            )
        )

    if has_next:
        row.append(
            InlineKeyboardButton(
                "Next ➡️",
                callback_data=f"{prefix}:{page + 1}"
            )
        )

    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(
            "🏠 Main Menu",
            callback_data="home"
        )
    ])

    return InlineKeyboardMarkup(buttons)


# ==========================================
# FORCE JOIN MENU
# ==========================================

def force_join_keyboard(channels):
    buttons = []

    for channel in channels:
        username = (
            channel.get("channel_username")
            or channel.get("channel")
            or ""
        )

        username = username.lstrip("@")

        if username:
            buttons.append([
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=f"https://t.me/{username}"
                )
            ])

    buttons.append([
        InlineKeyboardButton(
            "✅ Verify Joined",
            callback_data="verify_join"
        )
    ])

    return InlineKeyboardMarkup(buttons)


# ==========================================
# DELETE CONFIRMATION
# ==========================================

def delete_confirmation_keyboard(file_id):
    file_id = int(file_id)

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Confirm Delete",
                callback_data=f"file:confirmdelete:{file_id}"
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"file:canceldelete:{file_id}"
            )
        ]
    ])


# ==========================================
# ADMIN DELETE CONFIRMATION
# ==========================================

def admin_delete_confirmation_keyboard(file_id):
    file_id = int(file_id)

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Confirm",
                callback_data=f"admin:confirmdelete:{file_id}"
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="admin:files:0"
            )
        ]
    ])


# ==========================================
# FORCE JOIN ADMIN CONTROLS
# ==========================================

def force_channel_keyboard(channels):
    buttons = []

    for channel in channels:
        channel_id = channel.get("id")

        username = (
            channel.get("channel_username")
            or channel.get("channel")
            or "Private Channel"
        )

        buttons.append([
            InlineKeyboardButton(
                f"🗑 {username}",
                callback_data=f"admin:removeforce:{channel_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "➕ Add Channel",
            callback_data="admin:addforce"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Admin Menu",
            callback_data="admin:home"
        )
    ])

    return InlineKeyboardMarkup(buttons)


# ==========================================
# SHARING CONTROL
# ==========================================

def sharing_keyboard(file_id, enabled):
    file_id = int(file_id)

    if enabled:
        text = "🔴 Disable Sharing"
    else:
        text = "🟢 Enable Sharing"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text,
                callback_data=f"file:sharing:{file_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"file:details:{file_id}"
            )
        ]
    ])


# ==========================================
# GENERAL BACK BUTTON
# ==========================================

def back_keyboard(callback_data="home"):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=callback_data
            )
        ]
    ])
