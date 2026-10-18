import logging
from utils import get_random_mix_id, get_size, is_subscribed, is_req_subscribed, group_setting_buttons, get_poster, get_posterx, temp, get_settings, save_group_settings, get_cap, imdb, is_check_admin, extract_request_content, log_error, clean_filename, generate_season_variations, clean_search_text, get_settings_text
from rapidfuzz import process
from dreamxbotz.util.file_properties import get_name, get_hash
from urllib.parse import quote_plus

from database.ia_filterdb import Media, Media2, get_search_results, get_bad_files
from database.config_db import mdb
from pyrogram.errors import MessageIdInvalid, UserIsBlocked, MessageNotModified, PeerIdInvalid, MessageDeleteForbidden
from pyrogram import Client, filters, enums
from pyrogram.types import LinkPreviewOptions, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto
from info import (
    ADMINS, AUTH_CHANNELS, AUTH_REQ_CHANNELS, BIN_CHANNEL, DELETE_TIME,
    EMOJI_MODE, GRP_LNK, LANDSCAPE_POSTER, LANGUAGES, LOG_CHANNEL, MAX_B_TN, MSG_ALRT,
    MULTIPLE_DB, NO_RESULTS_MSG, OWNER_LNK, OWNER_UPI_ID, PICS, PICS_URL, QR_CODE, QUALITIES,
    REACTIONS, REQST_CHANNEL, SEASONS, STAR_PREMIUM_PLANS, SUBSCRIPTION, SUPPORT_CHAT_ID,
    TMDB_ON_SEARCH, TMDB_POSTER, ULTRA_FAST_MODE, UPDATE_CHNL_LNK, URL
)
from Script import script
from pyrogram.errors.exceptions.bad_request_400 import MediaEmpty, PhotoInvalidDimensions, WebpageMediaEmpty
from database.refer import referdb
from database.users_chats_db import db
import asyncio
import re
import math
import random
import pytz
import json
import html
import time
import uuid
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
lock = asyncio.Lock()

logger = logging.getLogger(__name__)

TIMEZONE = "Asia/Kolkata"
BUTTON = {}
BUTTONS = {}
FRESH = {}
BUTTONS0 = {}
BUTTONS1 = {}
BUTTONS2 = {}
SPELL_CHECK = {}


# ================= PREMIUM PAYMENT API =================
# The API creates an order and returns payment_url + status_check_url.
# Price is stored in MongoDB when available; an in-memory fallback keeps the
# bot usable if the optional settings collection is not exposed by this fork.
PAYMENT_CREATE_API = "https://checkout.ascinemaa.bond/api.php"
PAYMENT_API_KEY = "heyupi"
DEFAULT_MONTHLY_PREMIUM_PRICE = 20
_PAYMENT_MEMORY = {"monthly_price": DEFAULT_MONTHLY_PREMIUM_PRICE, "orders": {}}
_PAYMENT_LOCKS = {}


async def _payment_api_get(url):
    def _request():
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "PremiumPaymentBot/1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw)

    return await asyncio.to_thread(_request)


async def _get_monthly_price():
    try:
        return await db.get_premium_monthly_price(DEFAULT_MONTHLY_PREMIUM_PRICE)
    except Exception:
        logger.exception("Could not read premium monthly price from database")
        return int(_PAYMENT_MEMORY.get("monthly_price", DEFAULT_MONTHLY_PREMIUM_PRICE))


async def _set_monthly_price(amount):
    amount = int(amount)
    # Persist to MongoDB through users_chats_db.py; only fall back to memory
    # if the database operation itself fails.
    try:
        await db.set_premium_monthly_price(amount)
    except Exception:
        logger.exception("Could not persist premium monthly price")
        _PAYMENT_MEMORY["monthly_price"] = amount
        raise
    _PAYMENT_MEMORY["monthly_price"] = amount


async def _save_payment_order(token, order):
    try:
        await db.save_premium_payment_order(token, order)
    except Exception:
        logger.exception("Could not persist premium payment order")
        # Do not silently continue with a non-persistent order: a restart could
        # otherwise lose the order and prevent payment verification.
        raise
    _PAYMENT_MEMORY["orders"][token] = dict(order)


async def _get_payment_order(token):
    try:
        row = await db.get_premium_payment_order(token)
        if row:
            return row
    except Exception:
        logger.exception("Could not read premium payment order")
    return _PAYMENT_MEMORY["orders"].get(token)


async def _update_payment_order(token, changes):
    try:
        await db.update_premium_payment_order(token, changes)
    except Exception:
        logger.exception("Could not update premium payment order in MongoDB")
        raise
    order = _PAYMENT_MEMORY["orders"].get(token, {"_id": str(token), "token": str(token)})
    order.update(changes)
    _PAYMENT_MEMORY["orders"][token] = order

async def _claim_payment_order(token):
    """Atomically claim a pending order so separate bot workers cannot activate it twice."""
    try:
        result = await db.premium_payment_orders.update_one(
            {"_id": str(token), "status": "pending"},
            {"$set": {"status": "processing", "processing_started_at": int(time.time())}},
        )
        if result.modified_count == 1:
            order = _PAYMENT_MEMORY["orders"].get(token, {"_id": str(token), "token": str(token)})
            order.update({"status": "processing", "processing_started_at": int(time.time())})
            _PAYMENT_MEMORY["orders"][token] = order
            return True
        return False
    except Exception:
        logger.exception("Could not claim premium payment order")
        raise


def _expiry_to_datetime(value):
    """Normalize stored expiry values to a naive datetime used by this database."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value))
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
            except (TypeError, ValueError):
                return None
    return None


async def _activate_paid_premium(user_id, months):
    """Activate premium using the exact users_chats_db Database API/schema."""
    user_id = int(user_id)
    now = datetime.now()
    current_expiry = None
    user = await db.get_user(user_id)
    if isinstance(user, dict):
        current_expiry = _expiry_to_datetime(user.get("expiry_time"))
    start = current_expiry if current_expiry and current_expiry > now else now
    expiry = start + timedelta(days=int(months) * 30)
    # update_user accepts a single user_data dict; expiry_time is a datetime.
    await db.update_user({"id": user_id, "expiry_time": expiry})
    return expiry


async def _is_admin_user(user_id):
    try:
        return int(user_id) in {int(admin) for admin in ADMINS}
    except Exception:
        return str(user_id) in {str(admin) for admin in ADMINS}


@Client.on_message(filters.private & filters.command("pset"))
async def set_premium_monthly_price(client, message):
    if not message.from_user or not await _is_admin_user(message.from_user.id):
        return await message.reply_text("⛔ You are not authorized to change the premium price.")
    if len(message.command) != 2:
        return await message.reply_text("Usage: <code>/pset 20</code>\nSets the premium price per month in INR.", parse_mode=enums.ParseMode.HTML)
    try:
        amount = int(message.command[1])
        if amount < 1 or amount > 100000:
            raise ValueError
    except ValueError:
        return await message.reply_text("❌ Enter a valid amount between ₹1 and ₹100000.")
    await _set_monthly_price(amount)
    await message.reply_text(f"✅ Premium monthly price set to <b>₹{amount}</b>.", parse_mode=enums.ParseMode.HTML)


def _ist_datetime(value):
    """Format Unix timestamps/datetimes in India Standard Time."""
    ist = pytz.timezone(TIMEZONE)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), ist)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Database datetimes are stored naive; treat them as UTC only when
            # they came from Unix timestamps, otherwise preserve local fields.
            return ist.localize(value)
        return value.astimezone(ist)
    if isinstance(value, str):
        try:
            return _ist_datetime(float(value))
        except (TypeError, ValueError):
            try:
                return _ist_datetime(datetime.fromisoformat(value))
            except (TypeError, ValueError):
                return None
    return None


async def _plan_details_text(user_id):
    user = None
    try:
        user = await db.get_user(int(user_id))
    except Exception:
        logger.exception("Could not read user premium plan: %s", user_id)

    expiry = _expiry_to_datetime(user.get("expiry_time")) if isinstance(user, dict) else None
    now = datetime.now()
    active = bool(expiry and expiry > now)
    orders = []
    try:
        cursor = db.premium_payment_orders.find({"user_id": int(user_id), "status": "success"}).sort("verified_at", -1)
        async for row in cursor:
            orders.append(row)
    except Exception:
        logger.exception("Could not load premium purchase history for %s", user_id)

    latest = orders[0] if orders else {}
    purchase_ts = latest.get("verified_at") or latest.get("created_at")
    purchase_dt = _ist_datetime(purchase_ts)
    expiry_text = expiry.strftime("%d %b %Y") if expiry else "Not available"
    expiry_time = expiry.strftime("%I:%M %p") if expiry else "Not available"
    buy_date = purchase_dt.strftime("%d %b %Y") if purchase_dt else "Not available"
    buy_time = purchase_dt.strftime("%I:%M %p") if purchase_dt else "Not available"
    months = int(latest.get("months", 0) or 0)
    extra_days = int((user or {}).get("extra_premium_days", latest.get("extra_days", 0)) or 0) if isinstance(user, dict) else int(latest.get("extra_days", 0) or 0)
    remaining_days = max(0, (expiry.date() - now.date()).days) if active and expiry else 0
    if active:
        status = "🟢 Active"
        plan_name = f"Premium · {months} month(s)" if months else "Premium · Admin/Manual grant"
    else:
        status = "🔴 Expired / Inactive"
        plan_name = f"Premium · {months} month(s)" if months else "No active plan"

    return (
        "💎 <b>YOUR PREMIUM PLAN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Current Plan:</b> {html.escape(plan_name)}\n"
        f"📊 <b>Status:</b> {status}\n\n"
        f"🛒 <b>Buy Date:</b> <code>{html.escape(buy_date)}</code>\n"
        f"🕒 <b>Buy Time:</b> <code>{html.escape(buy_time)}</code>\n\n"
        f"📅 <b>End Date:</b> <code>{html.escape(expiry_text)}</code>\n"
        f"🕒 <b>End Time:</b> <code>{html.escape(expiry_time)}</code>\n"
        f"🎁 <b>Extra Premium:</b> <code>{extra_days} day(s)</code>\n"
        f"⏳ <b>Days Remaining:</b> <code>{remaining_days}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Use <b>History</b> to view previous successful purchases."
    )


@Client.on_message(filters.private & filters.command("myplan"))
async def my_premium_plan(client, message):
    if not message.from_user:
        return
    command_id = message.id
    text = await _plan_details_text(message.from_user.id)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 History", callback_data=f"plan_history#{command_id}"),
         InlineKeyboardButton("🔙 Back", callback_data=f"plan_back#{command_id}")],
        [InlineKeyboardButton("💎 Buy Plan", callback_data="pay_plan_menu")],
    ])
    await message.reply_text(text, reply_markup=buttons, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^plan_(history|back|historyback)#\d+$"))
async def premium_plan_history_callbacks(client, query):
    action, command_id = query.data.split("#", 1)
    command_id = int(command_id)
    if action == "back":
        # Delete both the /myplan command and its panel, as requested.
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            await client.delete_messages(query.message.chat.id, command_id)
        except Exception:
            pass
        return await query.answer("Closed.")

    if action == "historyback":
        text = await _plan_details_text(query.from_user.id)
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 History", callback_data=f"plan_history#{command_id}"),
             InlineKeyboardButton("🔙 Back", callback_data=f"plan_back#{command_id}")],
            [InlineKeyboardButton("💎 Buy Plan", callback_data="pay_plan_menu")],
        ])
        await query.message.edit_text(text, reply_markup=buttons, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
        return await query.answer()

    rows = []
    try:
        cursor = db.premium_payment_orders.find({"user_id": int(query.from_user.id), "status": "success"}).sort("verified_at", -1)
        async for row in cursor:
            when = _ist_datetime(row.get("verified_at") or row.get("created_at"))
            buy_date = when.strftime("%d %b %Y") if when else "Unknown"
            buy_time = when.strftime("%I:%M %p") if when else "Unknown"
            expiry = _ist_datetime(row.get("premium_expiry"))
            expiry_text = expiry.strftime("%d %b %Y, %I:%M %p") if expiry else "See current plan"
            months = int(row.get("months", 0) or 0)
            amount = row.get("amount", "N/A")
            order_id = html.escape(str(row.get("order_id", "N/A")))
            txn = html.escape(str(row.get("transaction_id") or "N/A"))
            rows.append(
                f"<b>#{len(rows) + 1} · Premium Purchase</b>\n"
                f"🛒 Bought: <code>{buy_date}</code> · <code>{buy_time}</code>\n"
                f"📦 Plan: <b>{months} month(s)</b>\n"
                f"💰 Paid: <b>₹{html.escape(str(amount))}</b>\n"
                f"📅 Expiry: <code>{html.escape(expiry_text)}</code>\n"
                f"🧾 Order: <code>{order_id}</code>\n"
                f"🔖 Transaction: <code>{txn}</code>"
            )
    except Exception:
        logger.exception("Could not load premium history for %s", query.from_user.id)

    history_text = "📜 <b>PREMIUM PURCHASE HISTORY</b>\n━━━━━━━━━━━━━━━━━━\n\n"
    history_text += "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(rows) if rows else "No successful purchase history found yet.\n\nNew verified purchases will appear here."
    # Telegram text limit safety: keep history within one editable panel.
    if len(history_text) > 3800:
        history_text = history_text[:3700] + "\n\n<i>Showing recent history only.</i>"
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Plan", callback_data=f"plan_historyback#{command_id}")]])
    await query.message.edit_text(history_text, reply_markup=buttons, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
    await query.answer()


# -------------------- Admin premium management commands --------------------
async def _premium_admin_required(message):
    if not message.from_user or not await _is_admin_user(message.from_user.id):
        await message.reply_text("⛔ You are not authorized to use premium management commands.")
        return False
    return True


def _premium_expiry_text(user):
    expiry = _expiry_to_datetime(user.get("expiry_time")) if isinstance(user, dict) else None
    if not expiry:
        return None, False
    active = expiry > datetime.now()
    return expiry.strftime("%d %b %Y, %I:%M %p"), active


@Client.on_message(filters.private & filters.command(["addpremium", "add_premium", "addprem", "add"]))
async def admin_add_premium(client, message):
    """Usage: /addpremium <user_id> <days>, or reply to a user with /addpremium <days>."""
    if not await _premium_admin_required(message):
        return

    args = message.command[1:]
    target_id = None
    days = None
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = int(message.reply_to_message.from_user.id)
            if len(args) == 1:
                days = int(args[0])
        elif len(args) == 2:
            target_id, days = int(args[0]), int(args[1])
    except (TypeError, ValueError):
        target_id, days = None, None

    if not target_id or not days or days < 1 or days > 3650:
        return await message.reply_text(
            "Usage:\n<code>/addpremium USER_ID DAYS</code>\n"
            "Or reply to a user's message with <code>/addpremium DAYS</code>.\n"
            "Days must be between 1 and 3650.",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        user = await db.get_user(target_id) or {"id": target_id}
        current_expiry = _expiry_to_datetime(user.get("expiry_time"))
        now = datetime.now()
        start = current_expiry if current_expiry and current_expiry > now else now
        expiry = start + timedelta(days=days)
        # Track admin-granted bonus days so /myplan can display Extra Premium.
        previous_extra = int(user.get("extra_premium_days", 0) or 0)
        await db.update_user({
            "id": target_id,
            "expiry_time": expiry,
            "extra_premium_days": previous_extra + days,
        })
        await message.reply_text(
            f"✅ Premium added successfully.\n\n"
            f"👤 User ID: <code>{target_id}</code>\n"
            f"📆 Added: <b>{days} days</b>\n"
            f"⏳ Expires: <code>{expiry.strftime('%d %b %Y, %I:%M %p')}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        logger.exception("Failed to add premium to user %s", target_id)
        await message.reply_text(f"❌ Could not add premium: <code>{html.escape(str(e)[:300])}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command(["getpremium", "get_premium", "getprem", "get"]))
async def admin_get_premium(client, message):
    if not await _premium_admin_required(message):
        return
    if len(message.command) != 2:
        return await message.reply_text("Usage: <code>/getpremium USER_ID</code>", parse_mode=enums.ParseMode.HTML)
    try:
        user_id = int(message.command[1])
        user = await db.get_user(user_id)
        expiry_text, active = _premium_expiry_text(user)
        if expiry_text and active:
            text = ("✅ <b>Premium is ACTIVE</b>\n\n"
                    f"👤 User ID: <code>{user_id}</code>\n"
                    f"📅 Expires: <code>{expiry_text}</code>")
        elif expiry_text:
            text = ("❌ <b>Premium is EXPIRED</b>\n\n"
                    f"👤 User ID: <code>{user_id}</code>\n"
                    f"📅 Expired at: <code>{expiry_text}</code>")
        else:
            text = f"❌ No premium record found for user <code>{user_id}</code>."
        await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
    except ValueError:
        await message.reply_text("❌ User ID must be a number.")
    except Exception as e:
        logger.exception("Failed to get premium info")
        await message.reply_text(f"❌ Could not get premium info: <code>{html.escape(str(e)[:300])}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command(["premiuminfo", "premium_info", "infopremium", "info"]))
async def admin_premium_info(client, message):
    # Same details as /getpremium; keep the familiar /info command alias.
    await admin_get_premium(client, message)


@Client.on_message(filters.private & filters.command(["totalpremium", "total_premium", "premiumcount", "total"]))
async def admin_total_premium(client, message):
    if not await _premium_admin_required(message):
        return
    try:
        count = await db.all_premium_users()
        await message.reply_text(f"💎 <b>Total active premium users:</b> <code>{count}</code>", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        logger.exception("Failed to count premium users")
        await message.reply_text(f"❌ Could not count premium users: <code>{html.escape(str(e)[:300])}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command(["premiumlist", "premium_list", "listpremium"]))
async def admin_premium_list(client, message):
    if not await _premium_admin_required(message):
        return
    try:
        cursor = await db.get_all_premium_user_data()
        rows = []
        async for user in cursor:
            expiry_text, active = _premium_expiry_text(user)
            if active:
                rows.append(f"• <code>{int(user.get('id', 0))}</code> — <code>{expiry_text}</code>")
        if not rows:
            return await message.reply_text("No active premium users found.")
        # Keep each Telegram message comfortably below the message length limit.
        header = f"💎 <b>Active premium users: {len(rows)}</b>\n\n"
        chunks, current = [], header
        for row in rows:
            if len(current) + len(row) + 1 > 3500:
                chunks.append(current)
                current = "💎 <b>Active premium users (continued)</b>\n\n"
            current += row + "\n"
        if current.strip():
            chunks.append(current)
        for chunk in chunks:
            await message.reply_text(chunk, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        logger.exception("Failed to list premium users")
        await message.reply_text(f"❌ Could not list premium users: <code>{html.escape(str(e)[:300])}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command(["removepremium", "remove_premium", "delpremium"]))
async def admin_remove_premium(client, message):
    if not await _premium_admin_required(message):
        return
    if len(message.command) != 2:
        return await message.reply_text("Usage: <code>/removepremium USER_ID</code>", parse_mode=enums.ParseMode.HTML)
    try:
        user_id = int(message.command[1])
        user = await db.get_user(user_id)
        if not user:
            return await message.reply_text("No premium record found for that user.")
        await db.remove_premium_access(user_id)
        await message.reply_text(f"✅ Premium removed for user <code>{user_id}</code>.", parse_mode=enums.ParseMode.HTML)
    except ValueError:
        await message.reply_text("❌ User ID must be a number.")
    except Exception as e:
        logger.exception("Failed to remove premium")
        await message.reply_text(f"❌ Could not remove premium: <code>{html.escape(str(e)[:300])}</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^(paymonths|checkpay)#"))
async def premium_payment_callbacks(client, query):
    data_parts = query.data.split("#")
    action = data_parts[0]
    user_id = query.from_user.id

    if action == "paymonths":
        try:
            months = int(data_parts[1])
        except (IndexError, ValueError):
            return await query.answer("Invalid plan selected.", show_alert=True)
        if months not in (1, 3, 6, 12):
            return await query.answer("Invalid plan selected.", show_alert=True)
        monthly_price = await _get_monthly_price()
        total_amount = monthly_price * months
        await query.answer("Creating your payment order...")
        try:
            params = urllib.parse.urlencode({"api_key": PAYMENT_API_KEY, "amount": total_amount})
            response = await _payment_api_get(f"{PAYMENT_CREATE_API}?{params}")
            payload = response.get("data", response) if isinstance(response, dict) else {}
            order_id = payload.get("order_id")
            payment_url = payload.get("payment_url")
            status_url = payload.get("status_check_url")
            if not order_id or not payment_url or not status_url:
                raise ValueError("Payment API response is missing order_id, payment_url, or status_check_url")
            token = uuid.uuid4().hex[:12]
            order = {
                "_id": token,
                "token": token,
                "order_id": str(order_id),
                "user_id": int(user_id),
                "months": int(months),
                "extra_days": 0,
                "amount": int(total_amount),
                "payment_url": str(payment_url),
                "status_check_url": str(status_url),
                "status": "pending",
                "created_at": int(time.time()),
            }
            await _save_payment_order(token, order)
            caption = (
                "<b>💳 PREMIUM PAYMENT</b>\n\n"
                f"📦 Plan: <b>{months} month{'s' if months != 1 else ''}</b>\n"
                f"💰 Monthly price: <b>₹{monthly_price}</b>\n"
                f"🧾 Total amount: <b>₹{total_amount}</b>\n"
                f"🆔 Order ID: <code>{order_id}</code>\n\n"
                "1. Tap <b>Pay Now</b> and complete the payment.\n"
                "2. Return here and tap <b>Check Payment</b>.\n"
                "3. Premium activates only after the gateway confirms success."
            )
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Pay Now", url=str(payment_url))],
                [InlineKeyboardButton("✅ Check Payment", callback_data=f"checkpay#{token}")],
                [InlineKeyboardButton("↩️ Back to Plans", callback_data="pay_plan_menu")],
            ])
            # Edit the existing plan message in place. If it is a text message,
            # Telegram cannot turn it into media via edit_message_media, so edit text.
            try:
                await query.message.edit_media(
                    media=InputMediaPhoto(media=SUBSCRIPTION, caption=caption, parse_mode=enums.ParseMode.HTML),
                    reply_markup=buttons,
                )
            except Exception:
                await query.message.edit_text(
                    caption, reply_markup=buttons, parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception as e:
            logger.exception("Could not create premium payment order")
            fallback_buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Manual UPI", callback_data="upi_info")],
                [InlineKeyboardButton("↩️ Back to Plans", callback_data="pay_plan_menu")],
            ])
            try:
                await client.edit_message_media(
                    chat_id=query.message.chat.id,
                    message_id=query.message.id,
                    media=InputMediaPhoto(
                        media=SUBSCRIPTION,
                        caption=("⚠️ <b>Automatic payment is temporarily unavailable.</b>\n\n"
                                 "Please use the existing manual UPI option.\n\n"
                                 f"<code>{str(e)[:250]}</code>"),
                        parse_mode=enums.ParseMode.HTML,
                    ),
                    reply_markup=fallback_buttons,
                )
            except Exception:
                await query.answer("Payment API unavailable. Please use manual UPI.", show_alert=True)
        return

    if action == "checkpay":
        token = data_parts[1] if len(data_parts) > 1 else ""
        order = await _get_payment_order(token)
        if not order:
            return await query.answer("Order not found. Please create a new order.", show_alert=True)
        if int(order.get("user_id", 0)) != int(user_id):
            return await query.answer("This payment order belongs to another user.", show_alert=True)
        if order.get("status") == "success":
            return await query.answer("✅ Premium is already activated for this order.", show_alert=True)

        lock = _PAYMENT_LOCKS.setdefault(token, asyncio.Lock())
        async with lock:
            order = await _get_payment_order(token) or order
            if order.get("status") == "success":
                return await query.answer("✅ Premium is already activated for this order.", show_alert=True)
            if order.get("status") == "processing":
                return await query.answer("⏳ This order is already being processed. Please wait or contact support if premium does not activate.", show_alert=True)
            try:
                status_response = await _payment_api_get(str(order["status_check_url"]))
                status_data = status_response.get("data", status_response) if isinstance(status_response, dict) else {}
                returned_order_id = str(status_data.get("order_id", ""))
                payment_status = str(status_data.get("payment_status", status_data.get("status", "pending"))).lower()
                paid_amount = status_data.get("amount")
                if returned_order_id != str(order.get("order_id")):
                    return await query.answer("❌ Order verification failed. Contact support.", show_alert=True)
                try:
                    amount_matches = paid_amount is not None and round(float(paid_amount), 2) == round(float(order["amount"]), 2)
                except (TypeError, ValueError):
                    amount_matches = False
                if payment_status not in ("success", "successful", "paid", "completed"):
                    return await query.answer("⏳ Payment is still pending. Complete payment and try again.", show_alert=True)
                if not amount_matches:
                    return await query.answer("❌ Amount verification failed. Please contact support.", show_alert=True)
                claimed = await _claim_payment_order(token)
                if not claimed:
                    latest_order = await _get_payment_order(token) or {}
                    if latest_order.get("status") == "success":
                        return await query.answer("✅ Premium is already activated for this order.", show_alert=True)
                    return await query.answer("⏳ This order is already being processed. Please wait a moment and check again.", show_alert=True)
                await _update_payment_order(token, {"transaction_id": status_data.get("transaction_id")})
                try:
                    expiry = await _activate_paid_premium(user_id, int(order["months"]))
                except Exception:
                    await _update_payment_order(token, {"status": "pending", "transaction_id": None})
                    raise
                await _update_payment_order(token, {
                    "status": "success",
                    "transaction_id": status_data.get("transaction_id"),
                    "verified_at": int(time.time()),
                    "premium_expiry": int(expiry.timestamp()),
                    "premium_started_at": int(time.time()),
                })
                expiry_text = expiry.strftime("%d %b %Y, %I:%M %p")

                # Notify the configured log channel only after the gateway confirms
                # payment and premium activation has been saved successfully.
                try:
                    buyer = query.from_user
                    buyer_name = html.escape(buyer.first_name or "Unknown")
                    buyer_username = f"@{html.escape(buyer.username)}" if buyer.username else "N/A"
                    await client.send_message(
                        chat_id=LOG_CHANNEL,
                        text=(
                            "💎 <b>PREMIUM PURCHASE SUCCESSFUL</b>\n\n"
                            f"👤 Buyer: {buyer_name} ({buyer_username})\n"
                            f"🆔 User ID: <code>{int(user_id)}</code>\n"
                            f"📦 Plan: <b>{int(order['months'])} month(s)</b>\n"
                            f"💰 Amount: <b>₹{int(order['amount'])}</b>\n"
                            f"🧾 Order ID: <code>{html.escape(str(order.get('order_id', 'N/A')))}</code>\n"
                            f"🔖 Transaction ID: <code>{html.escape(str(status_data.get('transaction_id') or 'N/A'))}</code>\n"
                            f"📅 Premium expiry: <code>{expiry_text}</code>"
                        ),
                        parse_mode=enums.ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logger.exception("Could not send premium purchase alert to LOG_CHANNEL=%s", LOG_CHANNEL)
                success_text = (
                    "<b>✅ PAYMENT VERIFIED SUCCESSFULLY</b>\n\n"
                    f"📦 Plan: <b>{int(order['months'])} month(s)</b>\n"
                    f"💰 Amount: <b>₹{int(order['amount'])}</b>\n"
                    f"🆔 Order ID: <code>{html.escape(str(order.get('order_id', 'N/A')))}</code>\n"
                    f"🧾 Transaction ID: <code>{html.escape(str(status_data.get('transaction_id') or 'N/A'))}</code>\n\n"
                    f"🎉 Premium activated until <b>{expiry_text}</b>."
                )
                success_buttons = InlineKeyboardMarkup([[InlineKeyboardButton("📅 My Plan", callback_data="myplan_callback")]])
                try:
                    await query.message.edit_caption(success_text, reply_markup=success_buttons, parse_mode=enums.ParseMode.HTML)
                except Exception:
                    try:
                        await query.message.edit_text(success_text, reply_markup=success_buttons, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
                    except Exception:
                        logger.exception("Could not update the existing payment message after success")
                return await query.answer("✅ Payment verified and premium activated!", show_alert=True)
            except Exception as e:
                logger.exception("Premium payment status check failed")
                return await query.answer("⚠️ Could not verify payment right now. Please try again shortly.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^(pay_plan_menu|myplan_callback)$"))
async def premium_payment_menu_callbacks(client, query):
    if query.data == "myplan_callback":
        user = None
        try:
            user = await db.get_user(query.from_user.id)
        except Exception:
            pass
        expiry = _expiry_to_datetime(user.get("expiry_time")) if isinstance(user, dict) else None
        if expiry and expiry > datetime.now():
            exp_text = expiry.strftime("%d %b %Y, %I:%M %p")
            return await query.answer(f"Premium active until {exp_text}", show_alert=True)
        return await query.answer("No active paid premium plan found.", show_alert=True)

    price = await _get_monthly_price()
    caption = f"<b>💎 CHOOSE YOUR PREMIUM PLAN</b>\n\nMonthly price: <b>₹{price}</b>\nSelect the duration below."
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"1 Month · ₹{price}", callback_data="paymonths#1"), InlineKeyboardButton(f"3 Months · ₹{price * 3}", callback_data="paymonths#3")],
        [InlineKeyboardButton(f"6 Months · ₹{price * 6}", callback_data="paymonths#6"), InlineKeyboardButton(f"12 Months · ₹{price * 12}", callback_data="paymonths#12")],
        [InlineKeyboardButton("↩️ Back", callback_data="buy_info")],
    ])
    try:
        # Keep the flow in the same Telegram message; never send a separate payment page.
        await query.message.edit_media(
            media=InputMediaPhoto(media=SUBSCRIPTION, caption=caption, parse_mode=enums.ParseMode.HTML),
            reply_markup=buttons,
        )
    except Exception:
        try:
            await query.message.edit_text(caption, reply_markup=buttons, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
        except Exception as exc:
            logger.exception("Could not edit existing message with premium plans: %s", exc)
    await query.answer()


@Client.on_message(filters.group & filters.text & filters.incoming & ~filters.regex(r"^/"))
async def give_filter(client, message):
    if not message.from_user:
        return
    if EMOJI_MODE:
        try:
            await message.react(emoji=random.choice(REACTIONS), big=True)
        except Exception:
            await message.react(emoji="⚡️")
            pass
    await mdb.update_top_messages(message.from_user.id, message.text)
    if message.chat.id != SUPPORT_CHAT_ID:
        settings = await get_settings(message.chat.id)
        try:
            if settings['auto_ffilter']:
                if re.search(r'https?://\S+|www\.\S+|t\.me/\S+', message.text):
                    if await is_check_admin(client, message.chat.id, message.from_user.id):
                        return
                    return await message.delete()
                await auto_filter(client, message)
        except KeyError:
            await save_group_settings(message.chat.id, 'auto_ffilter', True)
            settings = await get_settings(message.chat.id)
            if settings['auto_ffilter']:
                await auto_filter(client, message)
        except Exception as e:
            logger.exception("Error in auto filter: %s", e)
            pass
    else:
        search = message.text
        _, _, total_results = await get_search_results(chat_id=message.chat.id, query=search.lower(), offset=0, filter=True)
        if total_results == 0:
            return
        await message.reply_text(
            script.ALREADY_AVAILABLE_TXT.format(message.from_user.mention, total_results, search),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔍 ᴊᴏɪɴ ᴀɴᴅ ꜱᴇᴀʀᴄʜ ʜᴇʀᴇ", url=GRP_LNK)]])
        )


@Client.on_message(filters.private & filters.text & filters.incoming & ~filters.regex(r"^/") & ~filters.regex(r"(https?://)?(t\.me|telegram\.me|telegram\.dog)/"))
async def pm_text(bot, message):
    bot_id = temp.ME
    content = message.text
    user = message.from_user.first_name
    user_id = message.from_user.id
    if EMOJI_MODE:
        try:
            await message.react(emoji=random.choice(REACTIONS), big=True)
        except Exception:
            await message.react(emoji="⚡️")
            pass
    if content.startswith(("#")):
        return
    try:
        await mdb.update_top_messages(user_id, content)
        pm_search = await db.pm_search_status(bot_id)
        if pm_search:
            await auto_filter(bot, message)
        else:
            await message.reply_text(
                text=script.PM_SEARCH_DISABLED_TXT.format(user),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 ʀᴇǫᴜᴇsᴛ ʜᴇʀᴇ ", url=GRP_LNK)]])
            )
            await bot.send_message(
                chat_id=LOG_CHANNEL,
                text=script.PM_LOG_TXT.format(user, user_id, content)
            )
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^reffff"))
async def refercall(bot, query):
    btn = [[
        InlineKeyboardButton(
            'invite link', url=f'https://telegram.me/share/url?url=https://t.me/{bot.me.username}?start=reff_{query.from_user.id}&text=Hello%21%20Experience%20a%20bot%20that%20offers%20a%20vast%20library%20of%20unlimited%20movies%20and%20series.%20%F0%9F%98%83'),
        InlineKeyboardButton(
            f'⏳ {referdb.get_refer_points(query.from_user.id)}', callback_data='ref_point'),
        InlineKeyboardButton('Back', callback_data='premium_info')
    ]]
    reply_markup = InlineKeyboardMarkup(btn)
    try:
        await bot.edit_message_media(
            query.message.chat.id,
            query.message.id,
            InputMediaPhoto("https://graph.org/file/1a2e64aee3d4d10edd930.jpg")
        )
    except Exception:
        pass
    await query.message.edit_text(
        text=script.REFER_TXT.format(bot.me.username, query.from_user.id),
        reply_markup=reply_markup,
        parse_mode=enums.ParseMode.HTML
    )
    await query.answer()

@Client.on_callback_query(filters.regex(r"^next"))
async def next_page(bot, query):
    try:
        await query.answer()
    except Exception:
        pass
    ident, req, key, offset = query.data.split("_")
    curr_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
    if int(req) not in [query.from_user.id, 0]:
        return await query.answer(script.ALRT_TXT.format(query.from_user.first_name), show_alert=True)
    try:
        offset = int(offset)
    except Exception:
        offset = 0
    if BUTTONS.get(key) is not None:
        search = BUTTONS.get(key)
    else:
        search = FRESH.get(key)
    if not search:
        await query.answer(script.OLD_ALRT_TXT.format(query.from_user.first_name), show_alert=True)
        return
    files, n_offset, total = await get_search_results(query.message.chat.id, search, offset=offset, filter=True)
    try:
        n_offset = int(n_offset)
    except Exception:
        n_offset = 0

    if not files:
        return
    temp.GETALL[key] = files
    temp.SHORT[query.from_user.id] = query.message.chat.id
    settings = await get_settings(query.message.chat.id)
    if settings.get('button'):
        btn = [
            [
                InlineKeyboardButton(text=f"🔗 {get_size(file.file_size)} ≽ " + clean_filename(
                    file.file_name), callback_data=f'file#{file.file_id}'),
            ]
            for file in files
        ]
        btn.insert(0,
                   [
                       InlineKeyboardButton(
                           'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                       InlineKeyboardButton(
                           "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                       InlineKeyboardButton(
                           "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                   ]
                   )
        btn.insert(0,
                   [
                       InlineKeyboardButton("", callback_data="ident"),
                       InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")
                   ]
                   )

    else:
        btn = []
        btn.insert(0,
                   [
                       InlineKeyboardButton(
                           'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                       InlineKeyboardButton(
                           "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                       InlineKeyboardButton(
                           "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                   ]
                   )
        btn.insert(0, [
            InlineKeyboardButton("", callback_data="ident"),
            InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")
        ])
    if ULTRA_FAST_MODE:
        if 0 < offset <= 10:
            off_set = 0
        elif offset == 0:
            off_set = None
        else:
            off_set = offset - 10
        if n_offset == 0:
            btn.append(
                [InlineKeyboardButton("⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"), InlineKeyboardButton(f"{math.ceil(int(offset)/10)+1}", callback_data="pages")]
            )
        elif off_set is None:
            btn.append([InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(f"{math.ceil(int(offset)/10)+1}", callback_data="pages"), InlineKeyboardButton("ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")])
        else:
            btn.append(
                [
                    InlineKeyboardButton("⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"),
                    InlineKeyboardButton(f"{math.ceil(int(offset)/10)+1}", callback_data="pages"),
                    InlineKeyboardButton("ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")
                ],
            )
    else:
        try:
            if settings['max_btn']:
                if 0 < offset <= 10:
                    off_set = 0
                elif offset == 0:
                    off_set = None
                else:
                    off_set = offset - 10
                if n_offset == 0:
                    btn.append([InlineKeyboardButton("⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"), InlineKeyboardButton(
                        f"{math.ceil(int(offset)/10)+1} / {math.ceil(total/10)}", callback_data="pages")])
                elif off_set is None:
                    btn.append([InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        f"{math.ceil(int(offset)/10)+1} / {math.ceil(total/10)}", callback_data="pages"), InlineKeyboardButton("ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")])
                else:
                    btn.append(
                        [
                            InlineKeyboardButton(
                                "⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"),
                            InlineKeyboardButton(
                                f"{math.ceil(int(offset)/10)+1} / {math.ceil(total/10)}", callback_data="pages"),
                            InlineKeyboardButton(
                                "ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")
                        ],
                    )
            else:
                if 0 < offset <= int(MAX_B_TN):
                    off_set = 0
                elif offset == 0:
                    off_set = None
                else:
                    off_set = offset - int(MAX_B_TN)
                if n_offset == 0:
                    btn.append([InlineKeyboardButton("⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"), InlineKeyboardButton(
                        f"{math.ceil(int(offset)/int(MAX_B_TN))+1} / {math.ceil(total/int(MAX_B_TN))}", callback_data="pages")])
                elif off_set is None:
                    btn.append([InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        f"{math.ceil(int(offset)/int(MAX_B_TN))+1} / {math.ceil(total/int(MAX_B_TN))}", callback_data="pages"), InlineKeyboardButton("ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")])
                else:
                    btn.append(
                        [
                            InlineKeyboardButton(
                                "⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"),
                            InlineKeyboardButton(
                                f"{math.ceil(int(offset)/int(MAX_B_TN))+1} / {math.ceil(total/int(MAX_B_TN))}", callback_data="pages"),
                            InlineKeyboardButton(
                                "ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")
                        ],
                    )
        except KeyError:
            await save_group_settings(query.message.chat.id, 'max_btn', True)
            if 0 < offset <= 10:
                off_set = 0
            elif offset == 0:
                off_set = None
            else:
                off_set = offset - 10
            if n_offset == 0:
                btn.append(
                    [InlineKeyboardButton("⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"), InlineKeyboardButton(
                        f"{math.ceil(int(offset)/10)+1} / {math.ceil(total/10)}", callback_data="pages")]
                )
            elif off_set is None:
                btn.append([InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                    f"{math.ceil(int(offset)/10)+1} / {math.ceil(total/10)}", callback_data="pages"), InlineKeyboardButton("ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")])
            else:
                btn.append(
                    [
                        InlineKeyboardButton(
                            "⋞ ʙᴀᴄᴋ", callback_data=f"next_{req}_{key}_{off_set}"),
                        InlineKeyboardButton(
                            f"{math.ceil(int(offset)/10)+1} / {math.ceil(total/10)}", callback_data="pages"),
                        InlineKeyboardButton(
                            "ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")
                    ],
                )
    if not settings["button"]:
        cur_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
        time_difference = timedelta(hours=cur_time.hour, minutes=cur_time.minute, seconds=(cur_time.second+(cur_time.microsecond/1000000))) - \
            timedelta(hours=curr_time.hour, minutes=curr_time.minute, seconds=(
                curr_time.second+(curr_time.microsecond/1000000)))
        remaining_seconds = "{:.2f}".format(time_difference.total_seconds())
        dreamx_title = clean_search_text(search)
        cap = None
        try:
            if settings['imdb']:
                cap = await get_cap(settings, remaining_seconds, files, query, total, dreamx_title, offset)
                if query.message.caption:
                    try:
                        await query.message.edit_caption(caption=cap, reply_markup=InlineKeyboardMarkup(btn), parse_mode=enums.ParseMode.HTML)
                    except (MessageNotModified, MessageIdInvalid):
                        pass
                    except Exception as e:
                        logger.exception(e)
                else:
                    try:
                        await query.message.edit_text(text=cap, reply_markup=InlineKeyboardMarkup(btn), link_preview_options=LinkPreviewOptions(is_disabled=True), parse_mode=enums.ParseMode.HTML)
                    except (MessageNotModified, MessageIdInvalid):
                        pass
            else:
                cap = await get_cap(settings, remaining_seconds, files, query, total, dreamx_title, offset+1)
                await query.message.edit_text(text=cap, reply_markup=InlineKeyboardMarkup(btn), link_preview_options=LinkPreviewOptions(is_disabled=True), parse_mode=enums.ParseMode.HTML)
        except (MessageNotModified, MessageIdInvalid):
            pass
        except Exception as e:
            logger.exception("Failed to send result: %s", e)
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btn))
        except (MessageNotModified, MessageIdInvalid):
            pass
    await query.answer()


@Client.on_callback_query(filters.regex(r"^spol"))
async def advantage_spoll_choker(bot, query):
    _, id, user = query.data.split('#')
    if int(user) != 0 and query.from_user.id != int(user):
        return await query.answer(script.ALRT_TXT.format(query.from_user.first_name), show_alert=True)
    movies = await get_posterx(id, id=True) if TMDB_ON_SEARCH else await get_poster(id, id=True)
    movie = movies.get('title')
    movie = re.sub(r"[:-]", " ", movie)
    movie = re.sub(r"\s+", " ", movie).strip()
    await query.answer(script.TOP_ALRT_MSG)
    files, offset, total_results = await get_search_results(query.message.chat.id, movie, offset=0, filter=True)
    if files:
        k = (movie, files, offset, total_results)
        await auto_filter(bot, query, k)
    else:
        reqstr1 = query.from_user.id if query.from_user else 0
        reqstr = await bot.get_users(reqstr1)
        if NO_RESULTS_MSG:
            try:
                await bot.send_message(chat_id=BIN_CHANNEL, text=script.NORSLTS.format(reqstr.id, reqstr.mention, movie))
            except Exception as e:
                logger.error("Error In Spol: %s — Make Sure Bot Admin BIN CHANNEL", e)
        btn = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔰Cʟɪᴄᴋ ʜᴇʀᴇ & ʀᴇǫᴜᴇsᴛ ᴛᴏ ᴀᴅᴍɪɴ", url=OWNER_LNK)]])
        k = await query.message.edit(script.MVE_NT_FND, reply_markup=btn)
        await asyncio.sleep(10)
        await k.delete()

# Qualities
@Client.on_callback_query(filters.regex(r"^qualities#"))
async def qualities_cb_handler(client: Client, query: CallbackQuery):
    _, req, key = query.data.split("#")
    try:
        if int(req) not in [query.from_user.id, 0]:
            return await query.answer(
                f"⚠️ ʜᴇʟʟᴏ {query.from_user.first_name},\n"
                f"ᴛʜɪꜱ ɪꜱ ɴᴏᴛ ʏᴏᴜʀ ᴍᴏᴠɪᴇ ʀᴇǫᴜᴇꜱᴛ,\nʀᴇǫᴜᴇꜱᴛ ʏᴏᴜʀ'ꜱ...",
                show_alert=True,
            )
    except Exception:
        pass

    search = FRESH.get(key)
    search = search.replace(' ', '_')

    btn = []
    for i in range(0, len(QUALITIES), 2):
        q1 = QUALITIES[i]
        row = [InlineKeyboardButton(
            text=q1, callback_data=f"fq#{q1.lower()}#{req}#{key}")]
        if i + 1 < len(QUALITIES):
            q2 = QUALITIES[i + 1]
            row.append(InlineKeyboardButton(
                text=q2, callback_data=f"fq#{q2.lower()}#{req}#{key}"))
        btn.append(row)

    btn.insert(0, [
        InlineKeyboardButton(text=" ꜱᴇʟᴇᴄᴛ ǫᴜᴀʟɪᴛʏ ", callback_data="ident")
    ])
    btn.append([
        InlineKeyboardButton(text="↭ ʙᴀᴄᴋ ᴛᴏ ꜰɪʟᴇs ↭",
                             callback_data=f"fq#homepage#{req}#{key}")
    ])

    await query.edit_message_reply_markup(InlineKeyboardMarkup(btn))


@Client.on_callback_query(filters.regex(r"^fq#"))
async def filter_qualities_cb_handler(client: Client, query: CallbackQuery):
    _, qual, req, key = query.data.split("#")
    curr_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
    search = FRESH.get(key, "")
    search = search.replace("_", " ")
    baal = qual in search
    if baal:
        search = search.replace(qual, "")
    else:
        search = search
    chat_id = query.message.chat.id
    message = query.message
    try:
        if int(req) not in [query.from_user.id, 0]:
            return await query.answer(f"⚠️ ʜᴇʟʟᴏ {query.from_user.first_name},\nᴛʜɪꜱ ɪꜱ ɴᴏᴛ ʏᴏᴜʀ ᴍᴏᴠɪᴇ ʀᴇǫᴜᴇꜱᴛ,\nʀᴇǫᴜᴇꜱᴛ ʏᴏᴜʀ'ꜱ...", show_alert=True,)
    except Exception:
        pass
    if qual != "homepage":
        search = f"{search} {qual}"
    BUTTONS[key] = search
    files, offset, total_results = await get_search_results(chat_id, search, offset=0, filter=True)
    if not files:
        await query.answer("🚫 ɴᴏ ꜰɪʟᴇꜱ ᴡᴇʀᴇ ꜰᴏᴜɴᴅ ", show_alert=1)
        return
    temp.GETALL[key] = files
    settings = await get_settings(message.chat.id)
    if settings.get('button'):
        btn = [
            [
                InlineKeyboardButton(text=f"🔗 {get_size(file.file_size)} ≽ " + clean_filename(
                    file.file_name), callback_data=f'file#{file.file_id}'),
            ]
            for file in files
        ]
        btn.insert(0,
                   [
                       InlineKeyboardButton(
                           'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                       InlineKeyboardButton(
                           "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                       InlineKeyboardButton(
                           "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                   ]
                   )
        btn.insert(0,
                   [
                       InlineKeyboardButton("", callback_data="ident"),
                       InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")
                   ])
    else:
        btn = []
        btn.insert(0,
                   [
                       InlineKeyboardButton(
                           'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                       InlineKeyboardButton(
                           "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                       InlineKeyboardButton(
                           "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                   ]
                   )
        btn.insert(0,
                   [
                       InlineKeyboardButton("", callback_data="ident"),
                       InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")

                   ])
    if offset != "":
        try:
            if settings['max_btn']:
                btn.append(

                    [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")]
                )
            else:
                btn.append(

                    [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        text=f"1/{math.ceil(int(total_results)/int(MAX_B_TN))}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")]
                )
        except KeyError:
            await save_group_settings(query.message.chat.id, 'max_btn', True)
            btn.append(

                [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                    text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")]
            )
    else:
        btn.append(

            [InlineKeyboardButton(
                text=" ɴᴏ ᴍᴏʀᴇ ᴘᴀɢᴇꜱ ᴀᴠᴀɪʟᴀʙʟᴇ", callback_data="pages")]
        )
    if not settings["button"]:
        cur_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
        time_difference = timedelta(hours=cur_time.hour, minutes=cur_time.minute, seconds=(cur_time.second+(cur_time.microsecond/1000000))) - \
            timedelta(hours=curr_time.hour, minutes=curr_time.minute, seconds=(
                curr_time.second+(curr_time.microsecond/1000000)))
        remaining_seconds = "{:.2f}".format(time_difference.total_seconds())
        dreamx_title = clean_search_text(search)
        cap = await get_cap(settings, remaining_seconds, files, query, total_results, dreamx_title, offset=1)
        try:
            await query.message.edit_text(text=cap, reply_markup=InlineKeyboardMarkup(btn), link_preview_options=LinkPreviewOptions(is_disabled=True), parse_mode=enums.ParseMode.HTML)
        except (MessageNotModified, MessageIdInvalid):
            pass
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btn))
        except (MessageNotModified, MessageIdInvalid):
            pass
    await query.answer()

# languages


@Client.on_callback_query(filters.regex(r"^languages#"))
async def languages_cb_handler(client: Client, query: CallbackQuery):
    _, req, key = query.data.split("#")
    try:
        if int(req) not in [query.from_user.id, 0]:
            return await query.answer(
                f"⚠️ ʜᴇʟʟᴏ {query.from_user.first_name},\n"
                f"ᴛʜɪꜱ ɪꜱ ɴᴏᴛ ʏᴏᴜʀ ᴍᴏᴠɪᴇ ʀᴇǫᴜᴇꜱᴛ,\nʀᴇǫᴜᴇꜱᴛ ʏᴏᴜʀ'ꜱ...",
                show_alert=True,
            )
    except Exception:
        pass

    search = FRESH.get(key)
    search = search.replace(' ', '_')

    items = list(LANGUAGES.items())
    btn = []

    for i in range(0, len(items), 2):
        name1, code1 = items[i]
        row = [InlineKeyboardButton(
            text=name1, callback_data=f"fl#{code1}#{req}#{key}")]
        if i + 1 < len(items):
            name2, code2 = items[i + 1]
            row.append(InlineKeyboardButton(
                text=name2, callback_data=f"fl#{code2}#{req}#{key}"))
        btn.append(row)

    btn.insert(0, [InlineKeyboardButton(
        text="⇊ ꜱᴇʟᴇᴄᴛ ʟᴀɴɢᴜᴀɢᴇ ⇊", callback_data="ident")])
    btn.append([InlineKeyboardButton(text="↭ ʙᴀᴄᴋ ᴛᴏ ꜰɪʟᴇs ↭",
               callback_data=f"fl#homepage#{req}#{key}")])

    await query.edit_message_reply_markup(InlineKeyboardMarkup(btn))


@Client.on_callback_query(filters.regex(r"^fl#"))
async def filter_languages_cb_handler(client: Client, query: CallbackQuery):
    _, lang, req, key = query.data.split("#")
    curr_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
    search = FRESH.get(key)
    search = search.replace("_", " ")
    baal = lang in search
    if baal:
        search = search.replace(lang, "")
    else:
        search = search
    chat_id = query.message.chat.id
    message = query.message
    try:
        if int(req) not in [query.from_user.id, 0]:
            return await query.answer(f"⚠️ ʜᴇʟʟᴏ {query.from_user.first_name},\nᴛʜɪꜱ ɪꜱ ɴᴏᴛ ʏᴏᴜʀ ᴍᴏᴠɪᴇ ʀᴇǫᴜᴇꜱᴛ,\nʀᴇǫᴜᴇꜱᴛ ʏᴏᴜʀ'ꜱ...", show_alert=True,)
    except Exception:
        pass
    if lang != "homepage":
        search = f"{search} {lang}"
    BUTTONS[key] = search
    files, offset, total_results = await get_search_results(chat_id, search, offset=0, filter=True)
    if not files:
        await query.answer("🚫 ɴᴏ ꜰɪʟᴇꜱ ᴡᴇʀᴇ ꜰᴏᴜɴᴅ ", show_alert=1)
        return
    temp.GETALL[key] = files
    settings = await get_settings(message.chat.id)
    if settings.get('button'):
        btn = [
            [
                InlineKeyboardButton(text=f"🔗 {get_size(file.file_size)} ≽ " + clean_filename(
                    file.file_name), callback_data=f'file#{file.file_id}'),
            ]
            for file in files
        ]
        btn.insert(0,
                   [
                       InlineKeyboardButton(
                           'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                       InlineKeyboardButton(
                           "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                       InlineKeyboardButton(
                           "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                   ]
                   )
        btn.insert(0,
                   [
                       InlineKeyboardButton("", callback_data="ident"),
                       InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")
                   ]
                   )
    else:
        btn = []
        btn.insert(0,
                   [
                       InlineKeyboardButton(
                           'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                       InlineKeyboardButton(
                           "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                       InlineKeyboardButton(
                           "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                   ])
        btn.insert(0,
                   [
                       InlineKeyboardButton("", callback_data="ident"),
                       InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")
                   ])
    if offset != "":
        try:
            if settings['max_btn']:
                btn.append(
                    [
                        InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                            text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")
                    ])
            else:
                btn.append(
                    [
                        InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                            text=f"1/{math.ceil(int(total_results)/int(MAX_B_TN))}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")
                    ])
        except KeyError:
            await save_group_settings(query.message.chat.id, 'max_btn', True)
            btn.append(
                [
                    InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")
                ])
    else:
        btn.append([InlineKeyboardButton(
            text=" ɴᴏ ᴍᴏʀᴇ ᴘᴀɢᴇꜱ ᴀᴠᴀɪʟᴀʙʟᴇ", callback_data="pages")])
    if not settings["button"]:
        cur_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
        time_difference = timedelta(hours=cur_time.hour, minutes=cur_time.minute, seconds=(cur_time.second+(cur_time.microsecond/1000000))) - \
            timedelta(hours=curr_time.hour, minutes=curr_time.minute, seconds=(
                curr_time.second+(curr_time.microsecond/1000000)))
        remaining_seconds = "{:.2f}".format(time_difference.total_seconds())
        dreamx_title = clean_search_text(search)
        cap = await get_cap(settings, remaining_seconds, files, query, total_results, dreamx_title, offset=1)
        try:
            await query.message.edit_text(text=cap, reply_markup=InlineKeyboardMarkup(btn), link_preview_options=LinkPreviewOptions(is_disabled=True), parse_mode=enums.ParseMode.HTML)
        except (MessageNotModified, MessageIdInvalid):
            pass
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btn))
        except (MessageNotModified, MessageIdInvalid):
            pass
    await query.answer()


@Client.on_callback_query(filters.regex(r"^seasons#"))
async def seasons_cb_handler(client: Client, query: CallbackQuery):
    _, req, key = query.data.split("#")
    try:
        if int(req) not in [query.from_user.id, 0]:
            return await query.answer(
                f"⚠️ ʜᴇʟʟᴏ {query.from_user.first_name},\nᴛʜɪꜱ ɪꜱ ɴᴏᴛ ʏᴏᴜʀ ᴍᴏᴠɪᴇ ʀᴇǫᴜᴇꜱᴛ,\nʀᴇǫᴜᴇꜱᴛ ʏᴏᴜʀ'ꜱ…",
                show_alert=True,
            )
    except Exception:
        pass
    offset = 0
    btn: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(SEASONS) - 1, 2):
        btn.append([
            InlineKeyboardButton(
                f"Sᴇᴀꜱᴏɴ {SEASONS[i][1:]}", callback_data=f"fs#{SEASONS[i].lower()}#{req}#{key}"),
            InlineKeyboardButton(
                f"Sᴇᴀꜱᴏɴ {SEASONS[i+1][1:]}", callback_data=f"fs#{SEASONS[i+1].lower()}#{req}#{key}")
        ])

    btn.insert(
        0,
        [InlineKeyboardButton("⇊ ꜱᴇʟᴇᴄᴛ ꜱᴇᴀꜱᴏɴ ⇊", callback_data="ident")],
    )
    btn.append([InlineKeyboardButton(text="↭ ʙᴀᴄᴋ ᴛᴏ ꜰɪʟᴇs ​↭",
               callback_data=f"next_{req}_{key}_{offset}")])
    await query.edit_message_reply_markup(InlineKeyboardMarkup(btn))
    await query.answer()


@Client.on_callback_query(filters.regex(r"^fs#"))
async def filter_seasons_cb_handler(client: Client, query: CallbackQuery):
    start_t = datetime.now(pytz.timezone("Asia/Kolkata"))
    _, season_tag, req, key = query.data.split("#")
    search = FRESH.get(key).replace("_", " ")
    season_tag = season_tag.lower()
    if season_tag == "homepage":
        search_final = search
        query_input = search_final
    else:
        season_number = int(season_tag[1:])
        query_input = generate_season_variations(search, season_number)
        search_final = query_input[0] if query_input else search

    BUTTONS[key] = search_final
    try:
        if int(req) not in [query.from_user.id, 0]:
            return await query.answer("⚠️ Not your request", show_alert=True)
    except Exception:
        pass

    chat_id = query.message.chat.id
    files, n_offset, total_results = await get_search_results(chat_id, query_input, offset=0, filter=True)
    if not files:
        BUTTONS[key] = None
        return await query.answer("🚫 ɴᴏ ꜰɪʟᴇꜱ ꜰᴏᴜɴᴅ ", show_alert=True)

    temp.GETALL[key] = files
    settings = await get_settings(chat_id)
    btn: list[list[InlineKeyboardButton]] = []
    if settings.get("button"):
        btn.extend(
            [
                [
                    InlineKeyboardButton(
                        f"🔗 {get_size(f.file_size)} ≽ " +
                        clean_filename(f.file_name),
                        callback_data=f"file#{f.file_id}",
                    )
                ]
                for f in files
            ]
        )
    btn.insert(
        0,
        [
            InlineKeyboardButton("Qᴜᴀʟɪᴛʏ", callback_data=f"qualities#{req}#{key}"),
            InlineKeyboardButton("Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
            InlineKeyboardButton("Sᴇᴀꜱᴏɴ", callback_data=f"seasons#{req}#{key}"),
        ],
    )
    btn.insert(
        0,
        [
            InlineKeyboardButton("", callback_data="ident"),
            InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}"),
        ],
    )
    if n_offset != "":
        try:
            if settings['max_btn']:
                btn.append(
                    [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")]
                )

            else:
                btn.append(
                    [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        text=f"1/{math.ceil(int(total_results)/int(MAX_B_TN))}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")]
                )
        except KeyError:
            await save_group_settings(query.message.chat.id, 'max_btn', True)
            btn.append(
                [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                    text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{n_offset}")]
            )
    else:
        n_offset = 0
        btn.append(
            [InlineKeyboardButton(
                " ɴᴏ ᴍᴏʀᴇ ᴘᴀɢᴇꜱ ᴀᴠᴀɪʟᴀʙʟᴇ", callback_data="pages")]
        )
    if not settings.get("button"):
        time_difference = datetime.now(pytz.timezone("Asia/Kolkata")) - start_t
        remaining_seconds = f"{time_difference.total_seconds():.2f}"
        dreamx_title = clean_search_text(search_final)
        cap = await get_cap(settings, remaining_seconds, files, query, total_results, dreamx_title, offset=1)
        try:
            await query.message.edit_text(
                text=cap,
                reply_markup=InlineKeyboardMarkup(btn),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except (MessageNotModified, MessageIdInvalid):
            pass
    else:
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup(btn))
        except (MessageNotModified, MessageIdInvalid):
            pass
    await query.answer()


@Client.on_callback_query(group=10)
async def cb_handler(client: Client, query: CallbackQuery):
    DreamxData = query.data
    try:
        await client.create_chat_invite_link(int(REQST_CHANNEL))
    except Exception:
        pass
    if query.data == "close_data":
        try:
            user = query.message.reply_to_message.from_user.id
        except Exception:
            user = query.from_user.id
        if int(user) != 0 and query.from_user.id != int(user):
            return await query.answer(script.NT_ALRT_TXT, show_alert=True)
        await query.answer("ᴛʜᴀɴᴋs ꜰᴏʀ ᴄʟᴏsᴇ 🙈")
        await query.message.delete()
        try:
            await query.message.reply_to_message.delete()
        except Exception:
            pass

    elif query.data == "pages":
        await query.answer("ᴛʜɪs ɪs ᴘᴀɢᴇs ʙᴜᴛᴛᴏɴ 😅")

    if query.data.startswith("file"):
        ident, file_id = query.data.split("#")
        user = query.message.reply_to_message.from_user.id if query.message.reply_to_message else query.from_user.id
        if int(user) != 0 and query.from_user.id != int(user):
            return await query.answer(script.ALRT_TXT.format(query.from_user.first_name), show_alert=True)
        await query.answer(url=f"https://t.me/{temp.U_NAME}?start=file_{query.message.chat.id}_{file_id}")

    elif query.data.startswith("sendfiles"):
        ident, key = query.data.split("#")
        settings = await get_settings(query.message.chat.id)
        try:
            await query.answer(url=f"https://telegram.me/{temp.U_NAME}?start=allfiles_{query.message.chat.id}_{key}")
            return
        except UserIsBlocked:
            await query.answer('Uɴʙʟᴏᴄᴋ ᴛʜᴇ ʙᴏᴛ ᴍᴀʜɴ !', show_alert=True)
        except PeerIdInvalid:
            await query.answer(url=f"https://telegram.me/{temp.U_NAME}?start=sendfiles3_{key}")
        except Exception as e:
            logger.exception(e)
            await query.answer(url=f"https://telegram.me/{temp.U_NAME}?start=sendfiles4_{key}")

    elif query.data.startswith("autofilter_delete"):
        await Media.collection.drop()
        if MULTIPLE_DB:    
            await Media2.collection.drop()
        await query.answer("Eᴠᴇʀʏᴛʜɪɴɢ's Gᴏɴᴇ")
        await query.message.edit('ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ᴀʟʟ ɪɴᴅᴇxᴇᴅ ꜰɪʟᴇꜱ ✅')

    elif query.data.startswith("checksub"):
        try:
            ident, kk, file_id = query.data.split("#")
            btn = []
            chat = file_id.split("_")[0]
            settings = await get_settings(chat)
            fsub_channels = list(dict.fromkeys((settings.get('fsub', []) if settings else [])+ AUTH_CHANNELS)) 
            btn += await is_subscribed(client, query.from_user.id, fsub_channels)
            btn += await is_req_subscribed(client, query.from_user.id, AUTH_REQ_CHANNELS)
            if btn:
                btn.append([InlineKeyboardButton("♻️ ᴛʀʏ ᴀɢᴀɪɴ ♻️", callback_data=f"checksub#{kk}#{file_id}")])
                try:
                    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btn))
                except (MessageNotModified, MessageIdInvalid):
                    pass
                await query.answer(
                    f"👋 Hello {query.from_user.first_name},\n\n"
                    "🛑 Yᴏᴜ ʜᴀᴠᴇ ɴᴏᴛ ᴊᴏɪɴᴇᴅ ᴀʟʟ ʀᴇǫᴜɪʀᴇᴅ ᴜᴘᴅᴀᴛᴇ Cʜᴀɴɴᴇʟs.\n"
                    "👉 Pʟᴇᴀsᴇ ᴊᴏɪɴ ᴇᴀᴄʜ ᴏɴᴇ ᴀɴᴅ ᴛʀʏ ᴀɢᴀɪɴ.\n",
                    show_alert=True
                )
                return
            await query.answer(url=f"https://t.me/{temp.U_NAME}?start={kk}_{file_id}")
            if query.message.chat.type == enums.ChatType.PRIVATE:
                try:
                    await query.message.delete()
                except MessageDeleteForbidden:
                    pass
        except Exception as e:
            await log_error(client, f"❌ Error in checksub callback:\n\n{repr(e)}")
            logger.error(f"❌ Error in checksub callback:\n\n{repr(e)}")

    elif query.data.startswith("killfilesdq"):
        ident, keyword = query.data.split("#")
        await query.message.edit_text(f"<b>Fetching Files for your query {keyword} on DB... Please wait...</b>")
        files, total = await get_bad_files(keyword)
        await query.message.edit_text("<b>ꜰɪʟᴇ ᴅᴇʟᴇᴛɪᴏɴ ᴘʀᴏᴄᴇꜱꜱ ᴡɪʟʟ ꜱᴛᴀʀᴛ ɪɴ 5 ꜱᴇᴄᴏɴᴅꜱ !</b>")
        await asyncio.sleep(5)
        deleted = 0
        async with lock:
            try:
                for file in files:
                    file_ids = file.file_id
                    file_name = file.file_name
                    result = await Media.collection.delete_one({
                        '_id': file_ids,
                    })
                    if not result.deleted_count and MULTIPLE_DB:
                        result = await Media2.collection.delete_one({
                            '_id': file_ids,
                        })
                    if result.deleted_count:
                        logger.info(
                            f'ꜰɪʟᴇ ꜰᴏᴜɴᴅ ꜰᴏʀ ʏᴏᴜʀ ǫᴜᴇʀʏ {keyword}! ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ {file_name} ꜰʀᴏᴍ ᴅᴀᴛᴀʙᴀꜱᴇ.')
                    deleted += 1
                    if deleted % 20 == 0:
                        await query.message.edit_text(f"<b>ᴘʀᴏᴄᴇꜱꜱ ꜱᴛᴀʀᴛᴇᴅ ꜰᴏʀ ᴅᴇʟᴇᴛɪɴɢ ꜰɪʟᴇꜱ ꜰʀᴏᴍ ᴅʙ. ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ {str(deleted)} ꜰɪʟᴇꜱ ꜰʀᴏᴍ ᴅʙ ꜰᴏʀ ʏᴏᴜʀ ǫᴜᴇʀʏ {keyword} !\n\nᴘʟᴇᴀꜱᴇ ᴡᴀɪᴛ...</b>")
            except Exception as e:
                logger.error("Error In killfiledq: %s", e)
                await query.message.edit_text(f'Error: {e}')
            else:
                await query.message.edit_text(f"<b>ᴘʀᴏᴄᴇꜱꜱ ᴄᴏᴍᴘʟᴇᴛᴇᴅ ꜰᴏʀ ꜰɪʟᴇ ᴅᴇʟᴇᴛᴀᴛɪᴏɴ !\n\nꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ ᴅᴇʟᴇᴛᴇᴅ {str(deleted)} ꜰɪʟᴇꜱ ꜰʀᴏᴍ ᴅʙ ꜰᴏʀ ʏᴏᴜʀ ǫᴜᴇʀʏ {keyword}.</b>")

    elif query.data.startswith("opnsetgrp"):
        ident, grp_id = query.data.split("#")
        userid = query.from_user.id if query.from_user else None
        st = await client.get_chat_member(int(grp_id), userid)
        if (
                st.status != enums.ChatMemberStatus.ADMINISTRATOR
                and st.status != enums.ChatMemberStatus.OWNER
                and str(userid) not in ADMINS
        ):
            await query.answer("ʏᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ʀɪɢʜᴛꜱ ᴛᴏ ᴅᴏ ᴛʜɪꜱ !", show_alert=True)
            return
        title = query.message.chat.title
        settings = await get_settings(grp_id)
        if settings is not None:
            btn = await group_setting_buttons(int(grp_id))
            reply_markup = InlineKeyboardMarkup(btn)
            await query.message.edit_text(
                text=await get_settings_text(grp_id, title),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode=enums.ParseMode.HTML
            )
            await query.message.edit_reply_markup(reply_markup)

    elif query.data.startswith("opnsetpm"):
        ident, grp_id = query.data.split("#")
        userid = query.from_user.id if query.from_user else None
        st = await client.get_chat_member(int(grp_id), userid)
        if (
                st.status != enums.ChatMemberStatus.ADMINISTRATOR
                and st.status != enums.ChatMemberStatus.OWNER
                and str(userid) not in ADMINS
        ):
            await query.answer("Yᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ sᴜғғɪᴄɪᴀɴᴛ ʀɪɢʜᴛs ᴛᴏ ᴅᴏ ᴛʜɪs !", show_alert=True)
            return
        title = query.message.chat.title
        settings = await get_settings(grp_id)
        btn2 = [[
            InlineKeyboardButton(
                "ᴄʜᴇᴄᴋ ᴍʏ ᴅᴍ 🗳️", url=f"telegram.me/{temp.U_NAME}")
        ]]
        reply_markup = InlineKeyboardMarkup(btn2)
        await query.message.edit_text(f"<b>ʏᴏᴜʀ sᴇᴛᴛɪɴɢs ᴍᴇɴᴜ ғᴏʀ {title} ʜᴀs ʙᴇᴇɴ sᴇɴᴛ ᴛᴏ ʏᴏᴜ ʙʏ ᴅᴍ.</b>")
        await query.message.edit_reply_markup(reply_markup)
        if settings is not None:
            btn = await group_setting_buttons(int(grp_id))
            reply_markup = InlineKeyboardMarkup(btn)
            await client.send_message(
                chat_id=userid,
                text=await get_settings_text(grp_id, title),
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode=enums.ParseMode.HTML
            )

    elif query.data.startswith("show_option"):
        ident, from_user = query.data.split("#")
        btn = [[
            InlineKeyboardButton("⚠️ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ ⚠️", callback_data=f"unavailable#{from_user}"),
            InlineKeyboardButton("🟢 ᴜᴘʟᴏᴀᴅᴇᴅ 🟢", callback_data=f"uploaded#{from_user}")
        ], [
            InlineKeyboardButton("♻️ ᴀʟʀᴇᴀᴅʏ ᴀᴠᴀɪʟᴀʙʟᴇ ♻️", callback_data=f"already_available#{from_user}")
        ], [
            InlineKeyboardButton("📌 Not Released 📌", callback_data=f"Not_Released#{from_user}"),
            InlineKeyboardButton("♨️Type Correct Spelling♨️", callback_data=f"Type_Correct_Spelling#{from_user}")
        ], [
            InlineKeyboardButton("⚜️ Not Available In The Hindi ⚜️", callback_data=f"Not_Available_In_The_Hindi#{from_user}")
        ]]
        if query.from_user.id in ADMINS:
            reply_markup = InlineKeyboardMarkup(btn)
            await query.message.edit_reply_markup(reply_markup)
            await query.answer("Hᴇʀᴇ ᴀʀᴇ ᴛʜᴇ ᴏᴘᴛɪᴏɴs !")
        else:
            await query.answer("Yᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ sᴜғғɪᴄɪᴀɴᴛ ʀɪɢʜᴛs ᴛᴏ ᴅᴏ ᴛʜɪs !", show_alert=True)

    elif query.data.split("#")[0] in ["unavailable", "Not_Released", "Type_Correct_Spelling", "Not_Available_In_The_Hindi", "uploaded", "already_available"]:
        key = query.data.split("#")[0]
        ident, from_user = query.data.split("#")
        if query.from_user.id not in ADMINS:
            return await query.answer("Yᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ sᴜғғɪᴄɪᴀɴᴛ ʀɪɢʜᴛs ᴛᴏ ᴅᴏ ᴛʜɪs !", show_alert=True)
            
        status_configs = {
            "unavailable": {
                "btn_text": "⚠️ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ ⚠️", "alert_key": "unalert", "answer": "Sᴇᴛ ᴛᴏ Uɴᴀᴠᴀɪʟᴀʙʟᴇ !",
                "pm": "<b>Hᴇʏ {mention},</b>\n\n<u>{content}</u> Hᴀs Bᴇᴇɴ Mᴀʀᴋᴇᴅ Aᴅ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ...💔\n\n#Uɴᴀᴠᴀɪʟᴀʙʟᴇ ⚠️",
                "sup": "<b>Hᴇʏ {mention},</b>\n\n<u>{content}</u> Hᴀs Bᴇᴇɴ Mᴀʀᴋᴇᴅ Aᴅ ᴜɴᴀᴠᴀɪʟᴀʙʟᴇ...💔\n\n#Uɴᴀᴠᴀɪʟᴀʙʟᴇ ⚠️\n\n<small>Bʟᴏᴄᴋᴇᴅ? Uɴʙʟᴏᴄᴋ ᴛʜᴇ ʙᴏᴛ ᴛᴏ ʀᴇᴄᴇɪᴠᴇ ᴍᴇꜱꜱᴀɢᴇꜱ.</small></b>",
                "search": False
            },
            "Not_Released": {
                "btn_text": "📌 Not Released 📌", "alert_key": "nralert", "answer": "Sᴇᴛ ᴛᴏ Nᴏᴛ Rᴇʟᴇᴀꜱᴇᴅ !",
                "pm": "<b>Hᴇʏ {mention}\n\n<code>{content}</code>, ʏᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ʜᴀꜱ ɴᴏᴛ ʙᴇᴇɴ ʀᴇʟᴇᴀꜱᴇᴅ ʏᴇᴛ\n\n#CᴏᴍɪɴɢSᴏᴏɴ...🕊️✌️</b>",
                "sup": "<u>Hᴇʏ {mention}</u>\n\n<b><code>{content}</code>, ʏᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ʜᴀꜱ ɴᴏᴛ ʙᴇᴇɴ ʀᴇʟᴇᴀꜱᴇᴅ ʏᴇᴛ\n\n#CᴏᴍɪɴɢSᴏᴏɴ...🕊️✌️\n\n<small>Bʟᴏᴄᴋᴇᴅ? Uɴʙʟᴏᴄᴋ ᴛʜᴇ ʙᴏᴛ ᴛᴏ ʀᴇᴄᴇɪᴠᴇ ᴍᴇꜱꜱᴀɢᴇꜱ.</small></b>",
                "search": False
            },
            "Type_Correct_Spelling": {
                "btn_text": "♨️ Type Correct Spelling ♨️", "alert_key": "wsalert", "answer": "Sᴇᴛ ᴛᴏ Cᴏʀʀᴇᴄᴛ Sᴘᴇʟʟɪɴɢ !",
                "pm": "<b>Hᴇʏ {mention}\n\nWᴇ Dᴇᴄʟɪɴᴇᴅ Yᴏᴜʀ Rᴇǫᴜᴇsᴛ <code>{content}</code>, Bᴇᴄᴀᴜsᴇ Yᴏᴜʀ Sᴘᴇʟʟɪɴɢ Wᴀs Wʀᴏɴg 😢\n\n#Wʀᴏɴɢ_Sᴘᴇʟʟɪɴɢ 😑</b>",
                "sup": "<u>Hᴇʏ {mention}</u>\n\n<b><code>{content}</code>, Bᴇᴄᴀᴜsᴇ Yᴏᴜʀ Sᴘᴇʟʟɪɴɢ Wᴀs Wʀᴏɴɢ 😢\n\n#Wʀᴏɴɢ_Sᴘᴇʟʟɪɴɢ 😑\n\n<small>Bʟᴏᴄᴋᴇᴅ? Uɴʙʟᴏᴄᴋ ᴛʜᴇ ʙᴏᴛ ᴛᴏ ʀᴇᴄᴇɪᴠᴇ ᴍᴇꜱꜱᴀɢᴇꜱ.</small></b>",
                "search": False
            },
            "Not_Available_In_The_Hindi": {
                "btn_text": "⚜️ Not Available In The Hindi ⚜️", "alert_key": "hnalert", "answer": "Sᴇᴛ ᴛᴏ Nᴏᴛ Aᴠᴀɪʟᴀʙʟᴇ Iɴ Hɪɴᴅɪ !",
                "pm": "<b>Hᴇʏ {mention}\n\nYᴏᴜʀ Rᴇǫᴜᴇsᴛ <code>{content}</code> ɪs Nᴏᴛ Aᴠᴀɪʟᴀʙʟᴇ ɪɴ Hɪɴᴅɪ ʀɪɢʜᴛ ɴᴏᴡ. Sᴏ ᴏᴜʀ ᴍᴏᴅᴇʀᴀᴛᴏʀs ᴄᴀɴ'ᴛ ᴜᴘʟᴏᴀᴅ ɪᴛ\n\n#Hɪɴᴅɪ_ɴᴏᴛ_ᴀᴠᴀɪʟᴀʙʟᴇ ❌</b>",
                "sup": "<u>Hᴇʏ {mention}</u>\n\n<b><code>{content}</code> ɪs Nᴏᴛ Aᴠᴀɪʟᴀʙʟᴇ ɪɴ Hɪɴᴅɪ ʀɪɢʜᴛ ɴᴏᴡ. Sᴏ ᴏᴜʀ ᴍᴏᴅᴇʀᴀᴛᴏʀs ᴄᴀɴ'ᴛ ᴜᴘʟᴏᴀᴅ ɪᴛ\n\n#Hɪɴᴅɪ_ɴᴏᴛ_ᴀᴠᴀɪʟᴀʙʟᴇ ❌\n\n<small>Bʟᴏᴄᴋᴇᴅ? Uɴʙʟᴏᴄᴋ ᴛʜᴇ ʙᴏᴛ ᴛᴏ ʀᴇᴄᴇɪᴠᴇ ᴍᴇꜱꜱᴀɢᴇꜱ.</small></b>",
                "search": False
            },
            "uploaded": {
                "btn_text": "🟢 ᴜᴘʟᴏᴀᴅᴇᴅ 🟢", "alert_key": "upalert", "answer": "Sᴇᴛ ᴛᴏ Uᴘʟᴏᴀᴅᴇᴅ !",
                "pm": "<b>Hᴇʏ {mention},\n\n<u>{content}</u> Yᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ʜᴀꜱ ʙᴇᴇɴ ᴜᴘʟᴏᴀᴅᴇᴅ ʙʏ ᴏᴜʀ ᴍᴏᴅᴇʀᴀᴛᴏʀs.\nKɪɴᴅʟʏ sᴇᴀʀᴄʜ ɪɴ ᴏᴜʀ Gʀᴏᴜᴘ.</b>\n\n#Uᴘʟᴏᴀᴅᴇᴅ✅",
                "sup": "<u>{content}</u>\n\n<b>Hᴇʏ {mention}, Yᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ʜᴀꜱ ʙᴇᴇɴ ᴜᴘʟᴏᴀᴅᴇᴅ ʙʏ ᴏᴜʀ ᴍᴏᴅᴇʀᴀᴛᴏʀs.Kɪɴᴅʟʏ sᴇᴀʀᴄʜ ɪɴ ᴏᴜʀ Gʀᴏᴜᴘ.</b>\n\n#Uᴘʟᴏᴀᴅᴇᴅ✅\n\n<small>Bʟᴏᴄᴋᴇᴅ? Uɴʙʟᴏᴄᴋ ᴛʜᴇ ʙᴏᴛ ᴛᴏ ʀᴇᴄᴇɪᴠᴇ ᴍᴇꜱꜱᴀɢᴇꜱ.</small>",
                "search": True
            },
            "already_available": {
                "btn_text": "♻️ ᴀʟʀᴇᴀᴅʏ ᴀᴠᴀɪʟᴀʙʟᴇ ♻️", "alert_key": "alalert", "answer": "Sᴇᴛ ᴛᴏ Aʟʀᴇᴀᴅʏ Aᴠᴀɪʟᴀʙʟᴇ !",
                "pm": "<b>Hᴇʏ {mention},\n\n<u>{content}</u> Yᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ɪꜱ ᴀʟʀᴇᴀᴅʏ ᴀᴠᴀɪʟᴀʙʟᴇ ɪɴ ᴏᴜʀ ʙᴏᴛ'ꜱ ᴅᴀᴛᴀʙᴀꜱᴇ.\nKɪɴᴅʟʏ sᴇᴀʀᴄʜ ɪɴ ᴏᴜʀ Gʀᴏᴜᴘ.</b>\n\n#Aᴠᴀɪʟᴀʙʟᴇ 💗",
                "sup": "<b>Hᴇʏ {mention},\n\n<u>{content}</u> Yᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ɪꜱ ᴀʟʀᴇᴀᴅʏ ᴀᴠᴀɪʟᴀʙʟᴇ ɪɴ ᴏᴜʀ ʙᴏᴛ'ꜱ ᴅᴀᴛᴀʙᴀꜱᴇ.\nKɪɴᴅʟʏ sᴇᴀʀᴄʜ ɪɴ ᴏᴜʀ Gʀᴏᴜᴘ.</b>\n\n#Aᴠᴀɪʟᴀʙʟᴇ 💗\n<small>Bʟᴏᴄᴋᴇᴅ? Uɴʙʟᴏᴄᴋ ᴛʜᴇ ʙᴏᴛ ᴛᴏ ʀᴇᴄᴇɪᴠᴇ ᴍᴇꜱꜱᴀɢES.</small></i>",
                "search": True
            }
        }
        cfg = status_configs[key]
        user = await client.get_users(int(from_user))
        btn = [[InlineKeyboardButton(cfg["btn_text"], callback_data=f'{cfg["alert_key"]}#{from_user}')]]
        btn2 = [[
            InlineKeyboardButton('ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ', url=UPDATE_CHNL_LNK),
            InlineKeyboardButton("ᴠɪᴇᴡ ꜱᴛᴀᴛᴜꜱ", url=f"{query.message.link}")
        ]]
        if cfg["search"]:
            btn2.append([InlineKeyboardButton("🔍 ꜱᴇᴀʀᴄʜ ʜᴇʀᴇ", url=GRP_LNK)])
            
        content = query.message.text
        await query.message.edit_text(f"<b><strike>{content}</strike></b>")
        await query.message.edit_reply_markup(InlineKeyboardMarkup(btn))
        await query.answer(cfg["answer"])
        
        req_content = extract_request_content(query.message.text)
        try:
            await client.send_message(
                chat_id=int(from_user),
                text=cfg["pm"].format(mention=user.mention, content=req_content),
                reply_markup=InlineKeyboardMarkup(btn2)
            )
        except UserIsBlocked:
            await client.send_message(
                chat_id=int(SUPPORT_CHAT_ID),
                text=cfg["sup"].format(mention=user.mention, content=req_content),
                reply_markup=InlineKeyboardMarkup(btn2)
            )

    elif query.data.split("#")[0] in ["alalert", "upalert", "unalert", "hnalert", "nralert", "wsalert"]:
        ident, from_user = query.data.split("#")
        alerts = {
            "alalert": "Hᴇʏ {name}, Yᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ɪꜱ Aʟʀᴇᴀᴅʏ Aᴠᴀɪʟᴀʙʟᴇ ✅",
            "upalert": "Hᴇʏ {name}, Yᴏᴜʀ ʀᴇǫᴜESꜱᴛ ɪꜱ Uᴘʟᴏᴀᴅᴇᴅ 🔼",
            "unalert": "Hᴇʏ {name}, Yᴏᴜʀ ʀᴇǫᴜᴇꜱᴛ ɪꜱ Uɴᴀᴠᴀɪʟᴀʙʟᴇ ⚠️",
            "hnalert": "Hᴇʏ {name}, Tʜɪꜱ ɪꜱ Nᴏᴛ Aᴠᴀɪʟᴀʙʟᴇ ɪɴ Hɪɴᴅɪ ❌",
            "nralert": "Hᴇʏ {name}, Tʜᴇ Mᴏᴠɪᴇ/ꜱʜᴏᴡ ɪꜱ Nᴏᴛ Rᴇʟᴇᴀꜱᴇᴅ Yᴇᴛ 🆕",
            "wsalert": "Hᴇʏ {name}, Yᴏᴜʀ Rᴇǫᴜᴇꜱᴛ ᴡᴀꜱ ʀᴇᴊᴇᴄᴛᴇᴅ ᴅᴜᴇ ᴛᴏ ᴡʀᴏɴɢ sᴘᴇʟʟɪɴɢ ❗"
        }
        if int(query.from_user.id) == int(from_user):
            user = await client.get_users(from_user)
            msg_text = alerts[ident].format(name=user.first_name)
            await query.answer(msg_text, show_alert=True)
        else:
            await query.answer("Yᴏᴜ ᴅᴏɴ'ᴛ ʜᴀᴠᴇ ᴘᴇʀᴍɪssɪᴏɴ ᴛᴏ sᴇᴇ ᴛʜɪꜱ ❌", show_alert=True)

    elif DreamxData.startswith("generate_stream_link"):
        _, file_id = DreamxData.split(":")
        try:
            user_id = query.from_user.id
            username = query.from_user.mention
            log_msg = await client.send_cached_media(chat_id=BIN_CHANNEL, file_id=file_id,)
            fileName = quote_plus(get_name(log_msg))
            dreamx_stream = f"{URL}watch/{str(log_msg.id)}/{quote_plus(get_name(log_msg))}?hash={get_hash(log_msg)}"
            dreamx_download = f"{URL}{str(log_msg.id)}/{quote_plus(get_name(log_msg))}?hash={get_hash(log_msg)}"
            await query.answer(MSG_ALRT)
            await asyncio.sleep(1)
            await log_msg.reply_text(
                text=f"•• ʟɪɴᴋ ɢᴇɴᴇʀᴀᴛᴇᴅ ꜰᴏʀ ɪᴅ #{user_id} \n•• ᴜꜱᴇʀɴᴀᴍᴇ : {username} \n\n•• ᖴᎥᒪᗴ Nᗩᗰᗴ : {fileName}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("• ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ •", url=dreamx_download),
                                                    InlineKeyboardButton('• ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ •', url=dreamx_stream)]])
            )
            dreamcinezone = await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("• ꜰᴀꜱᴛ ᴅᴏᴡɴʟᴏᴀᴅ •", url=dreamx_download),
                        InlineKeyboardButton('• ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ •', url=dreamx_stream)
                    ],
                    [
                        InlineKeyboardButton('ᴊᴏɪɴ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ', url=UPDATE_CHNL_LNK)
                    ]
                ])
            )
            await asyncio.sleep(DELETE_TIME)
            await dreamcinezone.delete()
            return
        except Exception as e:
            logger.error("Callback error: %s", e)
            await query.answer(f"⚠️ SOMETHING WENT WRONG STREAM LINK  \n\n{e}", show_alert=True)
            return

    elif query.data == "prestream":
        await query.answer(text=script.PRE_STREAM_ALERT, show_alert=True)
        dreamcinezone = await client.send_photo(
            chat_id=query.message.chat.id,
            photo="https://i.ibb.co/whf8xF7j/photo-2025-07-26-10-42-46-7531339305176793100.jpg",
            caption=script.PRE_STREAM,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Buy Premium ", callback_data="premium_info")]
            ])
        )
        await asyncio.sleep(DELETE_TIME)
        await dreamcinezone.delete()

    elif query.data == "start":
        buttons = [[
                    InlineKeyboardButton('🔰 ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ ', url=f'http://telegram.me/{temp.U_NAME}?startgroup=true')
                ],[
                    InlineKeyboardButton(' ʜᴇʟᴘ', callback_data='help'),
                    InlineKeyboardButton(' ᴀʙᴏᴜᴛ', callback_data='about')
                ],[
                    InlineKeyboardButton('ᴛᴏᴘ sᴇᴀʀᴄʜɪɴɢ', callback_data="topsearch"),
                     InlineKeyboardButton('ᴜᴘɢʀᴀᴅᴇ', callback_data="premium_info"),
                ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        current_time = datetime.now(pytz.timezone(TIMEZONE))
        curr_time = current_time.hour
        if curr_time < 12:
            gtxt = "ɢᴏᴏᴅ ᴍᴏʀɴɪɴɢ 🌞"
        elif curr_time < 17:
            gtxt = "ɢᴏᴏᴅ ᴀғᴛᴇʀɴᴏᴏɴ 🌓"
        elif curr_time < 21:
            gtxt = "ɢᴏᴏᴅ ᴇᴠᴇɴɪɴɢ 🌘"
        else:
            gtxt = "ɢᴏᴏᴅ ɴɪɢʜᴛ 🌑"
        try:
            if len(PICS) == 1:
                PIC = PICS[0]
            else:
                try:
                    PIC = f"{random.choice(PICS_URL)}?r={get_random_mix_id()}"
                except Exception:
                    PIC = random.choice(PICS)
            await client.edit_message_media(
                query.message.chat.id,
                query.message.id,
                InputMediaPhoto(PIC)
            )
        except Exception:
            pass
        await query.message.edit_text(
            text=script.START_TXT.format(query.from_user.mention, gtxt, temp.U_NAME, temp.B_NAME),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
        await query.answer(MSG_ALRT)

    elif query.data == "donation":
        buttons = [[
                InlineKeyboardButton('🌲 Sᴇɴᴅ Dᴏɴᴀᴛᴇ Sᴄʀᴇᴇɴsʜᴏᴛ Hᴇʀᴇ', url=OWNER_LNK)
            ],[
                InlineKeyboardButton('⇍ ʙᴀᴄᴋ ⇏', callback_data='about')
            ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        await query.message.edit_text(text="● ◌ ◌")
        await query.message.edit_text(text="● ● ◌")
        await query.message.edit_text(text="● ● ●")
        reply_markup = InlineKeyboardMarkup(buttons)
        await client.edit_message_media(
            query.message.chat.id,
            query.message.id,
            InputMediaPhoto('https://graph.org/file/99eebf5dbe8a134f548e0.jpg')
        )
        await query.message.edit_text(
            text=script.DREAMXBOTZ_DONATION.format(query.from_user.mention, QR_CODE, OWNER_UPI_ID),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )

    elif query.data == "help":
        buttons = [[
            InlineKeyboardButton('⇋ ʙᴀᴄᴋ ᴛᴏ ʜᴏᴍᴇ ⇋', callback_data='start')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        await query.message.edit_text(
            text=script.HELP_TXT,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )

    elif query.data == "about":
        buttons = [[
            InlineKeyboardButton('Disclaimer', callback_data='disclaimer'),
            InlineKeyboardButton ('Source', callback_data='source'),
        ],[
            InlineKeyboardButton('ᴅᴏɴᴀᴛɪᴏɴ 💰', callback_data='donation'),
        ],[
            InlineKeyboardButton('⇋ ʙᴀᴄᴋ ᴛᴏ ʜᴏᴍᴇ ⇋', callback_data='start')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        await query.message.edit_text(
            text=script.ABOUT_TXT.format(temp.U_NAME, temp.B_NAME, OWNER_LNK),
            reply_markup=reply_markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode=enums.ParseMode.HTML
        )

    elif query.data == "give_trial":
        try:
            user_id = query.from_user.id
            has_free_trial = await db.check_trial_status(user_id)
            if has_free_trial:
                await query.answer(
                    "🚸 ʏᴏᴜ'ᴠᴇ ᴀʟʀᴇᴀᴅʏ ᴄʟᴀɪᴍᴇᴅ ʏᴏᴜʀ ꜰʀᴇᴇ ᴛʀɪᴀʟ ᴏɴᴄᴇ !\n\n📌 ᴄʜᴇᴄᴋᴏᴜᴛ ᴏᴜʀ ᴘʟᴀɴꜱ ʙʏ : /plan",
                    show_alert=True
                )
                return
            else:
                await db.give_free_trial(user_id)
                await query.answer("✅ Trial activated!", show_alert=True)

                msg = await client.send_photo(
                    chat_id=query.message.chat.id,
                    photo="https://i.ibb.co/0jC8MSDZ/photo-2025-07-26-10-42-36-7531339283701956616.jpg",
                    caption=(
                        "<b>🥳 ᴄᴏɴɢʀᴀᴛᴜʟᴀᴛɪᴏɴꜱ\n\n"
                        "🎉 ʏᴏᴜ ᴄᴀɴ ᴜsᴇ ꜰʀᴇᴇ ᴛʀɪᴀʟ ꜰᴏʀ <u>5 ᴍɪɴᴜᴛᴇs</u> ꜰʀᴏᴍ ɴᴏᴡ !\n\n"
                        "ɴᴇᴇᴅ ᴘʀᴇᴍɪᴜᴍ 👉🏻 /plan</b>"
                    ),
                    parse_mode=enums.ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🚀 Buy Premium 🚀", callback_data="premium_info")
                    ]])
                )
                await asyncio.sleep(DELETE_TIME)
                return await msg.delete()
        except Exception:
            logger.exception("Error in give_trial callback")

    elif query.data == "source":
        buttons = [[
            InlineKeyboardButton('ᴅʀᴇᴀᴍxʙᴏᴛᴢ 📜', url='https://github.com/DreamXBotz/Auto_Filter_Bot.git'),
            InlineKeyboardButton('⇋ ʙᴀᴄᴋ ⇋', callback_data='about')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        await query.message.edit_text(
            text=script.SOURCE_TXT,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )

    elif query.data == "ref_point":
        await query.answer(f'You Have: {referdb.get_refer_points(query.from_user.id)} Refferal points.', show_alert=True)

    elif query.data == "disclaimer":
            btn = [[
                    InlineKeyboardButton("⇋ ʙᴀᴄᴋ ⇋", callback_data="about")
                  ]]
            reply_markup = InlineKeyboardMarkup(btn)
            await query.message.edit_text(
                text=(script.DISCLAIMER_TXT),
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML
            )

    elif query.data == "premium_info":
        try:
            btn = [[
                InlineKeyboardButton('• ʙᴜʏ ᴘʀᴇᴍɪᴜᴍ •', callback_data='buy_info'),
            ],[
                InlineKeyboardButton('• ʀᴇꜰᴇʀ ꜰʀɪᴇɴᴅꜱ', callback_data='reffff'),
                InlineKeyboardButton('ꜰʀᴇᴇ ᴛʀɪᴀʟ •', callback_data='give_trial')
            ],[
                InlineKeyboardButton('⇋ ʙᴀᴄᴋ ᴛᴏ ʜᴏᴍᴇ ⇋', callback_data='start')
            ]]
            reply_markup = InlineKeyboardMarkup(btn)
            await client.edit_message_media(
                chat_id=query.message.chat.id,
                message_id=query.message.id,
                media=InputMediaPhoto(media=SUBSCRIPTION, caption=script.BPREMIUM_TXT, parse_mode=enums.ParseMode.HTML),
                reply_markup=reply_markup
            )
        except Exception:
            logger.exception("Exception in 'premium_info' callback")

    elif query.data == "buy_info":
        try:
            btn = [[
                InlineKeyboardButton('💳 ᴀᴜᴛᴏ ᴜᴘɪ', callback_data='pay_plan_menu'),
                InlineKeyboardButton('ꜱᴛᴀʀ', callback_data='star_info'),
            ],[
                InlineKeyboardButton('ᴍᴀɴᴜᴀʟ ᴜᴘɪ', callback_data='upi_info')
            ],[
                InlineKeyboardButton('⇋ ʙᴀᴄᴋ ᴛᴏ ᴘʀᴇᴍɪᴜᴍ ⇋', callback_data='premium_info')
            ]]
            reply_markup = InlineKeyboardMarkup(btn)
            await client.edit_message_media(
                chat_id=query.message.chat.id,
                message_id=query.message.id,
                media=InputMediaPhoto(media=SUBSCRIPTION, caption=script.PREMIUM_TEXT, parse_mode=enums.ParseMode.HTML),
                reply_markup=reply_markup
            )
        except Exception:
            logger.exception("Exception in 'buy_info' callback")

    elif query.data == "upi_info":
        try:
            btn = [[
                InlineKeyboardButton('• ꜱᴇɴᴅ  ᴘᴀʏᴍᴇɴᴛ ꜱᴄʀᴇᴇɴꜱʜᴏᴛ •', url=OWNER_LNK),
            ],[
                InlineKeyboardButton('⇋ ʙᴀᴄᴋ ⇋', callback_data='buy_info')
            ]]
            reply_markup = InlineKeyboardMarkup(btn)
            await client.edit_message_media(
                chat_id=query.message.chat.id,
                message_id=query.message.id,
                media=InputMediaPhoto(media=SUBSCRIPTION, caption=script.PREMIUM_UPI_TEXT.format(OWNER_UPI_ID), parse_mode=enums.ParseMode.HTML),
                reply_markup=reply_markup
            )
        except Exception:
            logger.exception("Exception in 'upi_info' callback")

    elif query.data == "star_info":
        try:
            btn = [
                InlineKeyboardButton(f"{stars}⭐", callback_data=f"buy_{stars}")
                for stars, days in STAR_PREMIUM_PLANS.items()
            ]
            buttons = [btn[i:i + 2] for i in range(0, len(btn), 2)]
            buttons.append([InlineKeyboardButton("⋞ ʙᴀᴄᴋ", callback_data="buy_info")])
            reply_markup = InlineKeyboardMarkup(buttons)
            await client.edit_message_media(
                chat_id=query.message.chat.id,
                message_id=query.message.id,
                media=InputMediaPhoto(media=SUBSCRIPTION, caption=script.PREMIUM_STAR_TEXT, parse_mode=enums.ParseMode.HTML),
                reply_markup=reply_markup
            )
        except Exception:
            logger.exception("Exception in 'star' callback")

    elif query.data.startswith("grp_pm"):
        _, grp_id = query.data.split("#")
        user_id = query.from_user.id if query.from_user else None
        if not await is_check_admin(client, int(grp_id), user_id):
            return await query.answer(script.NT_ADMIN_ALRT_TXT, show_alert=True)

        btn = await group_setting_buttons(int(grp_id))
        dreamx = await client.get_chat(int(grp_id))
        await query.message.edit(text=await get_settings_text(grp_id, dreamx.title), reply_markup=InlineKeyboardMarkup(btn), parse_mode=enums.ParseMode.HTML)

    elif query.data.startswith("setgs"):
        ident, set_type, status, grp_id = query.data.split("#")
        userid = query.from_user.id if query.from_user else None
        if not await is_check_admin(client, int(grp_id), userid):
            await query.answer(script.NT_ADMIN_ALRT_TXT, show_alert=True)
            return
        if status == "True":
            await save_group_settings(int(grp_id), set_type, False)
            await query.answer("ᴏꜰꜰ ✗")
        else:
            await save_group_settings(int(grp_id), set_type, True)
            await query.answer("ᴏɴ ✓")
        settings = await get_settings(int(grp_id))
        if settings is not None:
            btn = await group_setting_buttons(int(grp_id))
            reply_markup = InlineKeyboardMarkup(btn)
            await query.message.edit_reply_markup(reply_markup)
    await query.answer(MSG_ALRT)


async def auto_filter(client, msg, spoll=False):
    """
    Core auto_filter logic with timing/debug logging removed.
    """
    curr_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()

    async def _schedule_delete(sent_obj, orig_msg, delay):
        try:
            await asyncio.sleep(delay)
            try:
                await sent_obj.delete()
            except Exception:
                pass
            try:
                await orig_msg.delete()
            except Exception:
                pass
        except Exception:
            pass
    m = None
    try:
        if not spoll:
            message = msg
            if message.text.startswith("/"):
                return
            if re.findall(r"((^\/|^,|^!|^\.|^[\U0001F600-\U000E007F]).*)", message.text):
                return
            if len(message.text) < 100:
                message_text = message.text or ""
                search = message_text.lower()
                m = await message.reply_text(script.SEARCHING_TXT.format(search))
                find = search.split(" ")
                search = ""
                removes = ["in", "upload", "series", "full",
                           "horror", "thriller", "mystery", "print", "file"]
                for x in find:
                    if x in removes:
                        continue
                    else:
                        search = search + x + " "
                search = re.sub(r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|latest|bro|bruh|broh|helo|that|find|dubbed|link|venum|iruka|pannunga|pannungga|anuppunga|anupunga|anuppungga|anupungga|film|undo|kitti|kitty|tharu|kittumo|kittum|movie|any(one)|with\ssubtitle(s)?)\b", "", search, flags=re.IGNORECASE)
                search = search.replace("-", " ")
                search = re.sub(r"[:']", "", search)
                search = re.sub(r"\s+", " ", search).strip()
                files, offset, total_results = await get_search_results(message.chat.id, search, offset=0, filter=True)
                settings = await get_settings(message.chat.id)
                if not files:
                    if settings.get("spell_check"):
                        ai_sts = await m.edit('🤖 ᴘʟᴇᴀꜱᴇ ᴡᴀɪᴛ, ᴀɪ ɪꜱ ᴄʜᴇᴄᴋɪɴɢ ʏᴏᴜʀ ꜱᴘᴇʟʟɪɴɢ...')
                        is_misspelled = await ai_spell_check(chat_id=message.chat.id, wrong_name=search)
                        if is_misspelled:
                            await ai_sts.edit(f'✅ Aɪ Sᴜɢɢᴇsᴛᴇᴅ: <code>{is_misspelled}</code>\n🔍 Searching for it...')
                            message.text = is_misspelled
                            await ai_sts.delete()
                            return await auto_filter(client, message)
                        await ai_sts.delete()
                        result = await advantage_spell_chok(client, message)
                        return result
                    else:
                        try:
                            if m:
                                await m.delete()
                        except Exception:
                            pass
                        result = await advantage_spell_chok(client, message)
                        return result
            else:
                return
        else:
            message = msg.message.reply_to_message
            search, files, offset, total_results = spoll
            m = await message.reply_text(f'🔎 sᴇᴀʀᴄʜɪɴɢ {search}')
            settings = await get_settings(message.chat.id)
            await msg.message.delete()
        key = f"{message.chat.id}-{message.id}"
        FRESH[key] = search
        temp.GETALL[key] = files
        req = message.from_user.id if message.from_user else 0
        temp.SHORT[message.from_user.id] = message.chat.id
        try:
            await client.send_reaction(chat_id=message.chat.id, message_id=message.id, emoji="🍿")
        except Exception:
            pass
        if settings.get('button'):
            btn = [
                [
                    InlineKeyboardButton(text=f"🔗 {get_size(file.file_size)} ≽ " + clean_filename(
                        file.file_name), callback_data=f'file#{file.file_id}'),
                ]
                for file in files
            ]
            btn.insert(0,
                       [
                           InlineKeyboardButton(
                               'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                           InlineKeyboardButton(
                               "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                           InlineKeyboardButton(
                               "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                       ]
                       )
            btn.insert(0,
                       [
                           InlineKeyboardButton("", callback_data="ident"),
                           InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")

                       ])
        else:
            btn = []
            btn.insert(0,
                       [
                           InlineKeyboardButton(
                               'Qᴜᴀʟɪᴛʏ', callback_data=f"qualities#{req}#{key}"),
                           InlineKeyboardButton(
                               "Lᴀɴɢᴜᴀɢᴇ", callback_data=f"languages#{req}#{key}"),
                           InlineKeyboardButton(
                               "Sᴇᴀsᴏɴ",  callback_data=f"seasons#{req}#{key}")
                       ]
                       )
            btn.insert(0,
                       [
                           InlineKeyboardButton("", callback_data="ident"),
                           InlineKeyboardButton("Sᴇɴᴅ Aʟʟ", callback_data=f"sendfiles#{key}")
                       ])

        if offset != "":
            req = message.from_user.id if message.from_user else 0
            if ULTRA_FAST_MODE:
                btn.append(
                    [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                        text="1", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")]
                )
            else:
                try:
                    if settings['max_btn']:
                        btn.append(
                            [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                                text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")]
                        )
                    else:
                        btn.append(
                            [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                                text=f"1/{math.ceil(int(total_results)/int(MAX_B_TN))}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")]
                        )
                except KeyError:
                    await save_group_settings(message.chat.id, 'max_btn', True)
                    btn.append(
                        [InlineKeyboardButton("ᴘᴀɢᴇ", callback_data="pages"), InlineKeyboardButton(
                            text=f"1/{math.ceil(int(total_results)/10)}", callback_data="pages"), InlineKeyboardButton(text="ɴᴇxᴛ ⋟", callback_data=f"next_{req}_{key}_{offset}")]
                    )
        else:
            btn.append([InlineKeyboardButton(
                text="ɴᴏ ᴍᴏʀᴇ ᴘᴀɢᴇꜱ ᴀᴠᴀɪʟᴀʙʟᴇ", callback_data="pages")])

        if settings.get('imdb'):
            imdb = await get_posterx(search, file=(files[0]).file_name) if TMDB_POSTER else await get_poster(search, file=(files[0]).file_name)
        else:
            imdb = None
        cur_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
        time_difference = timedelta(hours=cur_time.hour, minutes=cur_time.minute, seconds=(cur_time.second+(cur_time.microsecond/1000000))) - \
            timedelta(hours=curr_time.hour, minutes=curr_time.minute,
                      seconds=(curr_time.second+(curr_time.microsecond/1000000)))
        remaining_seconds = "{:.2f}".format(time_difference.total_seconds())
        TEMPLATE = script.IMDB_TEMPLATE_TXT
        settings = await get_settings(message.chat.id)
        if settings.get('template'):
            TEMPLATE = settings['template']
        if imdb:
            cap = TEMPLATE.format(
                query=search,
                title=imdb['title'],
                votes=imdb['votes'],
                aka=imdb["aka"],
                seasons=imdb["seasons"],
                box_office=imdb['box_office'],
                localized_title=imdb['localized_title'],
                kind=imdb['kind'],
                imdb_id=imdb["imdb_id"],
                cast=imdb['cast'],
                runtime=imdb['runtime'],
                countries=imdb['countries'],
                certificates=imdb['certificates'],
                languages=imdb['languages'],
                director=imdb['director'],
                writer=imdb['writer'],
                producer=imdb['producer'],
                composer=imdb['composer'],
                cinematographer=imdb['cinematographer'],
                music_team=imdb['music_team'],
                distributors=imdb['distributors'],
                release_date=imdb['release_date'],
                year=imdb['year'],
                genres=imdb['genres'],
                poster=imdb['poster'],
                plot=imdb['plot'] if settings.get('button') else "N/A",
                rating=imdb['rating'],
                url=imdb['url'],
                **locals()
            )
            temp.IMDB_CAP[message.from_user.id] = cap
            if not settings.get('button'):
                cap += "\n\n<b><u>Your Requested Files Are Here</u></b>\n\n"
                for idx, file in enumerate(files, start=1):
                    cap += f"<b>\n{idx}. <a href='https://telegram.me/{temp.U_NAME}?start=file_{message.chat.id}_{file.file_id}'>[{get_size(file.file_size)}] {clean_filename(file.file_name)}\n</a></b>"
        else:
            temp.IMDB_CAP[message.from_user.id] = None
            if ULTRA_FAST_MODE:
                if settings.get('button'):
                    cap = f"<b>🏷 ᴛɪᴛʟᴇ : <code>{search}</code>\n⏰ ʀᴇsᴜʟᴛ ɪɴ : <code>{remaining_seconds} Sᴇᴄᴏɴᴅs</code>\n\n📝 ʀᴇǫᴜᴇsᴛᴇᴅ ʙʏ : {message.from_user.mention}\n⚜️ ᴘᴏᴡᴇʀᴇᴅ ʙʏ : ⚡ {message.chat.title or temp.B_LINK or 'ᴅʀᴇᴀᴍxʙᴏᴛᴢ'} \n\n<u>Your Requested Files Are Here</u> \n\n</b>"
                else:
                    cap = f"<b>🏷 ᴛɪᴛʟᴇ : <code>{search}</code>\n⏰ ʀᴇsᴜʟᴛ ɪɴ : <code>{remaining_seconds} Sᴇᴄᴏɴᴅs</code>\n\n📝 ʀᴇǫᴜᴇsᴛᴇᴅ ʙʏ : {message.from_user.mention}\n⚜️ ᴘᴏᴡᴇʀᴇᴅ ʙʏ : ⚡ {message.chat.title or temp.B_LINK or 'ᴅʀᴇᴀᴍxʙᴏᴛᴢ'} \n\n<u>Your Requested Files Are Here</u> \n\n</b>"
                    for idx, file in enumerate(files, start=1):
                        cap += f"<b>\n{idx}. <a href='https://telegram.me/{temp.U_NAME}?start=file_{message.chat.id}_{file.file_id}'>[{get_size(file.file_size)}] {clean_filename(file.file_name)}\n</a></b>"
            else:
                if settings.get('button'):
                    cap = f"<b>🏷 ᴛɪᴛʟᴇ : <code>{search}</code>\n🧱 ᴛᴏᴛᴀʟ ꜰɪʟᴇꜱ : <code>{total_results}</code>\n⏰ ʀᴇsᴜʟᴛ ɪɴ : <code>{remaining_seconds} Sᴇᴄᴏɴᴅs</code>\n\n📝 ʀᴇǫᴜᴇsᴛᴇᴅ ʙʏ : {message.from_user.mention}\n⚜️ ᴘᴏᴡᴇʀᴇᴅ ʙʏ : ⚡ {message.chat.title or temp.B_LINK or 'ᴅʀᴇᴀᴍxʙᴏᴛᴢ'} \n\n<u>Your Requested Files Are Here</u> \n\n</b>"
                else:
                    cap = f"<b>🏷 ᴛɪᴛʟᴇ : <code>{search}</code>\n🧱 ᴛᴏᴛᴀʟ ꜰɪʟᴇꜱ : <code>{total_results}</code>\n⏰ ʀᴇsᴜʟᴛ ɪɴ : <code>{remaining_seconds} Sᴇᴄᴏɴᴅs</code>\n\n📝 ʀᴇǫᴜᴇsᴛᴇᴅ ʙʏ : {message.from_user.mention}\n⚜️ ᴘᴏᴡᴇʀᴇᴅ ʙʏ : ⚡ {message.chat.title or temp.B_LINK or 'ᴅʀᴇᴀᴍxʙᴏᴛᴢ'} \n\n<u>Your Requested Files Are Here</u> \n\n</b>"

                    for idx, file in enumerate(files, start=1):
                        cap += f"<b>\n{idx}. <a href='https://telegram.me/{temp.U_NAME}?start=file_{message.chat.id}_{file.file_id}'>[{get_size(file.file_size)}] {clean_filename(file.file_name)}\n</a></b>"
        sent = None
        try:
            if imdb and imdb.get('poster'):
                try:
                    if TMDB_POSTER:
                        photo = imdb.get('backdrop') if imdb.get('backdrop') and LANDSCAPE_POSTER else imdb.get('poster')
                    else:
                        photo = imdb.get('poster')
                    sent = await message.reply_photo(photo=photo, caption=cap, reply_markup=InlineKeyboardMarkup(btn), parse_mode=enums.ParseMode.HTML)
                    if m:
                        await m.delete()
                except (MediaEmpty, PhotoInvalidDimensions, WebpageMediaEmpty):
                    pic = imdb.get('poster')
                    poster = pic.replace('.jpg', "._V1_UX360.jpg")
                    sent = await message.reply_photo(photo=poster, caption=cap, reply_markup=InlineKeyboardMarkup(btn), parse_mode=enums.ParseMode.HTML)
                    if m:
                        await m.delete()
                except Exception as e:
                    logger.exception(e)
                    sent = await message.reply_text(text=cap, reply_markup=InlineKeyboardMarkup(btn), link_preview_options=LinkPreviewOptions(is_disabled=True), parse_mode=enums.ParseMode.HTML)
            else:
                sent = await message.reply_text(text=cap, reply_markup=InlineKeyboardMarkup(btn), link_preview_options=LinkPreviewOptions(is_disabled=True), parse_mode=enums.ParseMode.HTML)
                if m:
                    await m.delete()
        except Exception as e:
            logger.exception("Failed to send result: %s", e)
            return
        try:
            if settings.get('auto_delete'):
                asyncio.create_task(_schedule_delete(sent, message, DELETE_TIME))
        except KeyError:
            try:
                await save_group_settings(message.chat.id, 'auto_delete', True)
            except Exception:
                pass
            asyncio.create_task(_schedule_delete(sent, message, DELETE_TIME))
        return
    except Exception as e:
        logger.exception(e)
        return

async def ai_spell_check(chat_id, wrong_name):
    async def search_movie(wrong_name):
        search_results = imdb.search_movie(wrong_name)
        if not search_results or not hasattr(search_results, "titles"):
            return []
        movie_list = [movie.title for movie in search_results.titles]
        return movie_list
    movie_list = await search_movie(wrong_name)
    if not movie_list:
        return
    for _ in range(5):
        closest_match = process.extractOne(wrong_name, movie_list)
        if not closest_match or closest_match[1] <= 80:
            return
        movie = closest_match[0]
        files, _, _ = await get_search_results(chat_id=chat_id, query=movie)
        if files:
            return movie
        movie_list.remove(movie)

async def advantage_spell_chok(client, message):
    search = message.text
    query = re.sub(
        r"\b(pl(i|e)*?(s|z+|ease|se|ese|(e+)s(e)?)|((send|snd|giv(e)?|gib)(\sme)?)|movie(s)?|new|latest|br((o|u)h?)*|^h(e|a)?(l)*(o)*|mal(ayalam)?|t(h)?amil|file|that|find|und(o)*|kit(t(i|y)?)?o(w)?|thar(u)?(o)*w?|kittum(o)*|aya(k)*(um(o)*)?|full\smovie|any(one)|with\ssubtitle(s)?)",
        "", message.text, flags=re.IGNORECASE)
    query = query.strip() + " movie"
    try:
        movies = await get_poster(search, bulk=True)
    except Exception as e:
        logger.exception("get_poster failed for query=%s: %s", query, e)
        try:
            k = await message.reply(script.I_CUDNT.format(message.from_user.mention))
            await asyncio.sleep(60)
            try:
                await k.delete()
            except Exception:
                pass
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return
    if not movies:
        google = quote_plus(search)
        button = [[InlineKeyboardButton(
            "🔍 ᴄʜᴇᴄᴋ sᴘᴇʟʟɪɴɢ ᴏɴ ɢᴏᴏɢʟᴇ ", url=f"https://www.google.com/search?q={google}")]]
        k = await message.reply_text(text=script.I_CUDNT.format(search), reply_markup=InlineKeyboardMarkup(button))
        await asyncio.sleep(60)
        try:
            await k.delete()
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return
    user = message.from_user.id if message.from_user else 0
    buttons = [
        [InlineKeyboardButton(text=movie.title, callback_data=f"spol#{movie.imdb_id}#{user}")
         ] for movie in movies]

    buttons.append([InlineKeyboardButton(
        text="🚫 ᴄʟᴏsᴇ 🚫", callback_data='close_data')])
    d = await message.reply_text(text=script.CUDNT_FND.format(message.from_user.mention), reply_markup=InlineKeyboardMarkup(buttons))
    await asyncio.sleep(60)
    try:
        await d.delete()
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass
