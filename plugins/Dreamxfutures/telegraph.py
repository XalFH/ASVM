import os
import asyncio
import logging
import tempfile
import shutil

import aiohttp

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto
)

from info import BIN_CHANNEL
from database.ia_filterdb import get_file_details


logger = logging.getLogger(__name__)


# ============================================================
# IMGBB
# ============================================================

IMGBB_API_KEY = os.getenv("IMGBB_API_KEY")
IMGBB_URL = "https://api.imgbb.com/1/upload"


async def upload_to_imgbb(file_path: str):

    if not IMGBB_API_KEY:
        logger.error("IMGBB_API_KEY is not set")
        return None

    if not os.path.exists(file_path):
        return None

    try:

        with open(file_path, "rb") as file:
            image_bytes = file.read()

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
                IMGBB_URL,
                data=form
            ) as response:

                if response.status != 200:
                    logger.error(
                        "ImgBB HTTP error: %s",
                        response.status
                    )
                    return None

                result = await response.json(
                    content_type=None
                )

        if result.get("success"):

            return result["data"]["url"]

        logger.error(
            "ImgBB response: %s",
            result
        )

        return None

    except Exception as e:

        logger.exception(
            "ImgBB upload error: %s",
            e
        )

        return None


# ============================================================
# OLD /IMG COMMAND
# ============================================================

@Client.on_message(
    filters.command(
        ["img", "cup", "telegraph"],
        prefixes="/"
    ) & filters.reply
)
async def c_upload(
    client: Client,
    message: Message
):

    reply = message.reply_to_message

    if not reply or not reply.media:

        return await message.reply_text(
            "Reply to a media to upload it to Cloud."
        )

    if not reply.photo and not reply.document:

        return await message.reply_text(
            "Please reply to an image."
        )

    if reply.document:

        file_size = reply.document.file_size or 0

        if file_size > 5 * 1024 * 1024:

            return await message.reply_text(
                "File size limit is 5 MB."
            )

    msg = await message.reply_text(
        "⏳ Processing..."
    )

    downloaded_media = None

    try:

        downloaded_media = await reply.download()

        if (
            not downloaded_media
            or not os.path.exists(downloaded_media)
        ):

            return await msg.edit_text(
                "❌ Download failed."
            )

        image_url = await upload_to_imgbb(
            downloaded_media
        )

        if not image_url:

            return await msg.edit_text(
                "❌ ImgBB upload failed."
            )

        await msg.edit_text(
            "🖼️ <b>ImgBB URL:</b>\n\n"
            f"<code>{image_url}</code>"
        )

    except Exception as e:

        logger.exception(e)

        try:

            await msg.edit_text(
                f"❌ Error:\n<code>{e}</code>"
            )

        except Exception:
            pass

    finally:

        if (
            downloaded_media
            and os.path.exists(downloaded_media)
        ):

            try:
                os.remove(downloaded_media)
            except Exception:
                pass


# ============================================================
# COMMAND RUNNER
# ============================================================

async def run_command(*args):

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    return (
        process.returncode,
        stdout.decode(errors="ignore"),
        stderr.decode(errors="ignore")
    )


# ============================================================
# VIDEO DURATION
# ============================================================

async def get_video_duration(video_path):

    try:

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
            logger.error(
                "ffprobe error: %s",
                stderr
            )
            return None

        return float(
            stdout.strip()
        )

    except Exception as e:

        logger.exception(e)

        return None


# ============================================================
# SCREENSHOT EXTRACTION
# ============================================================

async def extract_screenshot(
    video_path,
    output_path,
    timestamp
):

    try:

        code, stdout, stderr = await run_command(
            "ffmpeg",
            "-y",
            "-ss",
            str(timestamp),
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            output_path
        )

        if code != 0:

            logger.error(
                "FFmpeg error: %s",
                stderr
            )

            return False

        return (
            os.path.exists(output_path)
            and os.path.getsize(output_path) > 0
        )

    except Exception as e:

        logger.exception(e)

        return False


# ============================================================
# SCREENSHOT BUTTON
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
            "📸 Extracting screenshots...",
            show_alert=False
        )

    except Exception:
        pass

    # Get file ID
    try:

        file_id = query.data.split(
            ":",
            1
        )[1]

    except Exception:

        return

    work_dir = tempfile.mkdtemp(
        prefix="asvm_ss_"
    )

    video_path = os.path.join(
        work_dir,
        "video"
    )

    screenshot_1 = os.path.join(
        work_dir,
        "screenshot_1.jpg"
    )

    screenshot_2 = os.path.join(
        work_dir,
        "screenshot_2.jpg"
    )

    cached_message = None

    try:

        # --------------------------------------------
        # DATABASE
        # --------------------------------------------

        files_ = await get_file_details(
            file_id
        )

        if not files_:

            await query.message.reply_text(
                "❌ File not found in database."
            )

            return

        file_info = files_[0]

        # --------------------------------------------
        # VIDEO CHECK
        # --------------------------------------------

        if getattr(
            file_info,
            "file_type",
            None
        ) != "video":

            await query.message.reply_text(
                "❌ Screenshots are available "
                "for video files only."
            )

            return

        # --------------------------------------------
        # GET TELEGRAM FILE
        # --------------------------------------------

        cached_message = (
            await client.send_cached_media(
                chat_id=BIN_CHANNEL,
                file_id=file_id
            )
        )

        # --------------------------------------------
        # DOWNLOAD VIDEO
        # --------------------------------------------

        downloaded = await cached_message.download(
            file_name=video_path
        )

        if (
            not downloaded
            or not os.path.exists(downloaded)
        ):

            await query.message.reply_text(
                "❌ Video download failed."
            )

            return

        video_path = downloaded

        # --------------------------------------------
        # VIDEO DURATION
        # --------------------------------------------

        duration = await get_video_duration(
            video_path
        )

        if not duration:

            await query.message.reply_text(
                "❌ Unable to read video duration."
            )

            return

        # --------------------------------------------
        # SCREENSHOT TIME
        # --------------------------------------------

        if duration <= 4:

            timestamp_1 = max(
                0.5,
                duration * 0.25
            )

            timestamp_2 = max(
                1.0,
                duration * 0.75
            )

        else:

            timestamp_1 = duration * 0.30
            timestamp_2 = duration * 0.70

        timestamp_1 = min(
            timestamp_1,
            max(0, duration - 0.2)
        )

        timestamp_2 = min(
            timestamp_2,
            max(0, duration - 0.1)
        )

        # --------------------------------------------
        # EXTRACT
        # --------------------------------------------

        result_1, result_2 = await asyncio.gather(

            extract_screenshot(
                video_path,
                screenshot_1,
                timestamp_1
            ),

            extract_screenshot(
                video_path,
                screenshot_2,
                timestamp_2
            )
        )

        if not result_1 or not result_2:

            await query.message.reply_text(
                "❌ FFmpeg could not extract screenshots."
            )

            return

        # --------------------------------
