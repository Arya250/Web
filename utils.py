import html
import re

from datetime import datetime, timezone

from telegram import Update

from config import (
    BOT_USERNAME,
    RENDER_EXTERNAL_URL,
    MAX_FILE_SIZE
)


def escape_html(value):

    return html.escape(
        str(value or ""),
        quote=True
    )


def format_size(size):

    if not size:
        return "Unknown"

    size = float(size)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ]

    for unit in units:

        if size < 1024:

            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size:.2f} PB"


def format_date(value):

    if not value:
        return "Unknown"

    return value.strftime(
        "%d %b %Y"
    )


def generate_share_link(token):

    if not BOT_USERNAME:

        raise RuntimeError(
            "BOT_USERNAME is not configured."
        )

    return (
        f"https://t.me/"
        f"{BOT_USERNAME}"
        f"?start=file_{token}"
    )


def generate_website_link(token):

    return (
        f"{RENDER_EXTERNAL_URL}"
        f"/file/{token}"
    )


def identify_file(message):

    file_data = None

    file_type = None

    file_name = None

    mime_type = None

    if message.document:

        file_data = message.document

        file_type = "document"

        file_name = (
            file_data.file_name
            or "document"
        )

        mime_type = file_data.mime_type


    elif message.video:

        file_data = message.video

        file_type = "video"

        file_name = "video.mp4"

        mime_type = file_data.mime_type


    elif message.photo:

        file_data = message.photo[-1]

        file_type = "photo"

        file_name = "photo.jpg"

        mime_type = "image/jpeg"


    elif message.audio:

        file_data = message.audio

        file_type = "audio"

        file_name = (
            file_data.file_name
            or "audio"
        )

        mime_type = file_data.mime_type


    elif message.voice:

        file_data = message.voice

        file_type = "voice"

        file_name = "voice.ogg"

        mime_type = file_data.mime_type


    elif message.video_note:

        file_data = message.video_note

        file_type = "video_note"

        file_name = "video_note.mp4"

        mime_type = file_data.mime_type


    elif message.animation:

        file_data = message.animation

        file_type = "animation"

        file_name = (
            file_data.file_name
            or "animation.gif"
        )

        mime_type = file_data.mime_type


    elif message.sticker:

        file_data = message.sticker

        file_type = "sticker"

        file_name = "sticker.webp"

        mime_type = "image/webp"


    elif message.voice:

        file_data = message.voice

        file_type = "voice"

        file_name = "voice.ogg"

        mime_type = file_data.mime_type


    if not file_data:
        return None

    file_size = getattr(
        file_data,
        "file_size",
        0
    ) or 0

    if file_size > MAX_FILE_SIZE:
        return {
            "error": "FILE_TOO_LARGE"
        }

    return {

        "file_id": file_data.file_id,

        "file_type": file_type,

        "file_name": file_name,

        "file_size": file_size,

        "mime_type": mime_type,

        "caption": message.caption or ""

    }


def is_valid_channel(channel):

    if not channel:
        return False

    channel = channel.strip()

    return bool(
        re.fullmatch(
            r"@[A-Za-z0-9_]{5,32}",
            channel
        )
    )


def utc_now():

    return datetime.now(
        timezone.utc
    )


def extract_start_token(text):

    if not text:
        return None

    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        return None

    payload = parts[1].strip()

    if payload.startswith("file_"):

        return payload[5:]

    return None


def human_file_type(file_type):

    names = {

        "document": "📄 Document",

        "video": "🎬 Video",

        "photo": "🖼 Photo",

        "audio": "🎵 Audio",

        "voice": "🎤 Voice",

        "video_note": "⭕ Video Note",

        "animation": "🎞 Animation",

        "sticker": "🏷 Sticker"

    }

    return names.get(
        file_type,
        "📁 File"
    )
