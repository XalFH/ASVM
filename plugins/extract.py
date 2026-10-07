import asyncio
import os
import logging
import aiofiles
import tempfile
import uuid
import aiohttp
import requests

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    InputMediaPhoto
)
from telegraph import Telegraph
from pymediainfo import MediaInfo

from database.ia_filterdb import get_file_details
from info import BIN_CHANNEL
from dreamxbotz.util.file_properties import get_name

logger = logging.getLogger(__name__)


# ============================================================
# CONFIG
# ============================================================

IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY")

TELEGRAPH_ACCESS_TOKEN = (
    os.environ.get("TELEGRAPH_ACCESS_TOKEN")
    or "6288219713e387a679586a9a184b6be21b795770e11f60d6006bd0005342"
)


# ============================================================
# TELEGRAPH INIT
# ============================================================

if TELEGRAPH_ACCESS_TOKEN:

    telegraph = Telegraph(
        access_token=TELEGRAPH_ACCESS_TOKEN
    )

else:

    telegraph = Telegraph()

    try:
        telegraph.create_account(
            short_name="ASVM"
        )
    except Exception:
        logger.exception(
            "Failed to create Telegraph account"
        )


# ============================================================
# HELPERS
# ============================================================

def format_track(
    lang: str | None,
    title: str | None
) -> str:

    lang = (lang or "").strip()
    title = (title or "").strip()

    if lang and lang.lower() != "und":
        return lang

    if title:
        return title

    return "und"


async def upload_to_imgbb(
    file_path: str
):
    """
    Upload image to ImgBB.

    Returns:
        ImgBB direct URL or None
    """

    if not IMGBB_API_KEY:
        logger.error(
            "IMGBB_API_KEY is not configured"
        )
        return None

    if not os.path.exists(file_path):
        return None

    try:

        with open(
            file_path,
            "rb"
        ) as f:

            image_bytes = f.read()

        form = aiohttp.FormData()

        form.add_field(
            "key",
            IMGBB_API_KEY
        )

        form.add_field(
            "image",
            image_bytes,
            filename=os.path.basename(file_path),
            content_type="image/jpeg"
        )

        timeout = aiohttp.ClientTimeout(
            total=120
        )

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.post(
                "https://api.imgbb.com/1/upload",
                data=form
            ) as response:

                if response.status != 200:

                    logger.error(
                        "ImgBB HTTP error: %s",
                        response.status
                    )

                    return None

                result = await response.json()

        if result.get("success"):

            return result["data"]["url"]

        logger.error(
            "ImgBB upload failed: %s",
            result
        )

        return None

    except Exception as e:

        logger.exception(
            "ImgBB upload error: %s",
            e
        )

        return None


async def run_command(
    *args
):
    """
    Run FFmpeg/FFprobe without blocking
    the asyncio event loop.
    """

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    return (
        process.returncode,
        stdout.decode(
            errors="ignore"
        ).strip(),
        stderr.decode(
            errors="ignore"
        ).strip()
    )


async def get_video_duration(
    video_path: str
):
    """
    Get video duration using ffprobe.
    """

    code, stdout, stderr = await run_command(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_path
    )

    if code != 0:
        raise RuntimeError(
            stderr or "Unable to read video duration."
        )

    try:

        duration = float(
            stdout.strip()
        )

    except Exception:

        raise RuntimeError(
            "Invalid video duration."
        )

    if duration <= 0:
        raise RuntimeError(
            "Invalid video duration."
        )

    return duration


async def extract_frame(
    video_path: str,
    output_path: str,
    timestamp: float
):
    """
    Extract one JPG frame using FFmpeg.
    """

    code, stdout, stderr = await run_command(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(timestamp),
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        output_path
    )

    if (
        code != 0
        or not os.path.exists(output_path)
        or os.path.getsize(output_path) == 0
    ):

        raise RuntimeError(
            stderr or "Failed to extract screenshot."
        )

    return output_path


async def download_media_file(
    client: Client,
    message,
    output_path: str
):
    """
    Download complete Telegram media.
    """

    async with aiofiles.open(
        output_path,
        "wb"
    ) as f:

        async for chunk in client.stream_media(
            message,
            limit=10
        ):

            await f.write(chunk)

    if (
        not os.path.exists(output_path)
        or os.path.getsize(output_path) == 0
    ):

        raise RuntimeError(
            "Downloaded file is empty."
        )

    return output_path


# ============================================================
# AUDIO / MEDIA INFO
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^extract_data"),
    group=2
)
async def extract_data_handler(
    client: Client,
    query: CallbackQuery
):

    try:

        await query.answer(
            "Fetching Details...",
            show_alert=False
        )

    except Exception:

        pass

    try:

        _, file_id = query.data.split(
            ":",
            1
        )

    except ValueError:

        await query.answer(
            "Invalid request.",
            show_alert=True
        )

        return

    current_markup = (
        query.message.reply_markup
    )

    wait_keyboard = []

    if (
        current_markup
        and getattr(
            current_markup,
            "inline_keyboard",
            None
        )
    ):

        for row in current_markup.inline_keyboard:

            new_row = []

            for btn in row:

                if btn.callback_data == query.data:

                    new_row.append(
                        InlineKeyboardButton(
                            "ᴘʟᴇᴀꜱᴇ ᴡᴀɪᴛ...",
                            callback_data="wait_data"
                        )
                    )

                else:

                    new_row.append(btn)

            wait_keyboard.append(
                new_row
            )

    try:

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                wait_keyboard
            )
        )

    except Exception:

        pass

    temp_path = os.path.join(
        tempfile.gettempdir(),
        f"acc_{query.from_user.id}_"
        f"{query.message.id}_"
        f"{uuid.uuid4().hex}.tmp"
    )

    try:

        files_ = await get_file_details(
            file_id
        )

        if not files_:

            await query.message.reply_text(
                "File not found in DB."
            )

            return

        if (
            query.message
            and query.message.media
        ):

            log_msg = query.message

        else:

            log_msg = await client.send_cached_media(
                chat_id=BIN_CHANNEL,
                file_id=file_id
            )

        file_name = get_name(
            log_msg
        )

        safe_title = (
            file_name
            .replace(".", " ")
            .replace("_", " ")
            .replace("-", " ")
            .replace("[", "")
            .replace("]", "")
            .replace("(", "")
            .replace(")", "")
            .replace("mkv", "")
            .replace("mp4", "")
        )

        media = (
            getattr(
                log_msg,
                log_msg.media.value
            )
            if log_msg.media
            else None
        )

        file_size = (
            getattr(
                media,
                "file_size",
                0
            )
            or 0
        )

        chunk_limit = (
            5
            if file_size > 200 * 1024 * 1024
            else 4
        )

        async with aiofiles.open(
            temp_path,
            "wb"
        ) as f:

            async for chunk in client.stream_media(
                log_msg,
                limit=chunk_limit
            ):

                await f.write(chunk)

        lib_path = (
            os.path.abspath(
                "MediaInfo.dll"
            )
            if os.path.exists(
                "MediaInfo.dll"
            )
            else None
        )

        media_info = await asyncio.wait_for(
            asyncio.to_thread(
                MediaInfo.parse,
                temp_path,
                library_file=lib_path
            ),
            timeout=10
        )

        audio_tracks = []
        subtitle_tracks = []
        video_info = []

        seen_audio = set()
        seen_subs = set()

        for track in media_info.tracks:

            ttype = (
                track.track_type or ""
            ).lower()

            if ttype == "video":

                codec = (
                    track.format
                    or track.codec_id
                    or "Unknown"
                )

                width = track.width or "?"
                height = track.height or "?"

                video_info.append(
                    f"Video: {codec} "
                    f"{width}x{height}"
                )

            elif ttype == "audio":

                lang = (
                    track.other_language[0]
                    if getattr(
                        track,
                        "other_language",
                        None
                    )
                    else track.language
                    or "und"
                )

                key = (
                    lang,
                    track.title
                )

                if key not in seen_audio:

                    seen_audio.add(
                        key
                    )

                    audio_tracks.append({
                        "language": lang,
                        "title": track.title
                    })

            elif ttype in (
                "text",
                "subtitle"
            ):

                lang = (
                    track.other_language[0]
                    if getattr(
                        track,
                        "other_language",
                        None
                    )
                    else track.language
                    or "und"
                )

                key = (
                    lang,
                    track.title
                )

                if key not in seen_subs:

                    seen_subs.add(
                        key
                    )

                    subtitle_tracks.append({
                        "language": lang,
                        "title": track.title
                    })

        page_parts = []

        page_parts.append(
            "<h3><b>Available Tracks</b></h3>"
        )

        page_parts.append(
            '<img src="https://i.ibb.co/HLzJZHG3/image.jpg">'
        )

        page_parts.append(
            "<br><br>"
        )

        page_parts.append(
            "<p>"
            "<b>Track Information</b><br>"
            "Below you can find all available video, "
            "audio and subtitle tracks for this file."
            "</p>"
        )

        page_parts.append(
            "<br>"
        )

        if video_info:

            page_parts.append(
                "<b>Video Track:</b><br>"
            )

            for video in video_info:

                page_parts.append(
                    f"<blockquote>• {video}</blockquote>"
                )

            page_parts.append(
                "<br>"
            )

        if audio_tracks:

            page_parts.append(
                f"<b>Audio Tracks "
                f"({len(audio_tracks)}):</b><br>"
            )

            for audio in audio_tracks:

                track_name = format_track(
                    audio["language"],
                    audio["title"]
                )

                page_parts.append(
                    f"<blockquote>• "
                    f"{track_name}</blockquote>"
                )

            page_parts.append(
                "<br>"
            )

        else:

            page_parts.append(
                "<b>Audio Tracks:</b> "
                "None<br><br>"
            )

        if subtitle_tracks:

            page_parts.append(
                f"<b>Subtitle Tracks "
                f"({len(subtitle_tracks)}):</b><br>"
            )

            for subtitle in subtitle_tracks:

                track_name = format_track(
                    subtitle["language"],
                    subtitle["title"]
                )

                page_parts.append(
                    f"<blockquote>• "
                    f"{track_name}</blockquote>"
                )

            page_parts.append(
                "<br>"
            )

        else:

            page_parts.append(
                "<b>Subtitle Tracks:</b> "
                "None<br><br>"
            )

        if len(audio_tracks) > 1:

            page_parts.append(
                "<blockquote>"
                "<b>Note:</b><br><br>"
                "This file contains multiple audio tracks. "
                "For the best experience and to switch "
                "between audio tracks, please use "
                "<b>VLC Media Player</b>.<br><br>"
                "<b>हिंदी:</b><br>"
                "इस फ़ाइल में कई ऑडियो ट्रैक हैं। "
                "ऑडियो ट्रैक बदलने के लिए "
                "<b>VLC Media Player</b> का इस्तेमाल करें।"
                "</blockquote>"
            )

        elif len(audio_tracks) == 1:

            page_parts.append(
                "<blockquote>"
                "<b>Note:</b><br><br>"
                "This file contains a single audio track "
                "and can be played easily using the "
                "<b>Telegram Player</b>.<br><br>"
                "<b>हिंदी:</b><br>"
                "इस फ़ाइल में केवल एक ऑडियो ट्रैक है और "
                "इसे <b>Telegram Player</b> में आसानी से "
                "चलाया जा सकता है।"
                "</blockquote>"
            )

        else:

            page_parts.append(
                "<blockquote>"
                "<b>Note:</b><br><br>"
                "No audio track was detected in this file. "
                "If you experience any playback issue, "
                "try using <b>VLC Media Player</b>.<br><br>"
                "<b>हिंदी:</b><br>"
                "इस फ़ाइल में कोई ऑडियो ट्रैक नहीं मिला। "
                "अगर फ़ाइल चलाने में कोई समस्या हो, "
                "तो <b>VLC Media Player</b> का इस्तेमाल करें।"
                "</blockquote>"
            )

        page_parts.append(
            "<br><br>"
        )

        page_parts.append(
            "<p>"
            "<b>ASVM</b><br>"
            "Media Track Information"
            "</p>"
        )

        page_content = "".join(
            page_parts
        )

        try:

            response = await asyncio.to_thread(
                telegraph.create_page,
                title=safe_title[:200],
                html_content=page_content,
                author_name="ASVM"
            )

        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout
        ):

            await query.message.reply_text(
                "Telegraph is busy. Try again later."
            )

            return

        telegraph_url = response["url"]

        success_keyboard = []

        if (
            current_markup
            and getattr(
                current_markup,
                "inline_keyboard",
                None
            )
        ):

            for row in current_markup.inline_keyboard:

                new_row = []

                for btn in row:

                    if btn.callback_data == query.data:

                        new_row.append(
                            InlineKeyboardButton(
                                "• ᴠɪᴇᴡ ᴛʀᴀᴄᴋs •",
                                url=telegraph_url
                            )
                        )

                    else:

                        new_row.append(btn)

                success_keyboard.append(
                    new_row
                )

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                success_keyboard
            )
        )

    except Exception as e:

        logger.exception(e)

        try:

            await query.message.reply_text(
                f"Error: {e}"
            )

        except Exception:

            pass

    finally:

        if os.path.exists(temp_path):

            try:
                os.remove(temp_path)
            except Exception:
                pass


# ============================================================
# SCREENSHOT EXTRACTOR
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^screenshots:")
)
async def screenshots_handler(
    client: Client,
    query: CallbackQuery
):

    try:

        await query.answer(
            "Extracting 2 screenshots...",
            show_alert=False
        )

    except Exception:

        pass

    try:

        _, file_id = query.data.split(
            ":",
            1
        )

    except ValueError:

        try:
            await query.answer(
                "Invalid request.",
                show_alert=True
            )
        except Exception:
            pass

        return

    status = None

    try:

        status = await query.message.reply_text(
            "⏳ <b>Screenshot Extractor</b>\n\n"
            "Downloading video..."
        )

        files_ = await get_file_details(
            file_id
        )

        if not files_:

            await status.edit_text(
                "❌ File not found in DB."
            )

            return

        # Get original Telegram media
        if (
            query.message
            and query.message.media
        ):

            log_msg = query.message

        else:

            log_msg = await client.send_cached_media(
                chat_id=BIN_CHANNEL,
                file_id=file_id
            )

        file_name = get_name(
            log_msg
        )

        safe_title = (
            file_name
            .replace(".", " ")
            .replace("_", " ")
            .replace("-", " ")
            .strip()
        )

        temp_dir = tempfile.mkdtemp(
            prefix="asvm_ss_"
        )

        video_path = os.path.join(
            temp_dir,
            f"video_{uuid.uuid4().hex}.media"
        )

        screenshot_1 = os.path.join(
            temp_dir,
            "screenshot_1.jpg"
        )

        screenshot_2 = os.path.join(
            temp_dir,
            "screenshot_2.jpg"
        )

        try:

            await status.edit_text(
                "⏳ <b>Screenshot Extractor</b>\n\n"
                "Downloading video..."
            )

            await download_media_file(
                client,
                log_msg,
                video_path
            )

            await status.edit_text(
                "⏳ <b>Screenshot Extractor</b>\n\n"
                "Extracting 2 screenshots..."
            )

            duration = await get_video_duration(
                video_path
            )

            # 30% and 70%
            if duration <= 4:

                time_1 = max(
                    0.1,
                    duration * 0.25
                )

                time_2 = max(
                    0.1,
                    duration * 0.75
                )

            else:

                time_1 = duration * 0.30
                time_2 = duration * 0.70

            # Make sure timestamp is not exactly at EOF
            safe_end = max(
                0.1,
                duration - 0.2
            )

            time_1 = min(
                time_1,
                safe_end
            )

            time_2 = min(
                time_2,
                safe_end
            )

            await asyncio.gather(
                extract_frame(
                    video_path,
                    screenshot_1,
                    time_1
                ),
                extract_frame(
                    video_path,
                    screenshot_2,
                    time_2
                )
            )

            await status.edit_text(
                "⏳ <b>Screenshot Extractor</b>\n\n"
                "Uploading screenshots to ImgBB..."
            )

            # Upload both simultaneously
            img_url_1, img_url_2 = await asyncio.gather(
                upload_to_imgbb(
                    screenshot_1
                ),
                upload_to_imgbb(
                    screenshot_2
                )
            )

            if not img_url_1 or not img_url_2:

                await status.edit_text(
                    "❌ <b>ImgBB upload failed.</b>\n\n"
                    "Please check the ImgBB API key."
                )

                return

            # Send screenshots to Telegram
            caption = (
                f"📸 <b>Screenshots</b>\n\n"
                f"🎬 <b>{safe_title[:150]}</b>\n\n"
                f"• Screenshot 1\n"
                f"• Screenshot 2\n\n"
                f"<b>Uploaded to ImgBB</b>"
            )

            media_group = [
                InputMediaPhoto(
                    media=screenshot_1,
                    caption=caption
                ),
                InputMediaPhoto(
                    media=screenshot_2
                )
            ]

            await client.send_media_group(
                chat_id=query.message.chat.id,
                media=media_group,
                reply_to_message_id=query.message.id
            )

            # URL buttons
            url_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📸 Screenshot 1",
                            url=img_url_1
                        ),
                        InlineKeyboardButton(
                            "📸 Screenshot 2",
                            url=img_url_2
                        )
                    ]
                ]
            )

            await client.send_message(
                chat_id=query.message.chat.id,
                text=(
                    "🖼 <b>Screenshot Links</b>\n\n"
                    "Both screenshots have been uploaded "
                    "successfully to ImgBB."
                ),
                reply_markup=url_keyboard,
                reply_to_message_id=query.message.id
            )

            await status.delete()

        finally:

            # Cleanup all temporary files
            for path in (
                video_path,
                screenshot_1,
                screenshot_2
            ):

                if os.path.exists(path):

                    try:
                        os.remove(path)
                    except Exception:
                        pass

            try:

                os.rmdir(temp_dir)

            except Exception:

                pass

    except Exception as e:

        logger.exception(
            "Screenshot extractor error: %s",
            e
        )

        error_text = (
            "❌ <b>Screenshot extraction failed.</b>\n\n"
            f"<code>{str(e)[:1000]}</code>"
        )

        try:

            if status:

                await status.edit_text(
                    error_text
                )

            else:

                await query.message.reply_text(
                    error_text
                )

        except Exception:

            pass
