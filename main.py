import asyncio
import random
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ================== LOAD ENV ==================
load_dotenv()

API_ID_RAW = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
PAYMENT_BOT = os.getenv("PAYMENT_BOT", "WarungLENDIR_Robot")
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "warung_lendir_bot")

if not API_ID_RAW:
    raise ValueError("API_ID di file .env belum diisi")

if not API_HASH:
    raise ValueError("API_HASH di file .env belum diisi")

if not SESSION_STRING:
    raise ValueError("SESSION_STRING di file .env belum diisi")

if not MONGO_URI:
    raise ValueError("MONGO_URI di file .env belum diisi")

API_ID = int(API_ID_RAW.strip())
API_HASH = API_HASH.strip()
SESSION_STRING = SESSION_STRING.strip()
PAYMENT_BOT = PAYMENT_BOT.strip()

# ================== CLIENT ==================
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# ================== MONGODB ==================
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]

users_col = db["users"]
settings_col = db["settings"]
history_col = db["history"]
pending_col = db["pending_confirmations"]

# ================== FILES ==================
HARGA_IMAGE_FILE = "harga_list.jpg"

# ================== STATE MANAGEMENT ==================
user_states = {}
user_last_event = {}

# ================== DELAY ==================
DELAYS = {
    "START": 3.0,
    "MENU_AWAL": 1.2,
    "PAKET": 1.0,
    "GABUNG": 3.0,
    "WAIT_QRIS": 5.0
}

# ================== PAKET MAPPING ==================
PAKET_MAPPING = {
    "VVIP SUPER INDO": ["vvip super indo", "super indo"],
    "HIJAB PREMIUM": ["hijab premium", "hijab"],
    "ASIAN DAIRY": ["asian dairy", "dairy"],
    "INDO PREMIUM": ["indo premium"],
    "CAMPURAN PREMIUM": ["campuran premium", "campuran", "campur"],
    "ASIAN PREMIUM": ["asian premium", "asian"],
    "LIVE RECORD": ["live record", "live"],
    "BARATT": ["baratt", "barat"],
    "ONLY FANS": ["only fans", "onlyfans"],
    "SMP / SMA PREMIUM": ["smp", "sma"],
    "VVIP SUPER MALAY": ["vvip super malay", "super malay", "malay"],
    "Payment": ["payment"]
}


# ================== SETTINGS ==================
def default_settings():
    return {
        "kurs": 3500,
        "bayar_text": "✅ QRIS sudah dikirim ya {mention}\nSilakan lanjut pembayaran.",
        "verif_text": "✅ Bukti pembayaran sudah kami terima, mohon tunggu verifikasi ya {mention}",
        "thanks_text": "✅ Terima kasih {mention}, pembayaran berhasil diproses.",
        "text_harga": "📂 **LIST HARGA TERBARU**\nSilakan cek list di bawah ya kak."
    }


async def get_settings():
    data = await settings_col.find_one({"_id": "main"})
    if not data:
        defaults = default_settings()
        await settings_col.update_one(
            {"_id": "main"},
            {"$set": defaults},
            upsert=True
        )
        return defaults

    result = default_settings()
    for key, value in data.items():
        if key != "_id":
            result[key] = value
    return result


async def save_settings(settings_data):
    await settings_col.update_one(
        {"_id": "main"},
        {"$set": settings_data},
        upsert=True
    )


# ================== USERS ==================
async def get_user(user_id: int):
    return await users_col.find_one({"_id": str(user_id)})


async def upsert_user(user_id: int, user_data: dict):
    await users_col.update_one(
        {"_id": str(user_id)},
        {"$set": user_data},
        upsert=True
    )


async def count_users():
    return await users_col.count_documents({})


async def add_user_to_db(event):
    if not event.is_private:
        return

    try:
        sender = await event.get_sender()
    except Exception:
        sender = None

    if not sender:
        return

    if getattr(sender, "bot", False):
        return

    username = getattr(sender, "username", None)
    if (username or "").lower() == PAYMENT_BOT.lower():
        return

    user_id = str(event.sender_id)
    old_data = await users_col.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    payload = {
        "_id": user_id,
        "id": event.sender_id,
        "username": username,
        "first_name": getattr(sender, "first_name", None),
        "last_name": getattr(sender, "last_name", None),
        "first_seen": old_data.get("first_seen", now) if old_data else now,
        "last_seen": now,
        "auto_harga_sent": old_data.get("auto_harga_sent", False) if old_data else False
    }
    await users_col.update_one({"_id": user_id}, {"$set": payload}, upsert=True)


# ================== HISTORY ==================
async def save_history(user_id, username, paket_list):
    await history_col.insert_one({
        "timestamp": datetime.now(timezone.utc),
        "user_id": user_id,
        "username": username or "Unknown",
        "paket": paket_list,
        "jumlah": len(paket_list)
    })


async def get_last_history(limit=10):
    cursor = history_col.find({}).sort("timestamp", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def count_history():
    return await history_col.count_documents({})


async def clear_history():
    await history_col.delete_many({})


# ================== PENDING CONFIRMATIONS ==================
async def create_pending_confirmation(user_id: int, paket_list: list[str], hours: int = 5):
    now = datetime.now(timezone.utc)
    expired_at = now + timedelta(hours=hours)

    await pending_col.update_one(
        {"_id": str(user_id)},
        {
            "$set": {
                "user_id": user_id,
                "pakets": paket_list,
                "created_at": now,
                "expired_at": expired_at
            }
        },
        upsert=True
    )


async def get_pending_confirmation(user_id: int):
    return await pending_col.find_one({"_id": str(user_id)})


async def delete_pending_confirmation(user_id: int):
    await pending_col.delete_one({"_id": str(user_id)})


async def count_pending_confirmations():
    return await pending_col.count_documents({})


# ================== UTIL ==================
def match_kata(text, keyword):
    return re.search(rf"\b{re.escape(keyword.lower())}\b", text.lower()) is not None


def normalize_text(text):
    return (text or "").strip().lower()


async def render_template(text, event):
    try:
        sender = await event.get_sender()
    except Exception:
        sender = None

    user_id = str(event.sender_id or "")
    first_name = getattr(sender, "first_name", None) or "Kak"
    mention = f"[{first_name}](tg://user?id={user_id})"

    return (
        text.replace("{mention}", mention)
        .replace("{name}", first_name)
        .replace("{id}", user_id)
    )


def parse_requested_packages(raw_input):
    requested_items = [item.strip() for item in raw_input.split(",") if item.strip()]
    selected_pakets = []

    for requested in requested_items:
        matched = None

        for package_name, keywords in PAKET_MAPPING.items():
            if requested == package_name.lower() or any(requested == key.lower() for key in keywords):
                matched = package_name
                break

        if not matched:
            for package_name, keywords in PAKET_MAPPING.items():
                if any(match_kata(requested, key) for key in keywords):
                    matched = package_name
                    break

        if not matched:
            return None, requested

        if matched not in selected_pakets:
            selected_pakets.append(matched)

    return selected_pakets, None


# ================== MULAI FLOW PAYMENT ==================
async def mulai_flow_payment():
    try:
        print("🚀 Mengirim /start ke bot payment...")
        await client.send_message(PAYMENT_BOT, "/start")
        await asyncio.sleep(DELAYS["START"])
        return True
    except Exception as error:
        print(f"Error kirim /start ke payment bot: {error}")
        return False


# ================== KLIK TOMBOL ==================
async def klik_tombol(chat, teks):
    print(f"🔍 Mencari tombol: '{teks}'")
    for attempt in range(1, 20):
        try:
            async for msg in client.iter_messages(chat, limit=40):
                if not msg.buttons:
                    continue

                for row_index, row in enumerate(msg.buttons):
                    for col_index, button in enumerate(row):
                        button_text = (button.text or "").strip().lower()
                        print(f"DEBUG BUTTON: {button.text}")
                        if teks.lower() in button_text:
                            await asyncio.sleep(random.uniform(0.6, 1.2))
                            print(f"✅ KLIK → {button.text} (Attempt {attempt})")
                            await msg.click(row_index, col_index)
                            await asyncio.sleep(2.2)
                            return True

            await asyncio.sleep(1.8)
        except Exception as error:
            print(f"Error klik '{teks}': {error}")
            await asyncio.sleep(1.6)

    print(f"❌ GAGAL menemukan tombol: '{teks}'")
    return False


# ================== SMART QRIS FORWARD ==================
async def smart_forward_qris(event, user_id):
    settings_data = await get_settings()

    print("⏳ Menunggu QRIS muncul...")
    await asyncio.sleep(DELAYS["WAIT_QRIS"])

    for _ in range(10):
        try:
            messages = await client.get_messages(PAYMENT_BOT, limit=30)
            for msg in messages:
                text_lower = normalize_text(msg.text)
                if msg.photo or any(keyword in text_lower for keyword in ["qr", "scan", "qris", "bayar", "pembayaran", "silakan scan"]):
                    await msg.forward_to(event.chat_id)

                    bayar_text = await render_template(
                        settings_data.get("bayar_text", default_settings()["bayar_text"]),
                        event
                    )
                    await event.reply(bayar_text)

                    user_states[user_id] = "waiting_payment"
                    user_last_event[user_id] = event
                    print("✅ QRIS berhasil diforward!")
                    return True
        except Exception as error:
            print(f"Error cek QRIS: {error}")

        await asyncio.sleep(2.5)

    await event.reply("✅ Proses selesai. Silakan cek QRIS di bot pembayaran.")
    return False


# ================== FORWARD BUKTI & LINK ==================
async def forward_bukti_transfer(event):
    settings_data = await get_settings()

    user_id = event.sender_id
    if user_states.get(user_id) != "waiting_payment":
        return

    try:
        await event.forward_to(PAYMENT_BOT)
        verif_text = await render_template(
            settings_data.get("verif_text", default_settings()["verif_text"]),
            event
        )
        await event.reply(verif_text)
    except Exception as error:
        print(f"Error forward bukti: {error}")


async def forward_link_join(event):
    settings_data = await get_settings()

    text = event.message.text or ""
    text_lower = text.lower()

    if "join payment" not in text_lower and "t.me/" not in text_lower:
        return

    for user_id in list(user_states.keys()):
        if user_states.get(user_id) == "waiting_payment" and user_id in user_last_event:
            try:
                target_event = user_last_event[user_id]
                await event.forward_to(target_event.chat_id)

                thanks_text = await render_template(
                    settings_data.get("thanks_text", default_settings()["thanks_text"]),
                    target_event
                )
                await target_event.reply(thanks_text)

                user_states.pop(user_id, None)
                user_last_event.pop(user_id, None)
                return
            except Exception as error:
                print(f"Error forward link join: {error}")


# ================== PROSES ORDER ==================
async def proses_order_otomatis(event, sender, sender_id, selected_pakets):
    total_selected = len(selected_pakets)
    is_paket_hemat = total_selected >= 2
    menu_awal = "Paket Hemat" if is_paket_hemat else "VIP SATUAN"

    print("DEBUG MENU AWAL:", menu_awal)

    if is_paket_hemat:
        await event.reply(f"⚡ Memproses **{total_selected} paket** via **Paket Hemat**...")
    else:
        await event.reply(f"⚡ Memproses **{selected_pakets[0]}** via **VIP Satuan**...")

    try:
        started = await mulai_flow_payment()
        if not started:
            await event.reply("❌ Gagal memulai bot payment.")
            return

        menu_found = await klik_tombol(PAYMENT_BOT, menu_awal)
        if not menu_found:
            await event.reply(f"❌ Menu '{menu_awal}' tidak ditemukan di bot payment.")
            return

        await asyncio.sleep(DELAYS["MENU_AWAL"])

        for paket in selected_pakets:
            print("DEBUG: klik paket", paket)
            paket_found = await klik_tombol(PAYMENT_BOT, paket)
            if not paket_found:
                await event.reply(f"❌ Paket '{paket}' tidak ditemukan.")
                return
            await asyncio.sleep(DELAYS["PAKET"])

        print("DEBUG: klik tombol Gabung Sekarang")
        gabung_found = await klik_tombol(PAYMENT_BOT, "Gabung Sekarang")
        if not gabung_found:
            await event.reply("❌ Tombol 'Gabung Sekarang' tidak ditemukan.")
            return

        await asyncio.sleep(DELAYS["GABUNG"])
        await smart_forward_qris(event, sender_id)

        username = getattr(sender, "username", None) or "Unknown"
        await save_history(sender_id, username, selected_pakets)

    except Exception as error:
        print(f"❌ Error proses: {error}")
        await event.reply("❌ Terjadi error saat memproses pesanan.")


# ================== AUTO TIMEOUT KONFIRMASI ==================
async def check_expired_orders():
    while True:
        try:
            now = datetime.now(timezone.utc)

            cursor = pending_col.find({})
            all_pending = await cursor.to_list(length=None)

            for data in all_pending:
                user_id = data.get("user_id")
                expired_at = data.get("expired_at")

                if not user_id or not expired_at:
                    continue

                if now >= expired_at:
                    try:
                        await client.send_message(
                            int(user_id),
                            "❌ Konfirmasi pesanan sudah kadaluarsa karena tidak ada respon dalam 5 jam.\nSilakan order ulang ya kak."
                        )
                    except Exception as error:
                        print(f"Error kirim expired ke {user_id}: {error}")

                    await delete_pending_confirmation(int(user_id))

        except Exception as error:
            print(f"Error check_expired_orders: {error}")

        await asyncio.sleep(60)


# ================== COMMAND HELP ==================
@client.on(events.NewMessage(pattern=r"(?i)^[./]help$"))
async def help_handler(event):
    if not event.out:
        return

    await event.reply(
        "📂 **ASISTEN PREMIUM V-STABLE (MONSTER)**\n\n"
        "• `.setkurs [angka]` - Atur kurs MYR\n"
        "• `.setbayar [teks]` - Balasan saat mau bayar\n"
        "• `.setverif [teks]` - Balasan saat kirim bukti\n"
        "• `.setthanks [teks]` - Balasan saat sukses\n"
        "• `.settextharga [teks]` - Atur caption list\n"
        "• `.setharga` - Reply foto untuk update list\n"
        "• `.broadcast` - Reply pesan untuk kirim ke semua user\n"
        "• `.stats` - Cek jumlah database user\n"
        "• `.history` - Cek 10 riwayat terakhir\n"
        "• `.cleardb` - Kosongkan riwayat\n"
        "• `.order [nama paket]` - Fallback manual order\n\n"
        "**Keyword customer:** `harga`\n"
        "**Konfirmasi order customer:** `ya` / `tidak`\n"
        "**Timeout konfirmasi:** 5 jam\n"
        "**Variabel:** `{mention}`, `{name}`, `{id}`"
    )


# ================== COMMAND SETTINGS ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]setkurs\s+(\d+)$"))
async def setkurs_handler(event):
    if not event.out:
        return

    settings_data = await get_settings()
    settings_data["kurs"] = int(event.pattern_match.group(1))
    await save_settings(settings_data)
    await event.reply(f"✅ Kurs MYR berhasil diatur ke: **{settings_data['kurs']}**")


@client.on(events.NewMessage(pattern=r"(?is)^[.]setbayar\s+(.+)$"))
async def setbayar_handler(event):
    if not event.out:
        return

    settings_data = await get_settings()
    settings_data["bayar_text"] = event.pattern_match.group(1).strip()
    await save_settings(settings_data)
    await event.reply("✅ Teks bayar berhasil diupdate.")


@client.on(events.NewMessage(pattern=r"(?is)^[.]setverif\s+(.+)$"))
async def setverif_handler(event):
    if not event.out:
        return

    settings_data = await get_settings()
    settings_data["verif_text"] = event.pattern_match.group(1).strip()
    await save_settings(settings_data)
    await event.reply("✅ Teks verif berhasil diupdate.")


@client.on(events.NewMessage(pattern=r"(?is)^[.]setthanks\s+(.+)$"))
async def setthanks_handler(event):
    if not event.out:
        return

    settings_data = await get_settings()
    settings_data["thanks_text"] = event.pattern_match.group(1).strip()
    await save_settings(settings_data)
    await event.reply("✅ Teks thanks berhasil diupdate.")


@client.on(events.NewMessage(pattern=r"(?is)^[.]settextharga\s+(.+)$"))
async def settextharga_handler(event):
    if not event.out:
        return

    settings_data = await get_settings()
    settings_data["text_harga"] = event.pattern_match.group(1).strip()
    await save_settings(settings_data)
    await event.reply("✅ Caption list harga berhasil diupdate.")


@client.on(events.NewMessage(pattern=r"(?i)^[.]setharga$"))
async def setharga_handler(event):
    if not event.out:
        return

    if not event.is_reply:
        await event.reply("❌ Reply ke foto list harga dengan command `.setharga`")
        return

    replied_message = await event.get_reply_message()
    if not replied_message or not replied_message.photo:
        await event.reply("❌ Reply harus ke foto.")
        return

    try:
        await replied_message.download_media(file=HARGA_IMAGE_FILE)
        await event.reply("✅ Foto list harga berhasil disimpan.")
    except Exception as error:
        print(f"Error simpan foto harga: {error}")
        await event.reply("❌ Gagal menyimpan foto list harga.")


# ================== COMMAND STATS ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]stats$"))
async def stats_handler(event):
    if not event.out:
        return

    total_users = await count_users()
    total_waiting = sum(1 for state in user_states.values() if state == "waiting_payment")
    total_history = await count_history()
    total_pending = await count_pending_confirmations()

    settings_data = await get_settings()

    await event.reply(
        f"📊 **STATISTIK BOT**\n\n"
        f"• Total user database: **{total_users}**\n"
        f"• User waiting payment: **{total_waiting}**\n"
        f"• Pending konfirmasi order: **{total_pending}**\n"
        f"• Total history tersimpan: **{total_history}**\n"
        f"• Kurs MYR: **{settings_data.get('kurs', 0)}**"
    )


# ================== COMMAND HISTORY ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]history$"))
async def history_handler(event):
    if not event.out:
        return

    history = await get_last_history(10)
    if not history:
        await event.reply("📊 Belum ada riwayat.")
        return

    message_text = "📊 **Riwayat 10 Pesanan Terakhir:**\n\n"
    for item in history:
        ts = item.get("timestamp")
        ts_text = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else str(ts)
        message_text += f"• {ts_text} | @{item.get('username', '?')} → {item.get('jumlah', 0)} paket\n"

    await event.reply(message_text)


# ================== COMMAND CLEAR DB ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]cleardb$"))
async def cleardb_handler(event):
    if not event.out:
        return

    await clear_history()
    await event.reply("✅ Database riwayat berhasil dibersihkan.")


# ================== COMMAND BROADCAST ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]broadcast$"))
async def broadcast_handler(event):
    if not event.out:
        return

    if not event.is_reply:
        await event.reply("❌ Reply pesan yang ingin dibroadcast dengan command `.broadcast`")
        return

    replied_message = await event.get_reply_message()
    success_count = 0
    failed_count = 0

    await event.reply("📢 Broadcast dimulai...")

    cursor = users_col.find({})
    all_users = await cursor.to_list(length=None)

    for user in all_users:
        try:
            target_id = int(user["id"])

            if replied_message.media:
                await client.send_file(
                    target_id,
                    replied_message.media,
                    caption=replied_message.text or ""
                )
            else:
                await client.send_message(target_id, replied_message.text or "")

            success_count += 1
            await asyncio.sleep(1.2)
        except Exception:
            failed_count += 1

    await event.reply(
        f"✅ Broadcast selesai.\n\n"
        f"• Berhasil: **{success_count}**\n"
        f"• Gagal: **{failed_count}**"
    )


# ================== COMMAND ORDER MANUAL FALLBACK ==================
@client.on(events.NewMessage(pattern=r"(?is)^[.]order\s+(.+)$"))
async def manual_order_handler(event):
    if not event.out:
        return

    raw_input = event.pattern_match.group(1).strip().lower()
    selected_pakets, unknown_item = parse_requested_packages(raw_input)

    if unknown_item:
        await event.reply(f"❌ Paket tidak dikenali: `{unknown_item}`")
        return

    if not selected_pakets:
        await event.reply("❌ Format salah.\nContoh: `.order indo premium` atau `.order asian premium, campuran premium`")
        return

    try:
        sender = await event.get_sender()
    except Exception:
        sender = None

    await proses_order_otomatis(event, sender, event.sender_id, selected_pakets)


# ================== FIRST CHAT AUTO KIRIM HARGA ==================
@client.on(events.NewMessage(incoming=True, func=lambda event: event.is_private))
async def first_chat_send_harga_handler(event):
    settings_data = await get_settings()

    try:
        sender = await event.get_sender()
    except Exception:
        sender = None

    if not sender:
        return

    if getattr(sender, "bot", False):
        return

    sender_username = (getattr(sender, "username", "") or "").lower()
    if sender_username == PAYMENT_BOT.lower():
        return

    user_id = str(event.sender_id)
    now = datetime.now(timezone.utc)
    old_data = await get_user(event.sender_id)

    payload = {
        "_id": user_id,
        "id": event.sender_id,
        "username": getattr(sender, "username", None),
        "first_name": getattr(sender, "first_name", None),
        "last_name": getattr(sender, "last_name", None),
        "first_seen": old_data.get("first_seen", now) if old_data else now,
        "last_seen": now,
        "auto_harga_sent": old_data.get("auto_harga_sent", False) if old_data else False
    }
    await upsert_user(event.sender_id, payload)

    if old_data and old_data.get("auto_harga_sent", False):
        return

    caption_text = await render_template(
        settings_data.get("text_harga", default_settings()["text_harga"]),
        event
    )

    try:
        if os.path.exists(HARGA_IMAGE_FILE):
            await client.send_file(event.chat_id, HARGA_IMAGE_FILE, caption=caption_text)
        else:
            await event.reply(caption_text)

        payload["auto_harga_sent"] = True
        payload["last_seen"] = now
        await upsert_user(event.sender_id, payload)
        print(f"✅ Auto harga terkirim sekali ke user {user_id}")

    except Exception as error:
        print(f"Error kirim harga first chat: {error}")


# ================== PRIVATE PHOTO HANDLER ==================
@client.on(events.NewMessage(incoming=True, func=lambda event: event.is_private and bool(event.photo)))
async def photo_handler(event):
    await add_user_to_db(event)

    try:
        sender = await event.get_sender()
        sender_username = (getattr(sender, "username", "") or "").lower()
    except Exception:
        sender_username = ""

    if sender_username == PAYMENT_BOT.lower():
        return

    await forward_bukti_transfer(event)


# ================== PRIVATE MESSAGE HANDLER ==================
@client.on(events.NewMessage(incoming=True, func=lambda event: event.is_private and not bool(event.photo)))
async def private_message_handler(event):
    settings_data = await get_settings()

    await add_user_to_db(event)

    try:
        sender = await event.get_sender()
        sender_username = (getattr(sender, "username", "") or "").lower()
    except Exception:
        sender = None
        sender_username = ""

    if sender_username == PAYMENT_BOT.lower():
        await forward_link_join(event)
        return

    text = normalize_text(event.message.text)
    sender_id = event.sender_id

    if not text:
        return

    # ================== KEYWORD MANUAL HARGA ==================
    if text == "harga":
        caption_text = await render_template(
            settings_data.get("text_harga", default_settings()["text_harga"]),
            event
        )

        if os.path.exists(HARGA_IMAGE_FILE):
            try:
                await client.send_file(event.chat_id, HARGA_IMAGE_FILE, caption=caption_text)
                return
            except Exception as error:
                print(f"Error kirim foto harga: {error}")

        await event.reply(caption_text)
        return

    # ================== KONFIRMASI YA / TIDAK ==================
    pending = await get_pending_confirmation(sender_id)
    if pending:
        if text == "ya":
            selected_pakets = pending.get("pakets", [])
            await delete_pending_confirmation(sender_id)
            await event.reply("✅ Oke, pesanan sedang diproses...")
            await proses_order_otomatis(event, sender, sender_id, selected_pakets)
            return

        if text == "tidak":
            await delete_pending_confirmation(sender_id)
            await event.reply("❌ Oke, pesanan dibatalkan.")
            return

    # cegah command owner masuk ke flow order
    if text.startswith(".") or text.startswith("/"):
        return

    print("DEBUG TEXT:", text)

    selected_pakets = [
        package_name
        for package_name, keywords in PAKET_MAPPING.items()
        if any(match_kata(text, keyword) for keyword in keywords)
    ]

    print("DEBUG SELECTED PAKETS:", selected_pakets)

    if not selected_pakets:
        print("DEBUG: tidak ada paket yang cocok")
        return

    await create_pending_confirmation(sender_id, selected_pakets, hours=5)

    nama_paket = ", ".join(selected_pakets)
    await event.reply(
        f"Apakah benar ingin membeli:\n**{nama_paket}**\n\n"
        f"Balas: **ya** atau **tidak**\n"
        f"Konfirmasi berlaku selama **5 jam**."
    )


# ================== AUTO RECONNECT ==================
async def main():
    timeout_task = None

    while True:
        try:
            print("🚀 Menghubungkan ke Telegram...")
            await client.start()
            print("✅ Bot berhasil terhubung ke Telegram!")

            if timeout_task is None or timeout_task.done():
                timeout_task = asyncio.create_task(check_expired_orders())

            await client.run_until_disconnected()

        except KeyboardInterrupt:
            print("\n🛑 Bot dihentikan manual.")
            break

        except Exception as error:
            print(f"❌ Koneksi terputus: {type(error).__name__} - {error}")
            print("🔄 Mencoba reconnect dalam 8 detik...")
            await asyncio.sleep(8)


if __name__ == "__main__":
    print("🚀 WARUNG LENDIR ASSISTANT sedang berjalan...")
    asyncio.run(main())
