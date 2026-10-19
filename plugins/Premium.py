"""Professional online-premium payment integration for Pyrogram.

Add this module/section once only. Remove/disable older handlers for the same
callbacks (buy_info, api_buy_start, paycheck_*, etc.) to avoid duplicate menus.
Requires Motor/PyMongo async collection support and aiohttp.
"""

import asyncio
import datetime as dt
import logging
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import aiohttp
from pymongo import ReturnDocument
from pyrogram import Client, filters, StopPropagation
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified, MessageDeleteForbidden, MessageIdInvalid

from info import ADMINS
from database.users_chats_db import db
from utils import get_seconds

logger = logging.getLogger(__name__)

# ------------------------------ Configuration ------------------------------
PAYMENT_CREATE_API = "https://checkout.ascinemaa.bond/api.php"
PAYMENT_API_KEY = "heyupi"
DEFAULT_MONTHLY_PRICE = 20
MIN_MONTHS = 1
MAX_MONTHS = 24
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=8, sock_read=12)

# In-memory input state; order data itself is stored persistently in MongoDB.
PREMIUM_MONTH_INPUT = {}  # user_id -> prompt message id
PREMIUM_MONTH_LOCKS = {}  # user_id -> asyncio.Lock

premium_orders = db.db["premium_orders"]
premium_settings = db.db["premium_settings"]


def _now_utc():
    return dt.datetime.now(dt.timezone.utc)


def _naive_utc(value=None):
    """Store UTC datetimes consistently as naive UTC for compatibility."""
    value = value or _now_utc()
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value


def _keyboard(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text, callback_data=data, **kwargs)
         for text, data, kwargs in row]
        for row in rows
    ])


async def _edit_or_reply(message, text, reply_markup=None):
    """Prefer editing the current panel; reply only if it cannot be edited."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception as exc:
        logger.debug("Could not edit payment panel, trying reply: %s", exc)
        try:
            await message.reply_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:
            logger.exception("Could neither edit nor reply with payment panel")


async def _delete_message(message):
    try:
        await message.delete()
    except (MessageDeleteForbidden, MessageIdInvalid):
        pass
    except Exception:
        logger.debug("Unable to delete message", exc_info=True)


async def _safe_answer(query, text=None, show_alert=False):
    try:
        await query.answer(text or "", show_alert=show_alert)
    except Exception:
        logger.debug("Callback answer failed", exc_info=True)


# ------------------------------ Price helpers ------------------------------
async def get_monthly_price():
    try:
        row = await premium_settings.find_one({"_id": "pricing"})
        price = int(row.get("monthly_price")) if row else DEFAULT_MONTHLY_PRICE
        return price if price > 0 else DEFAULT_MONTHLY_PRICE
    except Exception:
        logger.exception("Unable to read premium price; using default")
        return DEFAULT_MONTHLY_PRICE


async def set_monthly_price(price: int):
    await premium_settings.update_one(
        {"_id": "pricing"},
        {"$set": {"monthly_price": int(price), "updated_at": _naive_utc()}},
        upsert=True,
    )


# ------------------------------ API helpers --------------------------------
def _validate_api_url(value, field_name):
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"Payment API returned an invalid {field_name}")
    return value


async def _read_json(response, context):
    try:
        payload = await response.json(content_type=None)
    except Exception as exc:
        raise RuntimeError(f"{context}: API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{context}: API response must be an object")
    return payload


async def create_payment_order(user_id: int, months: int):
    if not MIN_MONTHS <= int(months) <= MAX_MONTHS:
        raise ValueError(f"Months must be between {MIN_MONTHS} and {MAX_MONTHS}")

    monthly_price = await get_monthly_price()
    total_amount = monthly_price * int(months)

    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.get(
            PAYMENT_CREATE_API,
            params={"api_key": PAYMENT_API_KEY, "amount": total_amount},
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Payment creation API HTTP {response.status}")
            result = await _read_json(response, "Payment creation")

    # API contract supplied by the user: {"error": false, "data": {...}}
    if result.get("error") is not False:
        raise RuntimeError(str(result.get("message") or "Order creation failed"))

    data = result.get("data") or {}
    order_id = str(data.get("order_id") or "").strip()
    payment_url = _validate_api_url(data.get("payment_url"), "payment URL")
    status_url = _validate_api_url(data.get("status_check_url"), "status URL")
    if not order_id or len(order_id) > 180:
        raise RuntimeError("Payment API returned an invalid order ID")

    try:
        api_amount = Decimal(str(data.get("amount")))
        expected_amount = Decimal(str(total_amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("Payment API returned an invalid amount") from exc
    if api_amount != expected_amount:
        raise RuntimeError("Payment API amount did not match the requested amount")

    document = {
        "_id": order_id,
        "user_id": int(user_id),
        "months": int(months),
        "monthly_price": int(monthly_price),
        "amount": str(expected_amount),
        "payment_url": payment_url,
        "status_check_url": status_url,
        "payment_status": "pending",
        "activation_state": "pending",
        "activated": False,
        "created_at": _naive_utc(),
    }
    # Never overwrite a previous order with the same ID.
    await premium_orders.insert_one(document)
    return {"order_id": order_id, "amount": total_amount, "payment_url": payment_url}


async def get_payment_status(order):
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.get(order["status_check_url"]) as response:
            if response.status != 200:
                raise RuntimeError(f"Payment status API HTTP {response.status}")
            result = await _read_json(response, "Payment status")

    if result.get("error") is True:
        raise RuntimeError(str(result.get("message") or "Status check failed"))
    if str(result.get("order_id", "")) != str(order["_id"]):
        raise RuntimeError("Payment order ID verification failed")

    try:
        api_amount = Decimal(str(result.get("amount")))
        expected_amount = Decimal(str(order["amount"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("Payment amount verification failed") from exc
    if api_amount != expected_amount:
        raise RuntimeError("Payment amount mismatch")

    status = str(result.get("payment_status", "")).strip().lower()
    if status not in {"pending", "success", "failed", "cancelled"}:
        raise RuntimeError("Payment API returned an unknown status")
    return status, result


# ------------------------------ Activation ---------------------------------
async def activate_order_once(order, transaction_id=None):
    """Atomically claim the order, then extend the user's existing expiry."""
    order_id = str(order["_id"])
    user_id = int(order["user_id"])
    claimed = await premium_orders.find_one_and_update(
        {
            "_id": order_id,
            "user_id": user_id,
            "activated": {"$ne": True},
            "activation_state": {"$nin": ["processing", "activated"]},
            "payment_status": {"$ne": "success"},
        },
        {"$set": {
            "activation_state": "processing",
            "transaction_id": transaction_id,
            "activation_started_at": _naive_utc(),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        latest = await premium_orders.find_one({"_id": order_id, "user_id": user_id})
        if latest and latest.get("activated"):
            return "already_activated"
        return "processing"

    try:
        user = await db.get_user(user_id)
        now = dt.datetime.now()
        expiry = user.get("expiry_time") if user else None
        if isinstance(expiry, dt.datetime):
            # Align timezone awareness before comparison.
            comparison_now = now.replace(tzinfo=expiry.tzinfo) if expiry.tzinfo else now
            base_time = expiry if expiry > comparison_now else now
        else:
            base_time = now

        seconds = await get_seconds(f'{int(order["months"])} month')
        new_expiry = base_time + dt.timedelta(seconds=int(seconds))
        await db.update_user({"id": user_id, "expiry_time": new_expiry})

        await premium_orders.update_one(
            {"_id": order_id, "user_id": user_id, "activation_state": "processing"},
            {"$set": {
                "activated": True,
                "activation_state": "activated",
                "payment_status": "success",
                "activated_at": _naive_utc(),
                "expiry_time": new_expiry,
                "transaction_id": transaction_id,
            }},
        )
        logger.info("Premium activated user=%s order=%s months=%s", user_id, order_id, order["months"])
        return "activated"
    except Exception:
        logger.exception("Premium activation failed for order %s", order_id)
        await premium_orders.update_one(
            {"_id": order_id, "user_id": user_id, "activation_state": "processing", "activated": {"$ne": True}},
            {"$set": {"activation_state": "pending"}},
        )
        raise


# ------------------------------ Keyboards ----------------------------------
def payment_methods_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ONLINE PAYMENT", callback_data="api_buy_start")],
        [InlineKeyboardButton("MANUAL UPI", callback_data="upi_info")],
        [InlineKeyboardButton("BACK", callback_data="premium_pay_back")],
    ])


def payment_keyboard(order_id, payment_url):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("PAY NOW", url=payment_url)],
        [InlineKeyboardButton("CHECK PAYMENT", callback_data=f"paycheck_{order_id}")],
        [InlineKeyboardButton("MANUAL UPI", callback_data="upi_info")],
        [InlineKeyboardButton("CANCEL / BACK", callback_data="premium_pay_back")],
    ])


def _months_prompt_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("CANCEL", callback_data="premium_pay_back")]])


# ------------------------------ Admin command ------------------------------
@Client.on_message(filters.command("pset") & filters.user(ADMINS), group=-1)
async def set_premium_price(client, message):
    if len(message.command) != 2:
        price = await get_monthly_price()
        await message.reply_text(
            "PREMIUM PRICING\n\n"
            f"Current monthly price: ₹{price}\n\n"
            "Usage: /pset 20\n"
            "Enter a whole-number amount between ₹1 and ₹100000."
        )
        raise StopPropagation
    try:
        price = int(message.command[1])
        if not 1 <= price <= 100000:
            raise ValueError
        await set_monthly_price(price)
    except ValueError:
        await message.reply_text("Invalid price. Example: /pset 20")
        raise StopPropagation
    except Exception:
        logger.exception("Could not update premium pricing")
        await message.reply_text("Unable to update the price right now. Check the bot logs.")
        raise StopPropagation

    await message.reply_text(
        "PREMIUM PRICE UPDATED\n\n"
        f"Monthly: ₹{price}\n"
        f"3 months: ₹{price * 3}\n"
        f"6 months: ₹{price * 6}\n"
        f"12 months: ₹{price * 12}"
    )
    raise StopPropagation


# ------------------------------ Payment menu -------------------------------
@Client.on_callback_query(filters.regex(r"^buy_info$"), group=-1)
async def updated_buy_info(client, query):
    await _safe_answer(query)
    await _edit_or_reply(
        query.message,
        "CHOOSE YOUR PREMIUM PAYMENT METHOD\n\n"
        "ONLINE PAYMENT — automatic verification\n"
        "MANUAL UPI — admin verifies payment manually\n\n"
        "Select a payment method below.",
        payment_methods_keyboard(),
    )
    raise StopPropagation


@Client.on_callback_query(filters.regex(r"^premium_pay_back$"), group=-1)
async def premium_payment_back(client, query):
    await _safe_answer(query)
    # Back/cancel removes the active payment panel rather than opening another menu.
    await _delete_message(query.message)
    PREMIUM_MONTH_INPUT.pop(query.from_user.id, None)
    raise StopPropagation


@Client.on_callback_query(filters.regex(r"^api_buy_start$"), group=-1)
async def start_online_payment(client, query):
    user_id = query.from_user.id
    PREMIUM_MONTH_INPUT[user_id] = query.message.id
    price = await get_monthly_price()
    await _safe_answer(query)
    await _edit_or_reply(
        query.message,
        "BUY PREMIUM — ONLINE PAYMENT\n\n"
        f"Monthly price: ₹{price}\n\n"
        "Send the number of months you want to purchase.\n"
        "Examples: 1 = 1 month, 3 = 3 months, 6 = 6 months.\n\n"
        f"Choose a whole number from {MIN_MONTHS} to {MAX_MONTHS}.",
        _months_prompt_keyboard(),
    )
    raise StopPropagation


# ------------------------------ Month input --------------------------------
@Client.on_message(filters.private & filters.text & ~filters.command("start"), group=-1)
async def receive_premium_months(client, message):
    user_id = message.from_user.id if message.from_user else None
    if user_id not in PREMIUM_MONTH_INPUT:
        return

    lock = PREMIUM_MONTH_LOCKS.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await _delete_message(message)
        raise StopPropagation

    async with lock:
        raw = (message.text or "").strip()
        if not raw.isdigit():
            await message.reply_text(f"Please send a whole number from {MIN_MONTHS} to {MAX_MONTHS}.")
            raise StopPropagation
        months = int(raw)
        if not MIN_MONTHS <= months <= MAX_MONTHS:
            await message.reply_text(f"You can purchase between {MIN_MONTHS} and {MAX_MONTHS} months.")
            raise StopPropagation

        panel_id = PREMIUM_MONTH_INPUT.pop(user_id, None)
        try:
            order = await create_payment_order(user_id, months)
            panel = None
            if panel_id:
                try:
                    panel = await client.get_messages(message.chat.id, panel_id)
                except Exception:
                    panel = None
            text = (
                "PAYMENT ORDER CREATED\n\n"
                f"Order ID: {order['order_id']}\n"
                f"Plan duration: {months} month(s)\n"
                f"Total amount: ₹{order['amount']}\n\n"
                "1. Select PAY NOW and complete payment.\n"
                "2. Return here and select CHECK PAYMENT.\n"
                "3. Premium activates only after successful server-side verification."
            )
            markup = payment_keyboard(order["order_id"], order["payment_url"])
            if panel:
                await _edit_or_reply(panel, text, markup)
            else:
                await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            logger.exception("Could not create payment order for user %s", user_id)
            fallback = InlineKeyboardMarkup([
                [InlineKeyboardButton("MANUAL UPI", callback_data="upi_info")],
                [InlineKeyboardButton("BACK", callback_data="premium_pay_back")],
            ])
            text = "ONLINE PAYMENT UNAVAILABLE\n\nNo premium has been activated. Please try again later or use Manual UPI."
            try:
                panel = await client.get_messages(message.chat.id, panel_id) if panel_id else None
            except Exception:
                panel = None
            if panel:
                await _edit_or_reply(panel, text, fallback)
            else:
                await message.reply_text(text, reply_markup=fallback)
        finally:
            # Remove the user's numeric input message for a cleaner chat.
            await _delete_message(message)

    raise StopPropagation


# ------------------------------ Check payment ------------------------------
@Client.on_callback_query(filters.regex(r"^paycheck_[A-Za-z0-9_-]+$"), group=-1)
async def check_premium_payment(client, query):
    user_id = query.from_user.id
    order_id = query.data[len("paycheck_"):]
    order = await premium_orders.find_one({"_id": order_id, "user_id": user_id})
    if not order:
        await _safe_answer(query, "Payment order not found for your account.", True)
        raise StopPropagation
    if order.get("activated"):
        await _safe_answer(query, "This order has already activated your premium.", True)
        raise StopPropagation
    if order.get("activation_state") == "processing":
        await _safe_answer(query, "Activation is processing. Please wait a moment.", True)
        raise StopPropagation

    await _safe_answer(query, "Checking payment status…")
    try:
        status, result = await get_payment_status(order)
        if status == "pending":
            await _safe_answer(query, "Payment is still pending. Complete payment, then check again.", True)
            raise StopPropagation
        if status in {"failed", "cancelled"}:
            await premium_orders.update_one(
                {"_id": order_id, "user_id": user_id, "activated": {"$ne": True}},
                {"$set": {"payment_status": status, "last_checked_at": _naive_utc()}},
            )
            await _safe_answer(query, f"Payment status: {status}. Premium was not activated.", True)
            raise StopPropagation

        transaction_id = result.get("transaction_id")
        activation = await activate_order_once(order, transaction_id)
        if activation == "already_activated":
            await _safe_answer(query, "This payment has already been applied.", True)
            raise StopPropagation
        if activation == "processing":
            await _safe_answer(query, "Activation is already processing. Try again shortly.", True)
            raise StopPropagation

        await _safe_answer(query, "Payment verified. Premium activated successfully.", True)
        success_text = (
            "PREMIUM ACTIVATED SUCCESSFULLY\n\n"
            f"Duration: {order['months']} month(s)\n"
            f"Amount paid: ₹{order['amount']}\n"
            f"Order ID: {order_id}\n"
            f"Transaction ID: {transaction_id or 'Not provided'}\n\n"
            "Use /myplan to view your current subscription."
        )
        await _edit_or_reply(query.message, success_text, None)
    except StopPropagation:
        raise
    except Exception:
        logger.exception("Payment verification failed for order %s", order_id)
        await _safe_answer(query, "Unable to verify payment right now. Please try again later.", True)
    raise StopPropagation
