import os
import asyncio
import logging
import tempfile
import shutil

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InputMediaPhoto,
)

from info import BIN_CHANNEL
from database.ia_filterdb import get_file_details


logger = logging.getLogger(__name__)


# ============================================================
# NORMAL TELEGRAM MEDIA COMMANDS
# /img, /cup, /telegraph
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
        size = reply.document.file_size or 0

        if size > 5 * 1024 * 1024:
            return await message.reply_text(
                "File size limit is 5 MB."
            )

    msg = await message.reply_text(
        "This command's previous ImgBB uploader has been removed. "
        "Use the screenshot button for video screenshots."
    )


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
        stdout.decode(errors="ignore"),
        stderr.decode(errors="ignore")
    )


# ============================================================
# GET VIDEO DURATION
# ============================================================

async def get_video_duration(video_path):
    try:
        code, stdout, stderr = await run_command(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        )

        if code != 0:
            return None, stderr[-2000:]

        duration = float(stdout.strip())

        if duration <= 0:
            return None, "Invalid video duration."

        return duration, None

    except Exception as e:
        logger.exception("Video duration error")
        return None, str(e)


# ============================================================
# EXTRACT ONE SCREENSHOT
# ============================================================

async def extract_screenshot(
    video_path,
    output_path,
    timestamp
):
    try:
        code, stdout, stderr = await run_command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", str(timestamp),
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "3",
            "-vf", "scale='min(1280,iw)':-2",
            output_path
        )

        if code != 0:
            return False, stderr[-2000:]

        if not os.path.isfile(output_path):
            return False, "Screenshot was not created."

        if os.path.getsize(output_path) == 0:
            return False, "Screenshot file is empty."

        return True, None

    except Exception as e:
        logger.exception("Screenshot extraction error")
        return False, str(e)


# ============================================================
# SCREENSHOT EXTRACTOR CALLBACK
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^screenshots:")
)
async def screenshots_handler(
    client: Client,
    query: CallbackQuery
):
    work_dir = None
    cached_message = None
    status = None

    try:
        await query.answer(
            "Extracting screenshots...",
            show_alert=False
        )

        file_id = query.data.split(":", 1)[1]

        status = await query.message.reply_text(
            "📸 <b>Screenshot Extractor</b>\n\n"
            "⏳ Getting video..."
        )

        # ----------------------------------------------------
        # DATABASE
        # ----------------------------------------------------

        files_ = await get_file_details(file_id)

        if not files_:
            await status.edit_text(
                "❌ File not found in database."
            )
            return

        file_info = files_[0]

        if getattr(file_info, "file_type", None) != "video":
            await status.edit_text(
                "❌ Screenshots are available for videos only."
            )
            return

        # ----------------------------------------------------
        # TEMPORARY DIRECTORY
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # GET TELEGRAM VIDEO
        # ----------------------------------------------------

        await status.edit_text(
            "📥 <b>Downloading video...</b>"
        )

        cached_message = await client.send_cached_media(
            chat_id=BIN_CHANNEL,
            file_id=file_id
        )

        if not cached_message:
            await status.edit_text(
                "❌ Could not get video from Telegram."
            )
            return

        # ----------------------------------------------------
        # DOWNLOAD VIDEO
        # ----------------------------------------------------

        downloaded = await cached_message.download(
            file_name=video_path
        )

        if not downloaded or not os.path.isfile(downloaded):
            await status.edit_text(
                "❌ Video download failed."
            )
            return

        video_path = downloaded

        if os.path.getsize(video_path) == 0:
            await status.edit_text(
                "❌ Downloaded video is empty."
            )
            return

        # ----------------------------------------------------
        # GET DURATION
        # ----------------------------------------------------

        await status.edit_text(
            "🎬 <b>Preparing screenshots...</b>"
        )

        duration, duration_error = await get_video_duration(
            video_path
        )

        if not duration:
            await status.edit_text(
                "❌ <b>Unable to read video duration.</b>\n\n"
                f"<code>{duration_error}</code>"
            )
            return

        # ----------------------------------------------------
        # TIMESTAMPS
        # ----------------------------------------------------

        if duration < 2:
            timestamp_1 = duration * 0.25
            timestamp_2 = duration * 0.65
        else:
            timestamp_1 = duration * 0.30
            timestamp_2 = duration * 0.70

        timestamp_1 = max(
            0,
            min(timestamp_1, duration - 0.05)
        )

        timestamp_2 = max(
            0,
            min(timestamp_2, duration - 0.01)
        )

        # ----------------------------------------------------
        # EXTRACT BOTH IN PARALLEL
        # ----------------------------------------------------

        await status.edit_text(
            "⚡ <b>Extracting 2 screenshots...</b>"
        )

        results = await asyncio.gather(
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

        # ----------------------------------------------------
        # DELETE ORIGINAL VIDEO IMMEDIATELY
        # ----------------------------------------------------

        try:
            if os.path.isfile(video_path):
                os.remove(video_path)
                logger.info(
                    "Original video deleted after extraction."
                )
        except Exception:
            logger.exception(
                "Could not delete original video."
            )

        # Delete the temporary Telegram message.
        if cached_message:
            try:
                await cached_message.delete()
                cached_message = None
            except Exception:
                logger.warning(
                    "Could not delete temporary Telegram message."
                )

        # ----------------------------------------------------
        # VERIFY SCREENSHOTS
        # ----------------------------------------------------

        for index, (success, error) in enumerate(
            results,
            start=1
        ):
            if not success:
                await status.edit_text(
                    f"❌ <b>Screenshot {index} failed.</b>\n\n"
                    f"<code>{error}</code>"
                )
                return

        # ----------------------------------------------------
        # SEND PHOTOS DIRECTLY TO TELEGRAM
        # ----------------------------------------------------

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

        except Exception:
            # Fallback: send photos individually.
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

        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        await status.edit_text(
            "✅ <b>Screenshots extracted successfully!</b>\n\n"
            "📸 Both screenshots have been sent above.\n"
            "🗑️ Temporary video and screenshots cleaned up."
        )

    except Exception as e:
        logger.exception(
            "Screenshot extraction failed."
        )

        error_text = str(e)

        try:
            if status:
                await status.edit_text(
                    "❌ <b>Screenshot extraction failed.</b>\n\n"
                    f"<code>{error_text}</code>"
                )
            else:
                await query.message.reply_text(
                    "❌ <b>Screenshot extraction failed.</b>\n\n"
                    f"<code>{error_text}</code>"
                )
        except Exception:
            pass

    finally:
        # ----------------------------------------------------
        # DELETE TEMPORARY TELEGRAM MESSAGE IF STILL PRESENT
        # ----------------------------------------------------

        if cached_message:
            try:
                await cached_message.delete()
            except Exception:
                pass

        # ----------------------------------------------------
        # DELETE ALL LOCAL FILES
        # ----------------------------------------------------

        if work_dir and os.path.isdir(work_dir):
            shutil.rmtree(
                work_dir,
                ignore_errors=True
            )
