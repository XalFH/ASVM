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


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# IMGBB CONFIG
# ============================================================

IMGBB_API_KEY = os.getenv("IMGBB_API_KEY")

IMGBB_URL = "https://api.imgbb.com/1/upload"


# ============================================================
# UPLOAD IMAGE TO IMGBB
# ============================================================

async def upload_to_imgbb(file_path: str):

    if not IMGBB_API_KEY:
        logger.error(
            "IMGBB_API_KEY environment variable is missing."
        )
        return None

    if not file_path:
        return None

    if not os.path.exists(file_path):
        logger.error(
            "File does not exist: %s",
            file_path
        )
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

                    error_text = await response.text()

                    logger.error(
                        "ImgBB HTTP %s: %s",
                        response.status,
                        error_text
                    )

                    return None

                result = await response.json(
                    content_type=None
                )

        if result.get("success"):

            data = result.get(
                "data",
                {}
            )

            return (
                data.get("url")
                or data.get("display_url")
            )

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


# ============================================================
# OLD /IMG /CUP /TELEGRAPH COMMAND
# ============================================================

@Client.on_message(
    filters.command(
        [
            "img",
            "cup",
            "telegraph"
        ],
        prefixes="/"
    )
    & filters.reply
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

    # --------------------------------------------------------
    # DOCUMENT SIZE LIMIT
    # --------------------------------------------------------

    if reply.document:

        file_size = (
            reply.document.file_size
            or 0
        )

        if file_size > 5 * 1024 * 1024:

            return await message.reply_text(
                "❌ File size limit is 5 MB."
            )

    msg = await message.reply_text(
        "⏳ Processing..."
    )

    downloaded_media = None

    try:

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        downloaded_media = await reply.download()

        if (
            not downloaded_media
            or not os.path.exists(downloaded_media)
        ):

            await msg.edit_text(
                "❌ Something went wrong during download."
            )

            return

        # ----------------------------------------------------
        # IMGBB
        # ----------------------------------------------------

        image_url = await upload_to_imgbb(
            downloaded_media
        )

        if not image_url:

            await msg.edit_text(
                "❌ ImgBB upload failed.\n\n"
                "Please check the ImgBB API key."
            )

            return

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        await msg.edit_text(
            "🖼️ <b>ImgBB URL:</b>\n\n"
            f"<code>{image_url}</code>"
        )

    except Exception as e:

        logger.exception(
            "Normal ImgBB upload error: %s",
            e
        )

        try:

            await msg.edit_text(
                "❌ <b>Error:</b>\n\n"
                f"<code>{e}</code>"
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
# RUN COMMAND
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
        stdout.decode(
            errors="ignore"
        ),
        stderr.decode(
            errors="ignore"
        )
    )


# ============================================================
# GET VIDEO DURATION
# ============================================================

async def get_video_duration(
    video_path
):

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
                "FFprobe error: %s",
                stderr
            )

            return None, stderr

        value = stdout.strip()

        if not value:

            return None, "Duration not found."

        return float(value), None

    except Exception as e:

        logger.exception(
            "FFprobe exception: %s",
            e
        )

        return None, str(e)


# ============================================================
# EXTRACT SCREENSHOT USING FFMPEG
# ============================================================

async def extract_screenshot(
    video_path,
    output_path,
    timestamp
):

    try:

        process = await asyncio.create_subprocess_exec(

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

            "-vf",
            "scale='min(1280,iw)':-2",

            output_path,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        error = stderr.decode(
            errors="ignore"
        )

        if process.returncode != 0:

            logger.error(
                "FFmpeg error: %s",
                error
            )

            return False, error[-3000:]

        if not os.path.exists(
            output_path
        ):

            return False, (
                "Screenshot file was not created."
            )

        if os.path.getsize(
            output_path
        ) <= 0:

            return False, (
                "Screenshot file is empty."
            )

        return True, None

    except Exception as e:

        logger.exception(
            "Screenshot extraction error: %s",
            e
        )

        return False, str(e)


# ============================================================
# SCREENSHOT CALLBACK
# ============================================================

@Client.on_callback_query(
    filters.regex(
        r"^screenshots:"
    )
)
async def screenshots_handler(
    client: Client,
    query: CallbackQuery
):

    work_dir = None

    cached_message = None

    status = None

    try:

        # ----------------------------------------------------
        # CALLBACK ANSWER
        # ----------------------------------------------------

        try:

            await query.answer(
                "📸 Starting screenshot extraction...",
                show_alert=False
            )

        except Exception:
            pass

        # ----------------------------------------------------
        # GET FILE ID
        # ----------------------------------------------------

        try:

            file_id = query.data.split(
                ":",
                1
            )[1]

        except Exception:

            return

        # ----------------------------------------------------
        # STATUS MESSAGE
        # ----------------------------------------------------

        status = await query.message.reply_text(

            "📸 <b>Screenshot Extractor</b>\n\n"
            "⏳ Getting video..."

        )

        # ====================================================
        # DATABASE
        # ====================================================

        files_ = await get_file_details(
            file_id
        )

        if not files_:

            await status.edit_text(
                "❌ File not found in database."
            )

            return

        file_info = files_[0]

        # ====================================================
        # VIDEO CHECK
        # ====================================================

        if getattr(
            file_info,
            "file_type",
            None
        ) != "video":

            await status.edit_text(
                "❌ Screenshots are available "
                "for video files only."
            )

            return

        # ====================================================
        # CREATE TEMP DIRECTORY
        # ====================================================

        work_dir = tempfile.mkdtemp(
            prefix="asvm_screenshot_"
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

        # ====================================================
        # DOWNLOAD VIDEO
        # ====================================================

        await status.edit_text(

            "📥 <b>Downloading video...</b>\n\n"
            "Please wait..."

        )

        # ----------------------------------------------------
        # SEND CACHED VIDEO TO BIN CHANNEL
        # ----------------------------------------------------

        cached_message = (
            await client.send_cached_media(
                chat_id=BIN_CHANNEL,
                file_id=file_id
            )
        )

        if not cached_message:

            await status.edit_text(
                "❌ Could not get video from database."
            )

            return

        # ----------------------------------------------------
        # DOWNLOAD FROM TELEGRAM
        # ----------------------------------------------------

        downloaded = await cached_message.download(
            file_name=video_path
        )

        if not downloaded:

            await status.edit_text(
                "❌ Telegram video download failed."
            )

            return

        video_path = downloaded

        # ----------------------------------------------------
        # CHECK FILE
        # ----------------------------------------------------

        if not os.path.exists(
            video_path
        ):

            await status.edit_text(
                "❌ Downloaded video file not found."
            )

            return

        video_size = os.path.getsize(
            video_path
        )

        if video_size <= 0:

            await status.edit_text(
                "❌ Downloaded video is empty."
            )

            return

        # ====================================================
        # FFPROBE
        # ====================================================

        await status.edit_text(

            "🎬 <b>Video downloaded.</b>\n\n"
            "🔍 Reading video duration..."

        )

        duration, duration_error = (
            await get_video_duration(
                video_path
            )
        )

        if not duration:

            await status.edit_text(

                "❌ <b>FFprobe failed.</b>\n\n"
                f"<code>{duration_error}</code>"

            )

            return

        # ====================================================
        # CALCULATE TIMESTAMPS
        # ====================================================

        timestamp_1 = max(
            0.5,
            duration * 0.30
        )

        timestamp_2 = max(
            1.0,
            duration * 0.70
        )

        timestamp_1 = min(
            timestamp_1,
            max(
                0,
                duration - 0.2
            )
        )

        timestamp_2 = min(
            timestamp_2,
            max(
                0,
                duration - 0.1
            )
        )

        # ====================================================
        # FFMPEG SCREENSHOT 1
        # ====================================================

        await status.edit_text(

            "🎬 <b>Extracting screenshots...</b>\n\n"
            "📸 Extracting Screenshot 1..."

        )

        result_1, error_1 = (
            await extract_screenshot(

                video_path,

                screenshot_1,

                timestamp_1
            )
        )

        if not result_1:

            await status.edit_text(

                "❌ <b>Screenshot 1 failed.</b>\n\n"
                f"<code>{error_1}</code>"

            )

            return

        # ====================================================
        # FFMPEG SCREENSHOT 2
        # ====================================================

        await status.edit_text(

            "🎬 <b>Extracting screenshots...</b>\n\n"
            "✅ Screenshot 1 extracted\n"
            "📸 Extracting Screenshot 2..."

        )

        result_2, error_2 = (
            await extract_screenshot(

                video_path,

                screenshot_2,

                timestamp_2
            )
        )

        if not result_2:

            await status.edit_text(

                "❌ <b>Screenshot 2 failed.</b>\n\n"
                f"<code>{error_2}</code>"

            )

            return

        # ====================================================
        # IMGBB UPLOAD
        # ====================================================

        await status.edit_text(

            "☁️ <b>Uploading screenshots to ImgBB...</b>\n\n"
            "⏳ Please wait..."

        )

        img_1, img_2 = await asyncio.gather(

            upload_to_imgbb(
                screenshot_1
            ),

            upload_to_imgbb(
                screenshot_2
            )
        )

        if not img_1:

            await status.edit_text(
                "❌ Screenshot 1 ImgBB upload failed."
            )

            return

        if not img_2:

            await status.edit_text(
                "❌ Screenshot 2 ImgBB upload failed."
            )

            return

        # ====================================================
        # SEND SCREENSHOTS TO USER
        # ====================================================

        await status.edit_text(

            "📤 <b>Sending screenshots...</b>"

        )

        try:

            await client.send_media_group(

                chat_id=query.message.chat.id,

                media=[

                    InputMediaPhoto(

                        screenshot_1,

                        caption="📸 Screenshot 1"

                    ),

                    InputMediaPhoto(

                        screenshot_2,

                        caption="📸 Screenshot 2"

                    )

                ]

            )

        except Exception as media_error:

            logger.warning(
                "Media group failed: %s",
                media_error
            )

            # ------------------------------------------------
            # FALLBACK
            # ------------------------------------------------

            await client.send_photo(

                chat_id=query.message.chat.id,

                photo=screenshot_1,

                caption="📸 Screenshot 1"

            )

            await client.send_photo(

                chat_id=query.message.chat.id,

                photo=screenshot_2,

                caption="📸 Screenshot 2"

            )

        # ====================================================
        # IMGBB BUTTONS
        # ====================================================

        buttons = InlineKeyboardMarkup(

            [

                [

                    InlineKeyboardButton(

                        "🖼 Screenshot 1",

                        url=img_1

                    ),

                    InlineKeyboardButton(

                        "🖼 Screenshot 2",

                        url=img_2

                    )

                ]

            ]

        )

        # ====================================================
        # SUCCESS
        # ====================================================

        await status.edit_text(

            "<b>✅ Screenshots extracted successfully!</b>\n\n"
            "📸 Both screenshots have been sent above.\n\n"
            "☁️ Screenshots uploaded to ImgBB.",
            
            reply_markup=buttons

        )

    # ========================================================
    # GLOBAL ERROR
    # ========================================================

    except Exception as e:

        logger.exception(
            "Screenshot extraction failed"
        )

        error_text = str(e)

        if status:

            try:

                await status.edit_text(

                    "❌ <b>Screenshot extraction failed.</b>\n\n"
                    f"<code>{error_text}</code>"

                )

            except Exception:

                pass

        else:

            try:

                await query.message.reply_text(

                    "❌ <b>Screenshot extraction failed.</b>\n\n"
                    f"<code>{error_text}</code>"

                )

            except Exception:

                pass

    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

        # ----------------------------------------------------
        # DELETE TEMP BIN CHANNEL MESSAGE
        # ----------------------------------------------------

        if cached_message:

            try:

                await cached_message.delete()

            except Exception:

                pass

        # ----------------------------------------------------
        # DELETE TEMP FILES
        # ----------------------------------------------------

        if work_dir:

            try:

                shutil.rmtree(
                    work_dir,
                    ignore_errors=True
                )

            except Exception:

                pass
