import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon import TelegramClient, errors
from telethon.tl.types import (
    Channel,
    Chat,
    User,
)

# ────────────────────────── CONFIG ──────────────────────────
API_ID = int(os.getenv("API_ID", "37927665"))
API_HASH = os.getenv("API_HASH", "6cc390ad7fdf473b9c5df526acfa18e0")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8969502153:AAESZix1iVgpbv4yCpiwBmN_-L8BQAp0ymo")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8580367479").split(",")}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────── GLOBAL STORAGE ─────────────────────
# In production use a database (SQLite, Redis, etc.)
user_clients: dict[str, TelegramClient] = {}          # phone -> client
ad_data: dict = {}                                    # stored ad (text, media, entities)
broadcast_interval: int = 60                          # minutes
scheduler_task: asyncio.Task | None = None

# ─────────────────────── FSM STATES ────────────────────────
class AddAccount(StatesGroup):
    waiting_phone = State()
    waiting_otp = State()
    waiting_password = State()   # only if 2FA enabled

class SetAd(StatesGroup):
    waiting_ad = State()

class SetInterval(StatesGroup):
    waiting_minutes = State()

# ─────────────────────── BOT INIT ──────────────────────────
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ─────────────────────── HELPERS ───────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def admin_only(func):
    async def wrapper(event, *args, **kwargs):
        uid = event.from_user.id if hasattr(event, "from_user") else None
        if uid and is_admin(uid):
            return await func(event, *args, **kwargs)
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ You are not an admin.", show_alert=True)
        elif isinstance(event, Message):
            await event.reply("⛔ You are not an admin.")
        return None
    return wrapper

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Account", callback_data="add_account")],
        [InlineKeyboardButton(text="⏱ Set Interval", callback_data="set_interval")],
        [InlineKeyboardButton(text="⚡ Force Send Now", callback_data="force_send")],
        [InlineKeyboardButton(text="📝 Set Ad", callback_data="set_ad")],
    ])

async def get_all_entities(client: TelegramClient):
    """Return a deduplicated list of all users, groups, channels, and contacts."""
    entities = []
    seen = set()

    # 1️⃣ All dialogs (DMs, groups, channels, bots)
    async for dialog in client.iter_dialogs():
        e = dialog.entity
        key = (type(e).__name__, e.id)
        if key not in seen:
            seen.add(key)
            entities.append(e)

    # 2️⃣ Contacts (some contacts may not appear in dialogs)
    try:
        contacts = await client.get_contacts()
        for c in contacts:
            key = ("User", c.id)
            if key not in seen:
                seen.add(key)
                entities.append(c)
    except Exception as exc:
        logger.warning("Could not fetch contacts: %s", exc)

    return entities

async def send_ad_to_entity(client: TelegramClient, entity, ad: dict):
    """Send the stored ad to a single entity using the given user client."""
    try:
        if ad["type"] == "text":
            await client.send_message(entity, ad["text"], entities=ad.get("entities"))
        elif ad["type"] == "photo":
            await client.send_file(entity, ad["file"], caption=ad.get("caption"),
                                   caption_entities=ad.get("caption_entities"))
        elif ad["type"] == "video":
            await client.send_file(entity, ad["file"], caption=ad.get("caption"),
                                   caption_entities=ad.get("caption_entities"))
        elif ad["type"] == "document":
            await client.send_file(entity, ad["file"], caption=ad.get("caption"),
                                   caption_entities=ad.get("caption_entities"))
        else:
            logger.warning("Unsupported ad type: %s", ad["type"])
    except errors.FloodWaitError as e:
        logger.warning("Flood wait %s seconds for %s", e.seconds, entity)
        await asyncio.sleep(e.seconds)
    except Exception as exc:
        logger.debug("Failed to send to %s: %s", entity, exc)

async def broadcast_ad():
    """Force‑send the stored ad to every entity of every connected account."""
    if not ad_data:
        logger.warning("No ad set – broadcast aborted.")
        return
    if not user_clients:
        logger.warning("No accounts connected – broadcast aborted.")
        return

    for phone, client in user_clients.items():
        if not client.is_connected():
            await client.connect()
        entities = await get_all_entities(client)
        logger.info("Broadcasting via %s to %d entities", phone, len(entities))
        for entity in entities:
            await send_ad_to_entity(client, entity, ad_data)
            await asyncio.sleep(0.3)   # avoid flood limits

async def scheduler_loop():
    """Background task that broadcasts every `broadcast_interval` minutes."""
    while True:
        await asyncio.sleep(broadcast_interval * 60)
        logger.info("Scheduled broadcast triggered.")
        await broadcast_ad()

# ─────────────────────── /start ────────────────────────────
@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Broadcaster Bot</b>\n\n"
        "Use the buttons below to manage your accounts and ads.",
        reply_markup=main_keyboard(),
    )

# ────────────────── CALLBACK: ADD ACCOUNT ──────────────────
@router.callback_query(F.data == "add_account")
@admin_only
async def cb_add_account(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📱 Send the phone number you want to log in (with country code, e.g. <code>+919876543210</code>)."
    )
    await state.set_state(AddAccount.waiting_phone)
    await callback.answer()

@router.message(AddAccount.waiting_phone)
@admin_only
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith("+"):
        await message.reply("❌ Phone must start with <code>+</code>. Try again.")
        return

    session_file = f"sessions/{phone.replace('+', '')}.session"
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        await state.update_data(
            phone=phone,
            client=client,
            phone_code_hash=sent.phone_code_hash,
        )
        await message.answer("✅ OTP sent. Please enter the code you received.")
        await state.set_state(AddAccount.waiting_otp)
    except Exception as exc:
        await message.reply(f"❌ Could not send OTP: {exc}")
        await client.disconnect()

@router.message(AddAccount.waiting_otp)
@admin_only
async def process_otp(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data["phone"]
    client: TelegramClient = data["client"]
    code = message.text.strip()

    try:
        await client.sign_in(phone, code, phone_code_hash=data["phone_code_hash"])
    except errors.SessionPasswordNeededError:
        await message.answer("🔐 Two‑factor authentication is enabled. Please send your password.")
        await state.set_state(AddAccount.waiting_password)
        return
    except Exception as exc:
        await message.reply(f"❌ Login failed: {exc}")
        await client.disconnect()
        await state.clear()
        return

    user_clients[phone] = client
    await message.answer(
        f"✅ Account <b>{phone}</b> connected successfully!\n"
        f"Total accounts: {len(user_clients)}",
        reply_markup=main_keyboard(),
    )
    await state.clear()

@router.message(AddAccount.waiting_password)
@admin_only
async def process_password(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data["phone"]
    client: TelegramClient = data["client"]
    password = message.text.strip()

    try:
        await client.sign_in(password=password)
    except Exception as exc:
        await message.reply(f"❌ 2FA failed: {exc}")
        await client.disconnect()
        await state.clear()
        return

    user_clients[phone] = client
    await message.answer(
        f"✅ Account <b>{phone}</b> connected (2FA passed)!\n"
        f"Total accounts: {len(user_clients)}",
        reply_markup=main_keyboard(),
    )
    await state.clear()

# ────────────────── CALLBACK: SET INTERVAL ─────────────────
@router.callback_query(F.data == "set_interval")
@admin_only
async def cb_set_interval(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "⏱ Send the interval in **minutes** (e.g. `60` for every hour)."
    )
    await state.set_state(SetInterval.waiting_minutes)
    await callback.answer()

@router.message(SetInterval.waiting_minutes)
@admin_only
async def process_interval(message: Message, state: FSMContext):
    global broadcast_interval, scheduler_task
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await message.reply("❌ Please send a positive integer.")
        return

    broadcast_interval = minutes
    if scheduler_task:
        scheduler_task.cancel()
    scheduler_task = asyncio.create_task(scheduler_loop())
    await message.answer(
        f"✅ Broadcast interval set to **{minutes} minute(s)**.",
        reply_markup=main_keyboard(),
    )
    await state.clear()

# ────────────────── CALLBACK: FORCE SEND NOW ───────────────
@router.callback_query(F.data == "force_send")
@admin_only
async def cb_force_send(callback: CallbackQuery):
    if not ad_data:
        await callback.answer("⚠️ No ad set yet. Use “Set Ad” first.", show_alert=True)
        return
    if not user_clients:
        await callback.answer("⚠️ No accounts connected.", show_alert=True)
        return

    await callback.message.answer("🚀 Starting force broadcast…")
    await callback.answer()
    # Run in background so the bot remains responsive
    asyncio.create_task(broadcast_ad())
    await callback.message.answer("✅ Broadcast task started. Check logs for progress.")

# ────────────────── CALLBACK: SET AD ───────────────────────
@router.callback_query(F.data == "set_ad")
@admin_only
async def cb_set_ad(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📝 Send the ad you want to broadcast.\n"
        "It can be plain text with links, a photo, video, or document with caption."
    )
    await state.set_state(SetAd.waiting_ad)
    await callback.answer()

@router.message(SetAd.waiting_ad)
@admin_only
async def process_ad(message: Message, state: FSMContext):
    global ad_data

    # Text‑only ad
    if message.text and not message.photo and not message.video and not message.document:
        ad_data = {
            "type": "text",
            "text": message.text,
            "entities": message.entities,
        }
    # Media ad
    elif message.photo or message.video or message.document:
        if message.photo:
            ad_data = {
                "type": "photo",
                "file": message.photo[-1].file_id,
                "caption": message.caption,
                "caption_entities": message.caption_entities,
            }
        elif message.video:
            ad_data = {
                "type": "video",
                "file": message.video.file_id,
                "caption": message.caption,
                "caption_entities": message.caption_entities,
            }
        elif message.document:
            ad_data = {
                "type": "document",
                "file": message.document.file_id,
                "caption": message.caption,
                "caption_entities": message.caption_entities,
            }
    else:
        await message.reply("❌ Unsupported ad format. Send text, photo, video, or document.")
        return

    await message.answer(
        "✅ Ad stored successfully. It will be used for every broadcast.",
        reply_markup=main_keyboard(),
    )
    await state.clear()

# ─────────────────────── MAIN ──────────────────────────────
async def main():
    os.makedirs("sessions", exist_ok=True)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot started. Admins: %s", ADMIN_IDS)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
