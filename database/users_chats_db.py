import datetime
import logging
import pytz
import motor.motor_asyncio

from info import (
    DATABASE_NAME, DATABASE_URI, DATABASE_URI2, MULTIPLE_DB, MAINTENANCE,
    PM_SEARCH, BUTTON_MODE, P_TTI_SHOW_OFF, PROTECT_CONTENT, IMDB,
    SPELL_CHECK_REPLY, MELCOW_NEW_USERS, AUTO_DELETE, AUTO_FFILTER, MAX_BTN,
    IMDB_TEMPLATE, LOG_VR_CHANNEL, TUTORIAL, TUTORIAL_2, TUTORIAL_3,
    SHORTENER_API, SHORTENER_API2, SHORTENER_API3, SHORTENER_WEBSITE,
    SHORTENER_WEBSITE2, SHORTENER_WEBSITE3, IS_VERIFY, TWO_VERIFY_GAP,
    THREE_VERIFY_GAP, CUSTOM_FILE_CAPTION, AUTH_CHANNELS,
    MOVIE_UPDATE_NOTIFICATION,
)

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]

        # Existing collections
        self.col = self.db.users
        self.grp = self.db.groups
        self.users = self.db.uersz  # Premium users collection (existing name)
        self.req = self.db.requests
        self.botcol = self.db.bot_settings
        self.misc = self.db.misc
        self.verify_id = self.db.verify_id
        self.codes = self.db.codes
        self.filename_col = self.db.filename
        self.movie_updates = self.db.movie_updates
        self.connection = self.db.connections

        # Persistent storage for premium payment settings/orders.
        # These collections are safe to create lazily in MongoDB.
        self.premium_payment_settings = self.db.premium_payment_settings
        self.premium_payment_orders = self.db.premium_payment_orders

    # -------------------- Movie update notifications --------------------
    async def add_name(self, filename):
        if await self.movie_updates.find_one({"_id": filename}):
            return False
        await self.movie_updates.insert_one({"_id": filename})
        return True

    async def delete_all_msg(self):
        await self.movie_updates.delete_many({})
        logger.info("All filenames notification have been deleted.")
        return True

    # -------------------- Join requests --------------------
    async def add_join_req(self, user_id: int, channel_id: int):
        await self.req.update_one(
            {"user_id": int(user_id)},
            {
                "$addToSet": {"channels": int(channel_id)},
                "$setOnInsert": {"created_at": datetime.datetime.utcnow()},
            },
            upsert=True,
        )

    async def has_joined_channel(self, user_id: int, channel_id: int):
        doc = await self.req.find_one({"user_id": int(user_id)})
        return bool(doc and int(channel_id) in doc.get("channels", []))

    async def del_join_req(self):
        await self.req.drop()

    # -------------------- User/group records --------------------
    def new_user(self, id, name):
        return {
            "id": int(id),
            "name": name,
            "ban_status": {"is_banned": False, "ban_reason": ""},
        }

    def new_group(self, id, title):
        return {
            "id": int(id),
            "title": title,
            "chat_status": {"is_disabled": False, "reason": ""},
        }

    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.update_one({"id": int(id)}, {"$setOnInsert": user}, upsert=True)

    async def is_user_exist(self, id):
        return bool(await self.col.find_one({"id": int(id)}))

    async def total_users_count(self):
        return await self.col.count_documents({})

    async def remove_ban(self, id):
        await self.col.update_one(
            {"id": int(id)},
            {"$set": {"ban_status": {"is_banned": False, "ban_reason": ""}}},
        )

    async def ban_user(self, user_id, ban_reason="No Reason"):
        await self.col.update_one(
            {"id": int(user_id)},
            {"$set": {"ban_status": {"is_banned": True, "ban_reason": ban_reason}}},
        )

    async def get_ban_status(self, id):
        default = {"is_banned": False, "ban_reason": ""}
        user = await self.col.find_one({"id": int(id)})
        return user.get("ban_status", default) if user else default

    async def get_all_users(self):
        return self.col.find({})

    async def delete_user(self, user_id):
        await self.col.delete_many({"id": int(user_id)})

    async def delete_chat(self, id):
        await self.grp.delete_many({"id": int(id)})

    async def get_banned(self):
        users = self.col.find({"ban_status.is_banned": True})
        chats = self.grp.find({"chat_status.is_disabled": True})
        b_chats = [chat["id"] async for chat in chats]
        b_users = [user["id"] async for user in users]
        return b_users, b_chats

    async def add_chat(self, chat, title):
        group = self.new_group(chat, title)
        await self.grp.update_one({"id": int(chat)}, {"$setOnInsert": group}, upsert=True)

    async def get_chat(self, chat):
        group = await self.grp.find_one({"id": int(chat)})
        return False if not group else group.get("chat_status")

    async def re_enable_chat(self, id):
        await self.grp.update_one(
            {"id": int(id)},
            {"$set": {"chat_status": {"is_disabled": False, "reason": ""}}},
        )

    async def update_settings(self, id, settings):
        await self.grp.update_one(
            {"id": int(id)}, {"$set": {"settings": settings}}, upsert=True
        )

    async def get_settings(self, id):
        default = {
            "button": BUTTON_MODE,
            "botpm": P_TTI_SHOW_OFF,
            "file_secure": PROTECT_CONTENT,
            "imdb": IMDB,
            "spell_check": SPELL_CHECK_REPLY,
            "welcome": MELCOW_NEW_USERS,
            "auto_delete": AUTO_DELETE,
            "auto_ffilter": AUTO_FFILTER,
            "max_btn": MAX_BTN,
            "template": IMDB_TEMPLATE,
            "log": LOG_VR_CHANNEL,
            "tutorial": TUTORIAL,
            "tutorial_2": TUTORIAL_2,
            "tutorial_3": TUTORIAL_3,
            "shortner": SHORTENER_WEBSITE,
            "api": SHORTENER_API,
            "shortner_two": SHORTENER_WEBSITE2,
            "api_two": SHORTENER_API2,
            "shortner_three": SHORTENER_WEBSITE3,
            "api_three": SHORTENER_API3,
            "is_verify": IS_VERIFY,
            "verify_time": TWO_VERIFY_GAP,
            "third_verify_time": THREE_VERIFY_GAP,
            "caption": CUSTOM_FILE_CAPTION,
            "fsub": AUTH_CHANNELS,
        }
        group = await self.grp.find_one({"id": int(id)})
        if group and "settings" in group:
            saved = group["settings"]
            result = default.copy()
            result.update(saved)
            if not saved.get("custom_shortner"):
                result["shortner"] = SHORTENER_WEBSITE
                result["api"] = SHORTENER_API
            if not saved.get("custom_shortner_two"):
                result["shortner_two"] = SHORTENER_WEBSITE2
                result["api_two"] = SHORTENER_API2
            if not saved.get("custom_shortner_three"):
                result["shortner_three"] = SHORTENER_WEBSITE3
                result["api_three"] = SHORTENER_API3
            return result
        return default.copy()

    async def dreamx_reset_settings(self):
        try:
            result = await self.grp.update_many(
                {"settings": {"$exists": True}}, {"$unset": {"settings": ""}}
            )
            return result.modified_count
        except Exception as e:
            logger.error("Failed to reset group settings: %s", e)
            raise

    async def disable_chat(self, chat, reason="No Reason"):
        await self.grp.update_one(
            {"id": int(chat)},
            {"$set": {"chat_status": {"is_disabled": True, "reason": reason}}},
        )

    async def total_chat_count(self):
        return await self.grp.count_documents({})

    async def get_all_chats(self):
        return self.grp.find({})

    async def get_db_size(self):
        return (await self.db.command("dbstats"))["dataSize"]

    # -------------------- Premium users --------------------
    async def get_user(self, user_id):
        return await self.users.find_one({"id": int(user_id)})

    async def update_user(self, user_data, expiry_time=None):
        """
        Update premium-user data.

        Preferred usage:
            await db.update_user({"id": user_id, "expiry_time": expiry})

        Backward-compatible usage:
            await db.update_user(user_id, expiry_datetime)

        Only provided fields are updated, so fields such as has_free_trial
        and existing premium metadata are not accidentally removed.
        """
        if expiry_time is not None:
            user_data = {"id": int(user_data), "expiry_time": expiry_time}
        elif isinstance(user_data, dict):
            user_data = dict(user_data)
            if "id" not in user_data:
                raise ValueError("update_user requires an 'id' field")
            user_data["id"] = int(user_data["id"])
        else:
            raise TypeError("update_user expects a user-data dict")

        user_id = user_data["id"]
        update_fields = {key: value for key, value in user_data.items() if key != "_id"}
        await self.users.update_one(
            {"id": user_id},
            {"$set": update_fields},
            upsert=True,
        )

    async def has_premium_access(self, user_id):
        user_data = await self.get_user(user_id)
        if not user_data:
            return False

        expiry_time = user_data.get("expiry_time")
        if not isinstance(expiry_time, datetime.datetime):
            if expiry_time is not None:
                await self.users.update_one(
                    {"id": int(user_id)}, {"$set": {"expiry_time": None}}
                )
            return False

        now = datetime.datetime.now(expiry_time.tzinfo) if expiry_time.tzinfo else datetime.datetime.now()
        if now <= expiry_time:
            return True

        await self.users.update_one(
            {"id": int(user_id)}, {"$set": {"expiry_time": None}}
        )
        return False

    async def update_one(self, filter_query, update_data):
        try:
            result = await self.users.update_one(filter_query, update_data)
            return result.matched_count == 1
        except Exception as e:
            logger.error("Error updating document: %s", e)
            return False

    async def get_expired(self, current_time):
        expired_users = []
        cursor = self.users.find({"expiry_time": {"$lt": current_time}})
        async for user in cursor:
            expired_users.append(user)
        return expired_users

    async def remove_premium_access(self, user_id):
        return await self.update_one(
            {"id": int(user_id)}, {"$set": {"expiry_time": None}}
        )

    async def check_trial_status(self, user_id):
        user_data = await self.get_user(user_id)
        return bool(user_data and user_data.get("has_free_trial", False))

    async def give_free_trial(self, user_id):
        user_id = int(user_id)
        expiry_time = datetime.datetime.now() + datetime.timedelta(minutes=5)
        await self.users.update_one(
            {"id": user_id},
            {"$set": {"id": user_id, "expiry_time": expiry_time, "has_free_trial": True}},
            upsert=True,
        )

    async def reset_free_trial(self, user_id=None):
        if user_id is None:
            result = await self.users.update_many(
                {}, {"$set": {"has_free_trial": False}}
            )
            return result.modified_count

        result = await self.users.update_one(
            {"id": int(user_id)}, {"$set": {"has_free_trial": False}}
        )
        return 1 if result.modified_count > 0 else 0

    async def all_premium_users(self):
        return await self.users.count_documents(
            {"expiry_time": {"$gt": datetime.datetime.now()}}
        )

    async def get_all_premium_user_data(self):
        return self.users.find(
            {"expiry_time": {"$gt": datetime.datetime.now()}}
        )

    # -------------------- Premium payment persistence --------------------
    async def get_premium_monthly_price(self, default=20):
        row = await self.premium_payment_settings.find_one(
            {"_id": "premium_monthly_price"}
        )
        if row and isinstance(row.get("amount"), int) and row["amount"] > 0:
            return row["amount"]
        return int(default)

    async def set_premium_monthly_price(self, amount):
        amount = int(amount)
        if amount < 1 or amount > 100000:
            raise ValueError("Premium monthly price must be between 1 and 100000")
        await self.premium_payment_settings.update_one(
            {"_id": "premium_monthly_price"},
            {
                "$set": {
                    "amount": amount,
                    "updated_at": datetime.datetime.utcnow(),
                }
            },
            upsert=True,
        )
        return amount

    async def save_premium_payment_order(self, token, order):
        order = dict(order)
        order["_id"] = str(token)
        await self.premium_payment_orders.update_one(
            {"_id": str(token)},
            {"$set": {key: value for key, value in order.items() if key != "_id"}},
            upsert=True,
        )
        return order

    async def get_premium_payment_order(self, token):
        return await self.premium_payment_orders.find_one({"_id": str(token)})

    async def update_premium_payment_order(self, token, changes):
        await self.premium_payment_orders.update_one(
            {"_id": str(token)}, {"$set": dict(changes)}
        )

    # -------------------- Verification data --------------------
    async def get_notcopy_user(self, user_id):
        user_id = int(user_id)
        user = await self.misc.find_one({"user_id": user_id})
        ist_timezone = pytz.timezone("Asia/Kolkata")
        if not user:
            result = {
                "user_id": user_id,
                "last_verified": datetime.datetime(
                    2020, 5, 17, 0, 0, 0, tzinfo=ist_timezone
                ),
                "second_time_verified": datetime.datetime(
                    2019, 5, 17, 0, 0, 0, tzinfo=ist_timezone
                ),
            }
            await self.misc.insert_one(result)
            return result
        return user

    async def update_notcopy_user(self, user_id, value: dict):
        return await self.misc.update_one(
            {"user_id": int(user_id)}, {"$set": value}
        )

    async def is_user_verified(self, user_id):
        user = await self.get_notcopy_user(user_id)
        past_date = user.get("last_verified")
        if not past_date:
            return False
        ist_timezone = pytz.timezone("Asia/Kolkata")
        if past_date.tzinfo is None:
            past_date = ist_timezone.localize(past_date)
        else:
            past_date = past_date.astimezone(ist_timezone)
        current_time = datetime.datetime.now(tz=ist_timezone)
        midnight = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        return (current_time - past_date).total_seconds() <= (
            current_time - midnight
        ).total_seconds()

    async def user_verified(self, user_id):
        user = await self.get_notcopy_user(user_id)
        past_date = user.get("second_time_verified")
        if not past_date:
            return False
        ist_timezone = pytz.timezone("Asia/Kolkata")
        if past_date.tzinfo is None:
            past_date = ist_timezone.localize(past_date)
        else:
            past_date = past_date.astimezone(ist_timezone)
        current_time = datetime.datetime.now(tz=ist_timezone)
        midnight = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        return (current_time - past_date).total_seconds() <= (
            current_time - midnight
        ).total_seconds()

    async def use_second_shortener(self, user_id, time):
        user = await self.get_notcopy_user(user_id)
        if not user.get("second_time_verified"):
            ist_timezone = pytz.timezone("Asia/Kolkata")
            await self.update_notcopy_user(
                user_id,
                {"second_time_verified": datetime.datetime(
                    2019, 5, 17, 0, 0, 0, tzinfo=ist_timezone
                )},
            )
            user = await self.get_notcopy_user(user_id)

        if await self.is_user_verified(user_id):
            past_date = user.get("last_verified")
            if not past_date:
                return False
            ist_timezone = pytz.timezone("Asia/Kolkata")
            if past_date.tzinfo is None:
                past_date = ist_timezone.localize(past_date)
            else:
                past_date = past_date.astimezone(ist_timezone)
            current_time = datetime.datetime.now(tz=ist_timezone)
            if current_time - past_date > datetime.timedelta(seconds=time):
                second_time = user.get("second_time_verified")
                if second_time:
                    if second_time.tzinfo is None:
                        second_time = ist_timezone.localize(second_time)
                    else:
                        second_time = second_time.astimezone(ist_timezone)
                    return second_time < past_date
        return False

    async def use_third_shortener(self, user_id, time):
        user = await self.get_notcopy_user(user_id)
        if not user.get("third_time_verified"):
            ist_timezone = pytz.timezone("Asia/Kolkata")
            await self.update_notcopy_user(
                user_id,
                {"third_time_verified": datetime.datetime(
                    2018, 5, 17, 0, 0, 0, tzinfo=ist_timezone
                )},
            )
            user = await self.get_notcopy_user(user_id)

        if await self.user_verified(user_id):
            past_date = user.get("second_time_verified")
            second_time = user.get("third_time_verified")
            if not past_date or not second_time:
                return False
            ist_timezone = pytz.timezone("Asia/Kolkata")
            if past_date.tzinfo is None:
                past_date = ist_timezone.localize(past_date)
            else:
                past_date = past_date.astimezone(ist_timezone)
            if second_time.tzinfo is None:
                second_time = ist_timezone.localize(second_time)
            else:
                second_time = second_time.astimezone(ist_timezone)
            current_time = datetime.datetime.now(tz=ist_timezone)
            if current_time - past_date > datetime.timedelta(seconds=time):
                return second_time < past_date
        return False

    async def create_verify_id(self, user_id: int, hash):
        result = {
            "user_id": int(user_id),
            "hash": hash,
            "verified": False,
        }
        return await self.verify_id.insert_one(result)

    async def get_verify_id_info(self, user_id: int, hash):
        return await self.verify_id.find_one(
            {"user_id": int(user_id), "hash": hash}
        )

    async def update_verify_id_info(self, user_id, hash, value: dict):
        return await self.verify_id.update_one(
            {"user_id": int(user_id), "hash": hash}, {"$set": value}
        )

    # -------------------- Bot settings --------------------
    async def get_bot_setting(self, bot_id, setting_key, default_value):
        bot = await self.botcol.find_one(
            {"id": int(bot_id)}, {setting_key: 1, "_id": 0}
        )
        return bot[setting_key] if bot and setting_key in bot else default_value

    async def update_bot_setting(self, bot_id, setting_key, value):
        await self.botcol.update_one(
            {"id": int(bot_id)}, {"$set": {setting_key: value}}, upsert=True
        )

    async def connect_group(self, group_id, user_id):
        user = await self.connection.find_one({"_id": int(user_id)})
        if user:
            if int(group_id) not in user.get("group_ids", []):
                await self.connection.update_one(
                    {"_id": int(user_id)},
                    {"$push": {"group_ids": int(group_id)}},
                )
        else:
            await self.connection.insert_one(
                {"_id": int(user_id), "group_ids": [int(group_id)]}
            )

    async def get_connected_grps(self, user_id):
        user = await self.connection.find_one({"_id": int(user_id)})
        return user.get("group_ids", []) if user else []

    async def remove_group_connection(self, group_id, user_id):
        await self.connection.update_one(
            {"_id": int(user_id)},
            {"$pull": {"group_ids": int(group_id)}},
        )

    async def pm_search_status(self, bot_id):
        return await self.get_bot_setting(bot_id, "PM_SEARCH", PM_SEARCH)

    async def update_pm_search_status(self, bot_id, enable):
        await self.update_bot_setting(bot_id, "PM_SEARCH", enable)

    async def movie_update_status(self, bot_id):
        return await self.get_bot_setting(
            bot_id, "MOVIE_UPDATE_NOTIFICATION", MOVIE_UPDATE_NOTIFICATION
        )

    async def update_movie_update_status(self, bot_id, enable):
        await self.update_bot_setting(bot_id, "MOVIE_UPDATE_NOTIFICATION", enable)

    async def maintenance_status(self, bot_id):
        return await self.get_bot_setting(bot_id, "MAINTENANCE", MAINTENANCE)

    async def update_maintenance_status(self, bot_id, enable):
        await self.update_bot_setting(bot_id, "MAINTENANCE", enable)


db = Database(DATABASE_URI, DATABASE_NAME)

if MULTIPLE_DB and DATABASE_URI2:
    db2 = Database(DATABASE_URI2, DATABASE_NAME)
else:
    db2 = db
