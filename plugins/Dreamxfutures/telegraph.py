import os
import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message


IMGBB_API_KEY = os.getenv(
    "IMGBB_API_KEY",
    "d4cc3d793cb68b2c6cdc2197588e895c"
)

IMGBB_URL = "https://api.imgbb.com/1/upload"


async def upload_to_imgbb(file_path: str):
    """
    Upload a local image/file to ImgBB.

    Returns:
        str: ImgBB image URL
        None: if upload failed
    """

    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as file:
            image_bytes = file.read()

        form = aiohttp.FormData()
        form.add_field("key", IMGBB_API_KEY)
        form.add_field(
            "image",
            image_bytes,
            filename=os.path.basename(file_path)
        )

        timeout = aiohttp.ClientTimeout(total=120)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(IMGBB_URL, data=form) as response:

                if response.status != 200:
                    return None

                result = await response.json()

        if result.get("success"):
            return result["data"]["url"]

        return None

    except Exception:
        return None


@Client.on_message(
    filters.command(
        ["img", "cup", "telegraph"],
        prefixes="/"
    ) & filters.reply
)
async def c_upload(client, message: Message):

    reply = message.reply_to_message

    if not reply or not reply.media:
        return await message.reply_text(
            "Reply to a media to upload it to Cloud."
        )

    # ImgBB is mainly for images.
    if not reply.photo and not reply.document:
        return await message.reply_text(
            "Please reply to an image."
        )

    # 5 MB limit
    if reply.document and reply.document.file_size:
        if reply.document.file_size > 5 * 1024 * 1024:
            return await message.reply_text(
                "File size limit is 5 MB."
            )

    msg = await message.reply_text("⏳ Processing...")

    downloaded_media = None

    try:
        # Download Telegram media
        downloaded_media = await reply.download()

        if not downloaded_media or not os.path.exists(downloaded_media):
            return await msg.edit_text(
                "❌ Something went wrong during download."
            )

        # Upload to ImgBB
        image_url = await upload_to_imgbb(downloaded_media)

        if not image_url:
            return await msg.edit_text(
                "❌ ImgBB upload failed. Please try again later."
            )

        # Send URL
        await msg.edit_text(
            f"🖼️ <b>ImgBB URL:</b>\n\n"
            f"<code>{image_url}</code>"
        )

    except Exception as e:
        await msg.edit_text(
            f"❌ Error: <code>{str(e)}</code>"
        )

    finally:
        # Always remove downloaded file
        if downloaded_media and os.path.exists(downloaded_media):
            try:
                os.remove(downloaded_media)
            except Exception:
                pass
