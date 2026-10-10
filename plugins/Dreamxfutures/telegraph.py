import os
import asyncio
import logging
import tempfile
import shutil
import time
import html
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InputMediaPhoto,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from info import BIN_CHANNEL
from database.ia_filterdb import get_file_details

logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

MAX_CONCURRENT_EXTRACTIONS = 2
MAX_VIDEO_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB safety limit
TEMP_PREFIX = "asvm_ss_"

_active_tasks = set()
_task_lock = asyncio.Lock()
_active_temp_dirs = set()


# ============================================================
# ADMIN CHECK
# Configure ADMINS in info.py or as an environment variable.
# Example environment variable: ADMINS=123456789,987654321
# ============================================================

def get_admin_ids():
    value = os.getenv("ADMINS", "")
    ids = set()

    for item in value.split(","):
        item = item.strip()
        if item.isdigit():
            ids.add(int(item))

    try:
        from info import ADMINS

        for item in ADMINS:
            try:
                ids.add(int(item))
            except (ValueError, TypeError):
                pass
    except (ImportError, TypeError):
        pass

    return ids


def is_admin(user_id):
    return user_id in get_admin_ids()


# ============================================================
# STORAGE STATS
# ============================================================

def get_storage_stats():
    temp_root = tempfile.gettempdir()

    total = 0
    file_count = 0
    directories = 0

    try:
        for root, dirs, files in os.walk(temp_root):
            directories += len(dirs)

            # Count only this bot's temporary directories.
            if root == temp_root:
                dirs[:] = [
                    d for d in dirs
                    if d.startswith(TEMP_PREFIX)
                ]

            for filename in files:
                path = os.path.join(root, filename)

                try:
                    if os.path.isfile(path):
                        total += os.path.getsize(path)
                        file_count += 1
                except OSError:
                    pass

    except OSError:
        logger.exception("Unable to inspect temporary storage.")

    disk_total = 0
    disk_used = 0
    disk_free = 0

    try:
        usage = shutil.disk_usage("/")
        disk_total = usage.total
        disk_used = usage.used
        disk_free = usage.free
    except OSError:
        pass

    return {
        "temp_root": temp_root,
        "temp_bytes": total,
        "file_count": file_count,
        "directories": directories,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
    }


def human_size(size):
    size = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size:.2f} PB"


def get_ram_stats():
    result = {
        "total": "Unavailable",
        "available": "Unavailable",
        "used_percent": "Unavailable",
    }

    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as file:
            values = {}

            for line in file:
                parts = line.split(":", 1)

                if len(parts) != 2:
                    continue

                key = parts[0]
                value = parts[1].strip().split()[0]

                if value.isdigit():
                    values[key] = int(value) * 1024

        total = values.get("MemTotal", 0)
        available = values.get(
            "MemAvailable",
            values.get("MemFree", 0),
        )

        if total:
            result = {
                "total": human_size(total),
                "available": human_size(available),
                "used_percent": (
                    f"{((total - available) / total) * 100:.1f}%"
                ),
            }

    except Exception:
        logger.debug("RAM stats unavailable.", exc_info=True)

    return result


# ============================================================
# COMMAND RUNNER
# ============================================================

async def run_command(*args, timeout=90):
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )

    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()

        return (
            -1,
            "",
            f"Command timed out after {timeout} seconds.",
        )

    return (
        process.returncode,
        stdout.decode(errors="ignore"),
        stderr.decode(errors="ignore"),
    )


# ============================================================
# VIDEO DURATION
# ============================================================

async def get_video_duration(video_path):
    code, stdout, stderr = await run_command(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
        timeout=60,
    )

    if code != 0:
        return None, stderr[-1500:]

    try:
        duration = float(stdout.strip())

        if duration <= 0:
            return None, "Invalid video duration."

        return duration, None

    except (TypeError, ValueError):
        return None, "Could not read video duration."


# ============================================================
# EXTRACT SCREENSHOT
# ============================================================

async def extract_screenshot(video_path, output_path, timestamp):
    code, stdout, stderr = await run_command(
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-threads", "1",
        "-y",
        "-ss", str(timestamp),
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "4",
        "-vf", "scale='min(1280,iw)':-2",
        output_path,
        timeout=120,
    )

    if code != 0:
        return False, stderr[-1500:]

    if not os.path.isfile(output_path):
        return False, "Screenshot file was not created."

    if os.path.getsize(output_path) == 0:
        return False, "Screenshot file is empty."

    return True, None


# ============================================================
# NORMAL MEDIA COMMANDS
# ImgBB was removed, so explain this to users.
# ============================================================

@Client.on_message(
    filters.command(
        ["img", "cup", "telegraph"],
        prefixes="/",
    ) & filters.reply
)
async def c_upload(client: Client, message: Message):
    await message.reply_text(
        "ℹ️ ImgBB image uploading has been removed.\n\n"
        "Use the screenshot button to extract video screenshots."
    )


# ============================================================
# /KSTATS
# ============================================================

@Client.on_message(
    filters.command("kstats", prefixes="/")
)
async def kstats_command(client: Client, message: Message):
    stats = get_storage_stats()
    ram = get_ram_stats()

    async with _task_lock:
        active = len(_active_tasks)

    total = stats["disk_total"]
    used = stats["disk_used"]
    free = stats["disk_free"]

    disk_percent = (
        f"{(used / total) * 100:.1f}%"
        if total else "Unavailable"
    )

    text = (
        "📊 <b>KOYEB STORAGE & BOT STATS</b>\n\n"
        f"💽 <b>Disk total:</b> {human_size(total)}\n"
        f"📦 <b>Disk used:</b> {human_size(used)} "
        f"({disk_percent})\n"
        f"🟢 <b>Disk free:</b> {human_size(free)}\n\n"
        f"🗂 <b>Bot temp files:</b> "
        f"{human_size(stats['temp_bytes'])}\n"
        f"📄 <b>Temp file count:</b> {stats['file_count']}\n"
        f"⚡ <b>Active extractions:</b> {active}/"
        f"{MAX_CONCURRENT_EXTRACTIONS}\n\n"
        f"🧠 <b>RAM total:</b> {ram['total']}\n"
        f"🟢 <b>RAM available:</b> {ram['available']}\n"
        f"📈 <b>RAM used:</b> {ram['used_percent']}\n\n"
        "Note: Disk statistics may include the whole container, "
        "not just this bot."
    )

    buttons = None

    if message.from_user and is_admin(message.from_user.id):
        buttons = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "🧹 Clean Bot Temp Storage",
                    callback_data="kclean_confirm",
                ),
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data="kstats_refresh",
                ),
            ]]
        )

    await message.reply_text(
        text,
        reply_markup=buttons,
    )


# ============================================================
# REFRESH STATS
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^kstats_refresh$")
)
async def kstats_refresh(client: Client, query: CallbackQuery):
    if not query.from_user or not is_admin(query.from_user.id):
        return await query.answer(
            "Admin only.",
            show_alert=True,
        )

    stats = get_storage_stats()
    ram = get_ram_stats()

    async with _task_lock:
        active = len(_active_tasks)

    total = stats["disk_total"]
    used = stats["disk_used"]
    free = stats["disk_free"]

    percent = (
        f"{used / total * 100:.1f}%"
        if total else "Unavailable"
    )

    text = (
        "📊 <b>KOYEB STORAGE & BOT STATS</b>\n\n"
        f"💽 <b>Disk total:</b> {human_size(total)}\n"
        f"📦 <b>Disk used:</b> {human_size(used)} ({percent})\n"
        f"🟢 <b>Disk free:</b> {human_size(free)}\n\n"
        f"🗂 <b>Bot temp files:</b> {human_size(stats['temp_bytes'])}\n"
        f"📄 <b>Temp files:</b> {stats['file_count']}\n"
        f"⚡ <b>Active extractions:</b> {active}/"
        f"{MAX_CONCURRENT_EXTRACTIONS}\n\n"
        f"🧠 <b>RAM total:</b> {ram['total']}\n"
        f"🟢 <b>RAM available:</b> {ram['available']}\n"
        f"📈 <b>RAM used:</b> {ram['used_percent']}"
    )

    buttons = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🧹 Clean Bot Temp Storage",
                callback_data="kclean_confirm",
            ),
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="kstats_refresh",
            ),
        ]]
    )

    await query.answer("Stats refreshed.")

    try:
        await query.message.edit_text(
            text,
            reply_markup=buttons,
        )
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.warning("Stats refresh failed: %s", e)


# ============================================================
# CLEAN STORAGE CONFIRMATION
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^kclean_confirm$")
)
async def kclean_confirm(client: Client, query: CallbackQuery):
    if not query.from_user or not is_admin(query.from_user.id):
        return await query.answer(
            "Admin only.",
            show_alert=True,
        )

    buttons = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Confirm Clean",
                callback_data="kclean_run",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="kclean_cancel",
            ),
        ]]
    )

    await query.answer()

    await query.message.reply_text(
        "⚠️ <b>Clean Bot Temporary Storage?</b>\n\n"
        "This will remove old ASVM screenshot temporary directories "
        "that are not currently being used.\n\n"
        "Running extraction tasks will be preserved.",
        reply_markup=buttons,
    )


@Client.on_callback_query(
    filters.regex(r"^kclean_cancel$")
)
async def kclean_cancel(client: Client, query: CallbackQuery):
    if not query.from_user or not is_admin(query.from_user.id):
        return await query.answer("Admin only.", show_alert=True)

    await query.answer("Cancelled.")

    try:
        await query.message.edit_text(
            "❌ Storage cleanup cancelled."
        )
    except Exception:
        pass


# ============================================================
# CLEAN BOT TEMP STORAGE
# Only deletes this bot's inactive temporary directories.
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^kclean_run$")
)
async def kclean_run(client: Client, query: CallbackQuery):
    if not query.from_user or not is_admin(query.from_user.id):
        return await query.answer("Admin only.", show_alert=True)

    await query.answer("Cleaning temporary storage...")

    root = tempfile.gettempdir()
    removed_dirs = 0
    removed_bytes = 0
    errors = 0

    async with _task_lock:
        active_dirs = set(_active_temp_dirs)

    try:
        entries = os.scandir(root)
    except OSError as e:
        return await query.message.reply_text(
            f"❌ Cannot scan temporary storage: <code>{html.escape(str(e))}</code>"
        )

    with entries:
        for entry in entries:
            if not entry.name.startswith(TEMP_PREFIX):
                continue

            path = entry.path

            # Never touch directories used by active tasks.
            if path in active_dirs:
                continue

            try:
                if entry.is_dir(follow_symlinks=False):
                    size = 0

                    for base, dirs, files in os.walk(path):
                        for name in files:
                            file_path = os.path.join(base, name)

                            try:
                                size += os.path.getsize(file_path)
                            except OSError:
                                pass

                    shutil.rmtree(path)

                    removed_dirs += 1
                    removed_bytes += size

            except Exception:
                errors += 1
                logger.exception("Temporary cleanup failed: %s", path)

    await query.message.reply_text(
        "🧹 <b>Bot Temporary Storage Cleanup</b>\n\n"
        f"📁 Directories removed: {removed_dirs}\n"
        f"🗑 Space released: {human_size(removed_bytes)}\n"
        f"⚠️ Errors: {errors}\n\n"
        "Only inactive ASVM temporary directories were considered."
    )


# ============================================================
# SCREENSHOT EXTRACTOR
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^screenshots:")
)
async def screenshots_handler(client: Client, query: CallbackQuery):
    work_dir = None
    cached_message = None
    status = None
    task_id = None

    # Limit concurrent extractions.
    async with _task_lock:
        if len(_active_tasks) >= MAX_CONCURRENT_EXTRACTIONS:
            return await query.answer(
                "⚠️ Bot is busy. Try again shortly.",
                show_alert=True,
            )

        task_id = id(query)
        _active_tasks.add(task_id)

    try:
        await query.answer("Preparing screenshots...")

        file_id = query.data.split(":", 1)[1]

        status = await query.message.reply_text(
            "📸 <b>Screenshot Extractor</b>\n\n"
            "⏳ Getting video..."
        )

        files_ = await get_file_details(file_id)

        if not files_:
            return await status.edit_text(
                "❌ File not found in database."
            )

        file_info = files_[0]

        if getattr(file_info, "file_type", None) != "video":
            return await status.edit_text(
                "❌ Screenshots are available for videos only."
            )

        work_dir = tempfile.mkdtemp(prefix=TEMP_PREFIX)

        async with _task_lock:
            _active_temp_dirs.add(work_dir)

        video_path = os.path.join(work_dir, "video")
        screenshot_1 = os.path.join(work_dir, "screenshot_1.jpg")
        screenshot_2 = os.path.join(work_dir, "screenshot_2.jpg")

        await status.edit_text("📥 <b>Downloading video...</b>")

        cached_message = await client.send_cached_media(
            chat_id=BIN_CHANNEL,
            file_id=file_id,
        )

        if not cached_message:
            return await status.edit_text(
                "❌ Could not get video from Telegram."
            )

        downloaded = await cached_message.download(
            file_name=video_path
        )

        if not downloaded or not os.path.isfile(downloaded):
            return await status.edit_text(
                "❌ Video download failed."
            )

        video_path = downloaded
        video_size = os.path.getsize(video_path)

        if video_size == 0:
            return await status.edit_text(
                "❌ Downloaded video is empty."
            )

        if video_size > MAX_VIDEO_SIZE:
            return await status.edit_text(
                "⚠️ Video exceeds the 2 GB processing safety limit."
            )

        await status.edit_text("🎬 <b>Reading video duration...</b>")

        duration, error = await get_video_duration(video_path)

        if not duration:
            return await status.edit_text(
                "❌ <b>Unable to read video duration.</b>\n\n"
                f"<code>{html.escape(error or 'Unknown error')}</code>"
            )

        timestamp_1 = min(
            duration * 0.30,
            max(0, duration - 0.05),
        )

        timestamp_2 = min(
            duration * 0.70,
            max(0, duration - 0.01),
        )

        await status.edit_text(
            "⚡ <b>Extracting screenshots...</b>"
        )

        results = await asyncio.gather(
            extract_screenshot(
                video_path,
                screenshot_1,
                timestamp_1,
            ),
            extract_screenshot(
                video_path,
                screenshot_2,
                timestamp_2,
            ),
        )

        # Delete original video as soon as extraction completes.
        try:
            if os.path.isfile(video_path):
                os.remove(video_path)
        except OSError:
            logger.exception("Unable to delete original video.")

        if cached_message:
            try:
                await cached_message.delete()
                cached_message = None
            except Exception:
                logger.warning("Unable to delete temporary BIN_CHANNEL message.")

        for index, (success, error) in enumerate(results, start=1):
            if not success:
                return await status.edit_text(
                    f"❌ <b>Screenshot {index} failed.</b>\n\n"
                    f"<code>{html.escape(error or 'Unknown error')}</code>"
                )

        await status.edit_text("📤 <b>Sending screenshots...</b>")

        await client.send_media_group(
            chat_id=query.message.chat.id,
            media=[
                InputMediaPhoto(
                    screenshot_1,
                    caption="📸 Screenshot 1",
                ),
                InputMediaPhoto(
                    screenshot_2,
                    caption="📸 Screenshot 2",
                ),
            ],
        )

        await status.edit_text(
            "✅ <b>Screenshots extracted successfully!</b>\n\n"
            "📸 Both screenshots have been sent.\n"
            "🗑 Temporary files will now be removed."
        )

    except Exception as e:
        logger.exception("Screenshot extraction failed.")

        error_text = html.escape(str(e))[:2500]

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
        if cached_message:
            try:
                await cached_message.delete()
            except Exception:
                pass

        if work_dir:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                logger.exception("Temporary directory cleanup failed.")

            async with _task_lock:
                _active_temp_dirs.discard(work_dir)

        async with _task_lock:
            _active_tasks.discard(task_id)
