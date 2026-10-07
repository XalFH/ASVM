import os
import asyncio
import logging
import tempfile
import uuid

import aiohttp

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
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
    """
    Upload image to ImgBB.

    Returns:
        str: ImgBB URL
        None: Upload failed
    """

    if not IMGBB_API_KEY:
        logger.error("IMGBB_API_KEY environment variable is missing.")
        return None

    if not file_path or not os.path.isfile(file_path):
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
            data = result.get("data", {})

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
# NORMAL /img COMMAND
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

    # Only images
    if not reply.photo and not reply.document:
        return await message.reply_text(
            "Please reply to an image."
        )

    # Document size limit
    if reply.document:

        file_size = (
            reply.document.file_size
            or 0
        )

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
                "❌ Something went wrong during download."
            )

        image_url = await upload_to_imgbb(
            downloaded_media
        )

        if not image_url:
            return await msg.edit_text(
                "❌ ImgBB upload failed.\n\n"
                "Please try again later."
            )

        await msg.edit_text(
            "🖼️ <b>ImgBB URL:</b>\n\n"
            f"<code>{image_url}</code>"
        )

    except Exception as e:

        logger.exception(
            "Image upload error: %s",
            e
        )

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
# FFMPEG HELPERS
# ============================================================

async def run_command(*args):
    """
    Run an async subprocess command.
    """

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


async def get_video_duration(
    video_path: str
):
    """
    Get video duration using ffprobe.
    """

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

        return float(stdout.strip())

    except Exception as e:

        logger.exception(
            "Duration error: %s",
            e
        )

        return None


async def extract_screenshot(
    video_path: str,
    output_path: str,
    timestamp: float
):
    """
    Extract one JPG screenshot using FFmpeg.
    """

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
            "-vf",
            "scale='min(1280,iw)':-2",
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

        logger.exception(
            "Screenshot extraction error: %s",
            e
        )

        return False


# ============================================================
# SAFE MESSAGE EDIT
# ============================================================

async def safe_edit_reply_markup(
    query: CallbackQuery,
    markup
):
    """
    Safely edit inline keyboard.

    Prevents:
    MESSAGE_NOT_MODIFIED
    """

    try:

        current_markup = (
            query.message.reply_markup
            if query.message
            else None
        )

        if current_markup == markup:
            return

        await query.edit_message_reply_markup(
            reply_markup=markup
        )

    except Exception as e:

        if "MESSAGE_NOT_MODIFIED" in str(e):
            return

        raise


# ============================================================
# SCREENSHOT CALLBACK
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^screenshots:")
)
async def screenshots_handler(
    client: Client,
    query: CallbackQuery
):

    # Answer callback immediately
    try:
        await query.answer(
            "📸 Extracting screenshots...",
            show_alert=False
        )
    except Exception:
        pass

    # --------------------------------------------------------
    # Get file ID
    # --------------------------------------------------------

    try:

        file_id = query.data.split(
            ":",
            1
        )[1]

    except Exception:

        try:
            await query.answer(
                "Invalid screenshot request.",
                show_alert=True
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # Prevent repeated clicks
    # --------------------------------------------------------

    wait_markup = None

    try:

        current_markup = (
            query.message.reply_markup
        )

        if current_markup:

            new_keyboard = []

            for row in current_markup.inline_keyboard:

                new_row = []

                for button in row:

                    if (
                        button.callback_data
                        == query.data
                    ):

                        new_row.append(
                            InlineKeyboardButton(
                                "• ᴇxᴛʀᴀᴄᴛɪɴɢ... •",
                                callback_data="screenshot_wait"
                            )
                        )

                    else:

                        new_row.append(button)

                new_keyboard.append(new_row)

            wait_markup = InlineKeyboardMarkup(
                new_keyboard
            )

            await safe_edit_reply_markup(
                query,
                wait_markup
            )

    except Exception as e:

        logger.warning(
            "Could not update screenshot button: %s",
            e
        )

    # --------------------------------------------------------
    # Temporary directory
    # --------------------------------------------------------

    work_dir = tempfile.mkdtemp(
        prefix="asvm_ss_"
    )

    video_path = os.path.join(
        work_dir,
        f"video_{uuid.uuid4().hex}.mp4"
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

        # ----------------------------------------------------
        # Check DB
        # ----------------------------------------------------

        files_ = await get_file_details(
            file_id
        )

        if not files_:

            try:
                await query.message.reply_text(
                    "❌ File not found in database."
                )
            except Exception:
                pass

            return

        file_info = files_[0]

        # ----------------------------------------------------
        # Make sure this is video
        # ----------------------------------------------------

        file_type = getattr(
            file_info,
            "file_type",
            None
        )

        if file_type != "video":

            try:
                await query.message.reply_text(
                    "❌ Screenshots are available for video files only."
                )
            except Exception:
                pass

            return

        # ----------------------------------------------------
        # Get Telegram cached media
        # ----------------------------------------------------

        try:

            cached_message = (
                await client.send_cached_media(
                    chat_id=BIN_CHANNEL,
                    file_id=file_id
                )
            )

        except Exception as e:

            logger.exception(
                "Failed to fetch cached media: %s",
                e
            )

            try:
                await query.message.reply_text(
                    "❌ Unable to fetch the video from Telegram."
                )
            except Exception:
                pass

            return

        # ----------------------------------------------------
        # Download video
        # ----------------------------------------------------

        try:

            async with aiofiles.open(
                video_path,
                "wb"
            ) as output:

                async for chunk in client.stream_media(
                    cached_message,
                    limit=5
                ):
                    await output.write(chunk)

        except NameError:

            # aiofiles wasn't imported
            # fallback to Telegram download
            downloaded = await cached_message.download(
                file_name=video_path
            )

            if downloaded:
                video_path = downloaded

        # ----------------------------------------------------
        # Check downloaded file
        # ----------------------------------------------------

        if (
            not os.path.exists(video_path)
            or os.path.getsize(video_path) == 0
        ):

            try:
                await query.message.reply_text(
                    "❌ Video download failed."
                )
            except Exception:
                pass

            return

        # ----------------------------------------------------
        # Get duration
        # ----------------------------------------------------

        duration = await get_video_duration(
            video_path
        )

        if not duration or duration <= 0:

            try:
                await query.message.reply_text(
                    "❌ Unable to read video duration."
                )
            except Exception:
                pass

            return

        # ----------------------------------------------------
        # Screenshot positions
        # ----------------------------------------------------

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

        # Don't exceed duration
        timestamp_1 = min(
            timestamp_1,
            max(0, duration - 0.2)
        )

        timestamp_2 = min(
            timestamp_2,
            max(0, duration - 0.1)
        )

        # ----------------------------------------------------
        # Extract both screenshots
        # ----------------------------------------------------

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

            try:
                await query.message.reply_text(
                    "❌ FFmpeg could not extract the screenshots."
                )
            except Exception:
                pass

            return

        # ----------------------------------------------------
        # Upload both to ImgBB
        # ----------------------------------------------------

        img_1, img_2 = await asyncio.gather(

            upload_to_imgbb(
                screenshot_1
            ),

            upload_to_imgbb(
                screenshot_2
            )
        )

        if not img_1 or not img_2:

            try:
                await query.message.reply_text(
                    "❌ Screenshot extraction succeeded, "
                    "but ImgBB upload failed."
                )
            except Exception:
                pass

            return

        # ----------------------------------------------------
        # Send screenshots to Telegram
        # ----------------------------------------------------

        media = [
            InputMediaPhoto(
                screenshot_1,
                caption="📸 Screenshot 1"
            ),
            InputMediaPhoto(
                screenshot_2,
                caption="📸 Screenshot 2"
            )
        ]

        try:

            await client.send_media_group(
                chat_id=query.message.chat.id,
                media=media
            )

        except Exception as e:

            logger.exception(
                "Failed to send screenshots to Telegram: %s",
                e
            )

            # Fallback: send separately
            try:
                await client.send_photo(
                    query.message.chat.id,
                    screenshot_1,
                    caption="📸 Screenshot 1"
                )

                await client.send_photo(
                    query.message.chat.id,
                    screenshot_2,
                    caption="📸 Screenshot 2"
                )

            except Exception:
                pass

        # ----------------------------------------------------
        # ImgBB URL buttons
        # ----------------------------------------------------

        url_buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🖼️ Screenshot 1",
                        url=img_1
                    ),
                    InlineKeyboardButton(
                        "🖼️ Screenshot 2",
                        url=img_2
                    )
                ]
            ]
        )

        await client.send_message(
            chat_id=query.message.chat.id,
            text=(
                "<b>📸 Screenshots extracted successfully!</b>\n\n"
                "Two screenshots have been extracted and "
                "uploaded to ImgBB."
            ),
            reply_markup=url_buttons
        )

        # ----------------------------------------------------
        # Restore original keyboard
        # ----------------------------------------------------

        try:

            original
