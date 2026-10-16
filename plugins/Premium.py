# ============================================================
# PREMIUM PAYMENT API INTEGRATION
# Add this section at the END of your existing Premium.py
# ============================================================

import asyncio
import logging
import datetime
from decimal import Decimal, InvalidOperation

import aiohttp

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import MessageNotModified

from info import ADMINS
from database.users_chats_db import db
from utils import get_seconds


logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# PAYMENT API CONFIGURATION
# ------------------------------------------------------------

PAYMENT_CREATE_API = "https://checkout.ascinemaa.bond/api.php"
PAYMENT_API_KEY = "heyupi"

# Users awaiting month input
PREMIUM_MONTH_INPUT = set()

# MongoDB collections
premium_orders = db.db["premium_orders"]
premium_settings = db.db["premium_settings"]


# ------------------------------------------------------------
# COMMON HELPERS
# ------------------------------------------------------------

async def get_monthly_price():
    """Get monthly premium price from MongoDB."""
    settings = await premium_settings.find_one({"_id": "pricing"})

    if settings and settings.get("monthly_price") is not None:
        return int(settings["monthly_price"])

    # Default price if admin has not configured one yet.
    return 20


async def create_payment_order(user_id: int, months: int):
    """Create an order through the payment API."""

    monthly_price = await get_monthly_price()
    total_amount = monthly_price * months

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            PAYMENT_CREATE_API,
            params={
                "api_key": PAYMENT_API_KEY,
                "amount": total_amount,
            },
        ) as response:

            if response.status != 200:
                raise RuntimeError(
                    f"Payment API returned HTTP {response.status}"
                )

            result = await response.json(content_type=None)

    if result.get("error") is not False:
        raise RuntimeError(
            result.get("message", "Payment order creation failed")
        )

    data = result.get("data") or {}

    order_id = data.get("order_id")
    payment_url = data.get("payment_url")
    status_check_url = data.get("status_check_url")

    if not order_id or not payment_url or not status_check_url:
        raise RuntimeError("Payment API returned incomplete order details")

    # Verify that the API amount matches the amount requested.
    try:
        api_amount = Decimal(str(data.get("amount")))
        expected_amount = Decimal(str(total_amount))
    except (InvalidOperation, TypeError):
        raise RuntimeError("Invalid amount returned by payment API")

    if api_amount != expected_amount:
        raise RuntimeError("Payment API amount mismatch")

    # Store ownership and expected amount in MongoDB.
    await premium_orders.insert_one({
        "_id": str(order_id),
        "user_id": int(user_id),
        "months": int(months),
        "amount": str(expected_amount),
        "payment_url": str(payment_url),
        "status_check_url": str(status_check_url),
        "payment_status": "pending",
        "activation_state": "pending",
        "activated": False,
        "created_at": datetime.datetime.utcnow(),
    })

    return {
        "order_id": str(order_id),
        "amount": total_amount,
        "payment_url": str(payment_url),
    }


async def get_payment_status(order):
    """Fetch the current payment status from the API."""

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            order["status_check_url"]
        ) as response:

            if response.status != 200:
                raise RuntimeError(
                    f"Payment status API returned HTTP {response.status}"
                )

            result = await response.json(content_type=None)

    if result.get("error") is True:
        raise RuntimeError(
            result.get("message", "Unable to verify payment")
        )

    # Verify the returned order ID.
    if str(result.get("order_id", "")) != str(order["_id"]):
        raise RuntimeError("Order ID verification failed")

    # Verify the amount against the amount stored in MongoDB.
    try:
        api_amount = Decimal(str(result.get("amount")))
        expected_amount = Decimal(str(order["amount"]))
    except (InvalidOperation, TypeError):
        raise RuntimeError("Payment amount verification failed")

    if api_amount != expected_amount:
        raise RuntimeError("Payment amount mismatch")

    status = str(result.get("payment_status", "")).lower()

    if status not in ("pending", "success", "failed", "cancelled"):
        raise RuntimeError("Unknown payment status")

    return status, result


async def activate_order_once(order, transaction_id=None):
    """
    Activate premium once per order.

    MongoDB atomically claims the order before extending premium,
    preventing concurrent Check Payment clicks from granting it twice.
    """

    order_id = str(order["_id"])
    user_id = int(order["user_id"])

    claimed = await premium_orders.find_one_and_update(
        {
            "_id": order_id,
            "user_id": user_id,
            "activated": {"$ne": True},
            "activation_state": {"$ne": "processing"},
        },
        {
            "$set": {
                "activation_state": "processing",
                "transaction_id": transaction_id,
            }
        },
        return_document=True,
    )

    if not claimed:
        latest = await premium_orders.find_one({"_id": order_id})

        if latest and latest.get("activated"):
            return "already_activated"

        return "processing"

    try:
        user = await db.get_user(user_id)

        now = datetime.datetime.now()

        old_expiry = user.get("expiry_time") if user else None

        # Extend an existing valid premium subscription.
        if isinstance(old_expiry, datetime.datetime):
            compare_now = (
                now.replace(tzinfo=old_expiry.tzinfo)
                if old_expiry.tzinfo
                else now.replace(tzinfo=None)
            )

            if old_expiry > compare_now:
                base_time = old_expiry
            else:
                base_time = now
        else:
            base_time = now

        seconds = await get_seconds(f'{int(order["months"])} month')

        new_expiry = base_time + datetime.timedelta(seconds=seconds)

        # Use the existing premium database system.
        await db.update_user({
            "id": user_id,
            "expiry_time": new_expiry,
        })

        await premium_orders.update_one(
            {
                "_id": order_id,
                "user_id": user_id,
            },
            {
                "$set": {
                    "activated": True,
                    "activation_state": "activated",
                    "payment_status": "success",
                    "activated_at": datetime.datetime.utcnow(),
                    "expiry_time": new_expiry,
                    "transaction_id": transaction_id,
                }
            },
        )

        logger.info(
            "Premium activated: user=%s order=%s months=%s",
            user_id,
            order_id,
            order["months"],
        )

        return "activated"

    except Exception:
        logger.exception("Premium activation failed for order %s", order_id)

        # Release the processing lock so activation can be retried.
        await premium_orders.update_one(
            {
                "_id": order_id,
                "user_id": user_id,
                "activation_state": "processing",
                "activated": {"$ne": True},
            },
            {
                "$set": {
                    "activation_state": "pending",
                }
            },
        )

        raise


def payment_keyboard(order_id, payment_url):
    """Payment buttons shown to the user."""

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💳 Pay Now",
                url=payment_url,
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Check Payment",
                callback_data=f"paycheck_{order_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🛠 Manual UPI Purchase",
                callback_data="upi_info",
            )
        ],
    ])


# ------------------------------------------------------------
# ADMIN COMMAND: /pset 20
# ------------------------------------------------------------

@Client.on_message(
    filters.command("pset") & filters.user(ADMINS),
    group=-1,
)
async def set_premium_price(client, message):

    if len(message.command) != 2:
        current_price = await get_monthly_price()

        await message.reply_text(
            "💰 **Premium Monthly Price**\n\n"
            f"Current price: ₹{current_price}/month\n\n"
            "Usage: `/pset 20`\n"
            "Example: `/pset 50`"
        )
        raise StopPropagation

    try:
        price = int(message.command[1])

        if price < 1 or price > 100000:
            raise ValueError

    except ValueError:
        await message.reply_text(
            "❌ Invalid price.\n"
            "Use a positive whole number, for example `/pset 20`."
        )
        raise StopPropagation

    await premium_settings.update_one(
        {"_id": "pricing"},
        {
            "$set": {
                "monthly_price": price,
                "updated_at": datetime.datetime.utcnow(),
            }
        },
        upsert=True,
    )

    await message.reply_text(
        "✅ **Premium Price Updated**\n\n"
        f"Monthly price: ₹{price}\n"
        f"3 months: ₹{price * 3}\n"
        f"6 months: ₹{price * 6}\n"
        f"12 months: ₹{price * 12}"
    )

    raise StopPropagation


# ------------------------------------------------------------
# BUY PREMIUM MENU
# Intercepts the existing buy_info callback.
# ------------------------------------------------------------

@Client.on_callback_query(
    filters.regex(r"^buy_info$"),
    group=-1,
)
async def updated_buy_info(client, query):

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💳 Online Payment",
                callback_data="api_buy_start",
            )
        ],
        [
            InlineKeyboardButton(
                "⭐ Telegram Stars",
                callback_data="star_info",
            )
        ],
        [
            InlineKeyboardButton(
                "🛠 Manual UPI",
                callback_data="upi_info",
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 Back",
                callback_data="premium_info",
            )
        ],
    ])

    try:
        await query.message.reply_text(
            "💎 **Choose Your Premium Payment Method**\n\n"
            "💳 Online Payment — automatic verification\n"
            "⭐ Telegram Stars — existing Stars payment\n"
            "🛠 Manual UPI — contact admin for manual activation",
            reply_markup=buttons,
        )

        await query.answer()

    except Exception:
        logger.exception("Unable to show premium payment menu")
        await query.answer(
            "Unable to open the payment menu.",
            show_alert=True,
        )

    raise StopPropagation


# ------------------------------------------------------------
# START ONLINE PAYMENT
# ------------------------------------------------------------

@Client.on_callback_query(
    filters.regex(r"^api_buy_start$"),
    group=-1,
)
async def start_online_payment(client, query):

    user_id = query.from_user.id

    PREMIUM_MONTH_INPUT.add(user_id)

    await query.answer()

    await query.message.reply_text(
        "💎 **Buy Premium**\n\n"
        f"Monthly price: ₹{await get_monthly_price()}\n\n"
        "Send the number of months you want to purchase.\n\n"
        "Examples:\n"
        "`1` = 1 month\n"
        "`3` = 3 months\n"
        "`6` = 6 months\n\n"
        "Send a whole number between 1 and 24."
    )

    raise StopPropagation


# ------------------------------------------------------------
# RECEIVE MONTHS AND CREATE PAYMENT ORDER
# ------------------------------------------------------------

@Client.on_message(
    filters.private & filters.text & ~filters.command([]),
    group=-1,
)
async def receive_premium_months(client, message):

    user_id = message.from_user.id

    if user_id not in PREMIUM_MONTH_INPUT:
        return

    text = (message.text or "").strip()

    if not text.isdigit():
        await message.reply_text(
            "❌ Please send a valid number of months, such as `1`, `3` or `6`."
        )
        raise StopPropagation

    months = int(text)

    if months < 1 or months > 24:
        await message.reply_text(
            "❌ You can purchase between 1 and 24 months."
        )
        raise StopPropagation

    # Stop duplicate order creation from repeated text messages.
    PREMIUM_MONTH_INPUT.discard(user_id)

    try:
        order = await create_payment_order(user_id, months)

        await message.reply_text(
            "✅ **Payment Order Created**\n\n"
            f"📦 Order ID: `{order['order_id']}`\n"
            f"📅 Duration: {months} month(s)\n"
            f"💰 Total Amount: ₹{order['amount']}\n\n"
            "1. Tap **Pay Now** and complete your payment.\n"
            "2. Return here and tap **Check Payment**.\n"
            "3. Premium will activate after successful verification.",
            reply_markup=payment_keyboard(
                order["order_id"],
                order["payment_url"],
            ),
        )

    except Exception:
        logger.exception("Could not create payment order for user %s", user_id)

        await message.reply_text(
            "⚠️ **Online payment is temporarily unavailable.**\n\n"
            "Your premium has not been activated or charged through this bot.\n"
            "Please use the existing manual UPI purchase option.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🛠 Manual UPI Purchase",
                        callback_data="upi_info",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 Premium Menu",
                        callback_data="premium_info",
                    )
                ],
            ]),
        )

    raise StopPropagation


# ------------------------------------------------------------
# CHECK PAYMENT STATUS
# ------------------------------------------------------------

@Client.on_callback_query(
    filters.regex(r"^paycheck_[A-Za-z0-9_-]+$"),
    group=-1,
)
async def check_premium_payment(client, query):

    user_id = query.from_user.id
    order_id = query.data[len("paycheck_"):]

    order = await premium_orders.find_one({
        "_id": order_id,
        "user_id": user_id,
    })

    # An order belonging to another user cannot activate premium.
    if not order:
        await query.answer(
            "❌ Payment order not found for your account.",
            show_alert=True,
        )
        raise StopPropagation

    # Never extend premium again for an already activated order.
    if order.get("activated"):
        await query.answer(
            "✅ This payment has already activated your premium.",
            show_alert=True,
        )
        raise StopPropagation

    if order.get("activation_state") == "processing":
        await query.answer(
            "⏳ Your activation is being processed. Try again shortly.",
            show_alert=True,
        )
        raise StopPropagation

    try:
        status, result = await get_payment_status(order)

        if status == "pending":
            await query.answer(
                "⏳ Payment is still pending. Complete payment first.",
                show_alert=True,
            )
            raise StopPropagation

        if status in ("failed", "cancelled"):
            await premium_orders.update_one(
                {"_id": order_id, "user_id": user_id},
                {"$set": {"payment_status": status}},
            )

            await query.answer(
                f"❌ Payment status: {status}. Premium was not activated.",
                show_alert=True,
            )
            raise StopPropagation

        transaction_id = result.get("transaction_id")

        activation = await activate_order_once(
            order,
            transaction_id=transaction_id,
        )

        if activation == "already_activated":
            await query.answer(
                "✅ Premium has already been activated for this order.",
                show_alert=True,
            )
            raise StopPropagation

        if activation == "processing":
            await query.answer(
                "⏳ Activation is already processing. Try again shortly.",
                show_alert=True,
            )
            raise StopPropagation

        await query.answer(
            "✅ Payment verified! Premium activated.",
            show_alert=True,
        )

        try:
            await query.message.reply_text(
                "🎉 **Premium Activated Successfully!**\n\n"
                f"📅 Duration: {order['months']} month(s)\n"
                f"💰 Paid: ₹{order['amount']}\n"
                f"🧾 Order ID: `{order_id}`\n"
                f"🔖 Transaction ID: `{transaction_id or 'Not provided'}`\n\n"
                "Use `/myplan` to check your subscription."
            )
        except Exception:
            logger.exception("Could not send activation confirmation")

    except StopPropagation:
        raise

    except Exception:
        logger.exception("Payment verification failed for order %s", order_id)

        await query.answer(
            "⚠️ Unable to verify payment right now. Please try again later.",
            show_alert=True,
        )

    raise StopPropagation
