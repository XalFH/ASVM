import os
import asyncio
import logging
import tempfile
import shutil
import html

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from info import BIN_CHANNEL
from database.ia_filterdb import get_file_details


logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

TEMP_PREFIX = "asvm_ss_"

MAX_CONCURRENT_EXTRACTIONS = 2

_active_tasks = set()
_active_temp_dirs = set()
_task_lock = asyncio.Lock()


# ============================================================
# ADMIN CHECK
# ============================================================

def get_admin_ids():
    admin_ids = set()

    env_admins = os.getenv("ADMINS", "")

    for value in env_admins.split(","):
        value = value.strip()

        if value.isdigit():
            admin_ids.add(int(value))

    try:
        from info import ADMINS

        for value in ADMINS:
            try:
                admin_ids.add(int(value))
            except (ValueError, TypeError):
                pass

    except (ImportError, TypeError):
        pass

    return admin_ids


def is_admin(user_id):
    return user_id in get_admin_ids()


# ============================================================
# FORMAT SIZE
# ============================================================

def human_size(size):
    size = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size:.2f} PB"


# ============================================================
# STORAGE STATISTICS
# ============================================================

def get_storage_stats():
    temp_root = tempfile.gettempdir()

    temp_size = 0
    temp_files = 0
    temp_directories = 0

    try:
        with os.scandir(temp_root) as entries:
            for entry in entries:

                if not entry.name.startswith(TEMP_PREFIX):
                    continue

                if not entry.is_dir(follow_symlinks=False):
                    continue

                temp_directories += 1

                for root, dirs, files in os.walk(entry.path):

                    for filename in files:
                        path = os.path.join(root, filename)

                        try:
                            temp_size += os.path.getsize(path)
                            temp_files += 1
                        except OSError:
                            pass

    except OSError:
        logger.exception("Unable to read temporary storage.")

    try:
        disk = shutil.disk_usage("/")

        disk_total = disk.total
        disk_used = disk.used
        disk_free = disk.free

    except OSError:
        disk_total = 0
        disk_used = 0
        disk_free = 0

    return {
        "temp_size": temp_size,
        "temp_files": temp_files,
        "temp_directories": temp_directories,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
    }


# ============================================================
# RAM STATISTICS
# ============================================================

def get_ram_stats():
    try:
        values = {}

        with open("/proc/meminfo", "r", encoding="utf-8") as file:
            for line in file:
                key, value = line.split(":", 1)

                parts = value.strip().split()

                if parts and parts[0].isdigit():
                    values[key] = int(parts[0]) * 1024

        total = values.get("MemTotal", 0)

        available = values.get(
            "MemAvailable",
            values.get("MemFree", 0),
        )

        if not total:
            return "Unavailable"

        used_percent = (
            (total - available) / total
        ) * 100

        return (
            f"Total: {human_size(total)}\n"
            f"Available: {human_size(available)}\n"
            f"Used: {used_percent:.1f}%"
        )

    except Exception:
        logger.exception("Unable to read RAM statistics.")
        return "Unavailable"


# ============================================================
# STATS MESSAGE
# ============================================================

async def build_stats_message():
    stats = get_storage_stats()

    async with _task_lock:
        active_tasks = len(_active_tasks)

    disk_percent = (
        f"{stats['disk_used'] / stats['disk_total'] * 100:.1f}%"
        if stats["disk_total"]
        else "Unavailable"
    )

    return (
        "📊 <b>KOYEB STORAGE & BOT STATS</b>\n\n"

        "💽 <b>Disk Statistics</b>\n"
        f"Total: {human_size(stats['disk_total'])}\n"
        f"Used: {human_size(stats['disk_used'])} "
        f"({disk_percent})\n"
        f"Free: {human_size(stats['disk_free'])}\n\n"

        "🗂 <b>Bot Temporary Storage</b>\n"
        f"Size: {human_size(stats['temp_size'])}\n"
        f"Files: {stats['temp_files']}\n"
        f"Directories: {stats['temp_directories']}\n\n"

        "🧠 <b>Container RAM</b>\n"
        f"{get_ram_stats()}\n\n"

        "⚡ <b>Active Tasks:</b> "
        f"{active_tasks}\n\n"

        "ℹ️ Disk and RAM statistics describe the container "
        "and may not exactly match Koyeb dashboard billing figures."
    )


# ============================================================
# /KSTATS COMMAND
# ============================================================

@Client.on_message(
    filters.command("kstats", prefixes="/")
)
async def kstats_command(
    client: Client,
    message: Message
):
    try:
        text = await build_stats_message()

        buttons = None

        if message.from_user and is_admin(
            message.from_user.id
        ):
            buttons = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🧹 Clean Storage",
                            callback_data="kclean_confirm",
                        ),
                        InlineKeyboardButton(
                            "🔄 Refresh",
                            callback_data="kstats_refresh",
                        ),
                    ]
                ]
            )

        await message.reply_text(
            text,
            reply_markup=buttons,
        )

    except Exception:
        logger.exception("Kstats command failed.")

        await message.reply_text(
            "❌ Unable to retrieve storage statistics."
        )


# ============================================================
# REFRESH STATS
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^kstats_refresh$")
)
async def kstats_refresh(
    client: Client,
    query: CallbackQuery
):
    if not query.from_user or not is_admin(
        query.from_user.id
    ):
        return await query.answer(
            "Admin only.",
            show_alert=True,
        )

    await query.answer("Refreshing statistics...")

    try:
        text = await build_stats_message()

        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🧹 Clean Storage",
                        callback_data="kclean_confirm",
                    ),
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data="kstats_refresh",
                    ),
                ]
            ]
        )

        await query.message.edit_text(
            text,
            reply_markup=buttons,
        )

    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.exception("Stats refresh failed.")


# ============================================================
# CLEAN STORAGE CONFIRMATION
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^kclean_confirm$")
)
async def kclean_confirm(
    client: Client,
    query: CallbackQuery
):
    if not query.from_user or not is_admin(
        query.from_user.id
    ):
        return await query.answer(
            "Admin only.",
            show_alert=True,
        )

    await query.answer()

    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Cleanup",
                    callback_data="kclean_run",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="kclean_cancel",
                ),
            ]
        ]
    )

    await query.message.reply_text(
        "🧹 <b>Clean Temporary Storage?</b>\n\n"
        "This removes inactive temporary directories "
        "created by the screenshot extractor.\n\n"
        "Running task directories will not be deleted.\n"
        "Your movie database and other bot files are untouched.",
        reply_markup=buttons,
    )


# ============================================================
# CANCEL CLEANUP
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^kclean_cancel$")
)
async def kclean_cancel(
    client: Client,
    query: CallbackQuery
):
    if not query.from_user or not is_admin(
        query.from_user.id
    ):
        return await query.answer(
            "Admin only.",
            show_alert=True,
        )

    await query.answer("Cleanup cancelled.")

    try:
        await query.message.edit_text(
            "❌ Storage cleanup cancelled."
        )
    except Exception:
        pass


# ============================================================
# CLEAN INACTIVE BOT TEMP DIRECTORIES
# ============================================================

@Client.on_callback_query(
    filters.regex(r"^kclean_run$")
)
async def kclean_run(
    client: Client,
    query: CallbackQuery
):
    if not query.from_user or not is_admin(
        query.from_user.id
    ):
        return await query.answer(
            "Admin only.",
            show_alert=True,
        )

    await query.answer("Cleaning temporary storage...")

    removed_dirs = 0
    removed_bytes = 0
    errors = 0

    root = tempfile.gettempdir()

    async with _task_lock:
        active_dirs = set(_active_temp_dirs)

    try:
        entries = list(os.scandir(root))
    except OSError as e:
        return await query.message.reply_text(
            "❌ Unable to scan temporary storage:\n"
            f"<code>{html.escape(str(e))}</code>"
        )

    for entry in entries:
        if not entry.name.startswith(TEMP_PREFIX):
            continue

        if not entry.is_dir(follow_symlinks=False):
            continue

        path = entry.path

        if path in active_dirs:
            continue

        try:
            size = 0

            for base, dirs, files in os.walk(path):
                for filename in files:
                    file_path = os.path.join(base, filename)

                    try:
                        size += os.path.getsize(file_path)
                    except OSError:
                        pass

            shutil.rmtree(path)

            removed_dirs += 1
            removed_bytes += size

        except Exception:
            errors += 1
            logger.exception(
                "Failed to clean directory: %s",
                path,
            )

    await query.message.reply_text(
        "🧹 <b>Cleanup Completed</b>\n\n"
        f"📁 Directories removed: {removed_dirs}\n"
        f"🗑 Space released: {human_size(removed_bytes)}\n"
        f"⚠️ Errors: {errors}\n\n"
        "Only inactive bot temporary directories were considered."
    )


# ============================================================
# SCREENSHOT BUTTON — COMING SOON
# No video downloads, no FFmpeg, no temporary files.
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
            "This feature is coming soon!",
            show_alert=True,
        )
    except Exception:
        pass

    try:
        await query.message.reply_text(
            "🚧 <b>Screenshot Extractor</b>\n\n"
            "This feature is currently under development.\n\n"
            "We're working hard to bring it to you soon. "
            "Stay tuned! ❤️"
        )

    except Exception:
        logger.exception(
            "Unable to send coming-soon message."
        )
