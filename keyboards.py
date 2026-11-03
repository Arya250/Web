from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup
)


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


def file_keyboard(file_id):

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
        ]

    ])


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

    return InlineKeyboardMarkup(buttons)


def force_join_keyboard(channels):

    buttons = []

    for channel in channels:

        username = channel["channel"].lstrip("@")

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


def delete_confirmation_keyboard(file_id):

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


def admin_delete_confirmation_keyboard(file_id):

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
