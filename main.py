import asyncio
import os
import random
import re
from datetime import datetime, timedelta

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

# default 300 menit = 5 jam
CONFIRM_TIMEOUT_MINUTES = int(os.getenv("CONFIRM_TIMEOUT_MINUTES", "300"))

# timeout tunggu QRIS
WAIT_QRIS_MAX_ATTEMPTS = int(os.getenv("WAIT_QRIS_MAX_ATTEMPTS", "15"))
WAIT_QRIS_INTERVAL_SECONDS = float(os.getenv("WAIT_QRIS_INTERVAL_SECONDS", "3"))
WAIT_QRIS_INITIAL_DELAY_SECONDS = float(os.getenv("WAIT_QRIS_INITIAL_DELAY_SECONDS", "3"))

if not API_ID_RAW:
    raise ValueError("API_ID belum diisi di environment")

if not API_HASH:
    raise ValueError("API_HASH belum diisi di environment")

if not SESSION_STRING:
    raise ValueError("SESSION_STRING belum diisi di environment")

if not MONGO_URI:
    raise ValueError("MONGO_URI belum diisi di environment")

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
payments_col = db["payments"]

# ================== FILES ==================
HARGA_IMAGE_FILE = "harga_list.jpg"

# ================== RUNTIME STATE ==================
user_states = {}
user_last_event = {}
first_chat_skip_messages = set()

# queue checkout
order_queue = asyncio.Queue()
queued_user_ids = set()
current_checkout_user_id = None

# ================== DELAY ==================
DELAYS = {
    "START": 1.5,
    "MENU_AWAL": 0.7,
    "PAKET": 0.6,
    "GABUNG": 1.6,
    "POST_CLICK_BUFFER": 0.8
}

# ================== PAKET MAPPING ==================
PAKET_MAPPING = {
    "VVIP SUPER INDO": ["vvip super indo", "super indo"],
    "VVIP SUPER MALAY": ["vvip super malay", "super malay", "malay"],
    "HIJAB PREMIUM": ["hijab premium", "hijab"],
    "INDO PREMIUM": ["indo premium"],
    "ASIAN DAIRY": ["asian dairy", "dairy"],
    "CAMPURAN PREMIUM": ["campuran premium", "campuran", "campur"],
    "ASIAN PREMIUM": ["asian premium", "asian"],
    "LIVE RECORD": ["live record", "live"],
    "BARATT": ["baratt", "barat"],
    "ONLY FANS": ["only fans", "onlyfans"],
    "SMP / SMA PREMIUM": ["smp / sma premium", "smp", "sma"],
    "Payment": ["payment"]
}

# ================== HARGA PAKET ==================
PAKET_PRICES = {
    "VVIP SUPER INDO": 150000,
    "VVIP SUPER MALAY": 100000,
    "HIJAB PREMIUM": 60000,
    "INDO PREMIUM": 50000,
    "ASIAN DAIRY": 45000,
    "CAMPURAN PREMIUM": 50000,
    "ASIAN PREMIUM": 60000,
    "LIVE RECORD": 50000,
    "BARATT": 45000,
    "ONLY FANS": 50000,
    "SMP / SMA PREMIUM": 60000,
    "Payment": 100000
}

# ================== CONFIRMATION WORDS ==================
CONFIRM_YES_WORDS = {
    "ya", "y", "iya", "iy", "ok", "oke", "lanjut", "jadi", "jadi ya", "gas", "yes"
}

CONFIRM_NO_WORDS = {
    "tidak", "tak", "gak", "ga", "enggak", "nggak", "batal",
    "gajadi", "ga jadi", "gak jadi", "tak jadi", "cancel", "no"
}

# ================== UTIL ==================
def now_utc():
    return datetime.utcnow()


def normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def match_kata(text: str, keyword: str) -> bool:
    return re.search(rf"\b{re.escape(keyword.lower())}\b", text.lower()) is not None


def format_rupiah(nominal: int) -> str:
    return f"Rp{nominal:,}".replace(",", ".")


def format_timeout_text(total_minutes: int) -> str:
    if total_minutes % 60 == 0:
        hours = total_minutes // 60
        return f"{hours} jam"
    return f"{total_minutes} menit"


def hitung_total_harga_idr(selected_pakets: list[str]) -> int:
    total = 0
    for paket in selected_pakets:
        total += PAKET_PRICES.get(paket, 0)
    return total


def parse_requested_packages(raw_input: str):
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


def extract_payment_id(text: str) -> str | None:
    if not text:
        return None

    patterns = [
        r"id\s*pembayaran\s*[:\-]?\s*([A-Za-z0-9_\-]+)",
        r"id\s*payment\s*[:\-]?\s*([A-Za-z0-9_\-]+)",
        r"payment\s*id\s*[:\-]?\s*([A-Za-z0-9_\-]+)",
        r"invoice\s*id\s*[:\-]?\s*([A-Za-z0-9_\-]+)",
        r"trx\s*id\s*[:\-]?\s*([A-Za-z0-9_\-]+)",
        r"transaction\s*id\s*[:\-]?\s*([A-Za-z0-9_\-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


def extract_payment_amount(text: str) -> str | None:
    if not text:
        return None

    patterns = [
        r"harga\s*[:\-]?\s*(.+)",
        r"amount\s*[:\-]?\s*(.+)",
        r"nominal\s*[:\-]?\s*(.+)",
        r"total\s*bayar\s*[:\-]?\s*(.+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


def extract_payment_expiry(text: str) -> str | None:
    if not text:
        return None

    patterns = [
        r"berlaku\s*sampai\s*[:\-]?\s*(.+)",
        r"expired\s*[:\-]?\s*(.+)",
        r"expire\s*[:\-]?\s*(.+)",
        r"kadaluarsa\s*[:\-]?\s*(.+)",
        r"valid\s*sampai\s*[:\-]?\s*(.+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


def is_join_message(text: str) -> bool:
    text_lower = normalize_text(text)

    keywords = [
        "join payment",
        "please click the following link",
        "following link",
        "berikut link",
        "link join",
        "akses channel",
        "silakan join",
        "silahkan join",
        "https://t.me/",
        "http://t.me/",
        "t.me/+",
        "t.me/"
    ]

    return any(keyword in text_lower for keyword in keywords)


def is_qris_message(msg) -> bool:
    text = msg.text or ""
    text_lower = normalize_text(text)

    text_keywords = [
        "qris",
        "qr",
        "scan",
        "scan qr",
        "scan qris",
        "bayar",
        "pembayaran",
        "id pembayaran",
        "id payment",
        "payment id",
        "harga",
        "amount",
        "nominal",
        "berlaku sampai",
        "expired",
    ]

    if extract_payment_id(text):
        return True

    if any(keyword in text_lower for keyword in text_keywords):
        return True

    if msg.photo and any(keyword in text_lower for keyword in ["qris", "qr", "scan", "bayar", "pembayaran"]):
        return True

    return False


def is_affirmative(text: str) -> bool:
    return normalize_text(text) in CONFIRM_YES_WORDS


def is_negative(text: str) -> bool:
    return normalize_text(text) in CONFIRM_NO_WORDS


async def render_template(text: str, event) -> str:
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


def get_order_state(user_id: int) -> dict:
    return user_states.get(user_id, {})


def set_order_state(user_id: int, new_state: dict):
    user_states[user_id] = new_state


def clear_order_state(user_id: int):
    user_states.pop(user_id, None)
    user_last_event.pop(user_id, None)


# ================== SETTINGS ==================
def default_settings():
    return {
        "kurs": 3500,
        "bayar_text": "✅ QRIS sudah dikirim ya {mention}\nSilakan lanjut pembayaran.",
        "verif_text": "✅ Bukti pembayaran sudah kami terima, mohon tunggu verifikasi ya {mention}",
        "thanks_text": "✅ Terima kasih {mention}, pembayaran berhasil diproses.",
        "text_harga": (
            "Halo! {id} Selamat Datang ya\n"
            "Iya kak, mau join VIP ya? 👋\n\n"
            "Pesan ini adalah SISTEM OTOMATIS. Mohon ikuti langkah di bawah ini agar orderan Kakak bisa langsung diproses\n\n"
            "● VVIP SUPER INDO ➔ 150K IDR / RM42\n"
            "● VVIP SUPER MALAY ➔ 100K IDR / RM28\n"
            "● HIJAB PREMIUM ➔ 60K IDR / RM17\n"
            "● INDO PREMIUM ➔ 50K IDR / RM14\n"
            "● ASIAN DAIRY ➔ 45K IDR / RM12\n"
            "● CAMPURAN PREMIUM ➔ 50K IDR / RM14\n"
            "● ASIAN PREMIUM ➔ 60K IDR / RM17\n"
            "● LIVE RECORD ➔ 50K IDR / RM14\n"
            "● BARATT ➔ 45K IDR / RM12\n"
            "● ONLY FANS ➔ 50K IDR / RM14\n"
            "● SMP / SMA PREMIUM ➔ 60K IDR / RM17\n\n"
            "FORMAT ORDER (Silakan Balas Sekarang):\n\n"
            "BELI 1 PAKET\n"
            "Nama Paket: (Contoh: VVIP SUPER INDO)\n\n"
            "BELI 2+ PAKET ⬇️\n"
            " VVIP SUPER INDO \n"
            " VVIP SUPER MALAY \n\n"
            "Metode Pembayaran: QRIS\n\n"
            "Admin akan segera mengirimkan detail pembayaran. Mohon segera kirim format order ya! 🚀"
        )
    }


async def get_settings():
    data = await settings_col.find_one({"_id": "main"})
    if not data:
        defaults = default_settings()
        await settings_col.update_one({"_id": "main"}, {"$set": defaults}, upsert=True)
        return defaults

    result = default_settings()
    for key, value in data.items():
        if key != "_id":
            result[key] = value
    return result


async def save_settings(settings_data):
    await settings_col.update_one({"_id": "main"}, {"$set": settings_data}, upsert=True)


# ================== USERS ==================
async def get_user(user_id: int):
    return await users_col.find_one({"_id": str(user_id)})


async def upsert_user(user_id: int, user_data: dict):
    await users_col.update_one({"_id": str(user_id)}, {"$set": user_data}, upsert=True)


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
    now = now_utc()

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
        "timestamp": now_utc(),
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
async def create_pending_confirmation(user_id: int, paket_list: list[str], message_id: int | None = None):
    now = now_utc()
    expired_at = now + timedelta(minutes=CONFIRM_TIMEOUT_MINUTES)

    await pending_col.update_one(
        {"_id": str(user_id)},
        {
            "$set": {
                "user_id": user_id,
                "pakets": paket_list,
                "created_at": now,
                "expired_at": expired_at,
                "message_id": message_id
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


# ================== PAYMENTS ==================
async def create_or_update_payment_record(
    payment_id: str,
    user_id: int,
    selected_pakets: list[str],
    total_harga_idr: int,
    amount_text: str | None = None,
    expiry_text: str | None = None,
    source_message_id: int | None = None,
):
    await payments_col.update_one(
        {"_id": payment_id},
        {
            "$set": {
                "user_id": user_id,
                "selected_pakets": selected_pakets,
                "total_harga_idr": total_harga_idr,
                "amount_text": amount_text,
                "expiry_text": expiry_text,
                "status": "waiting_payment",
                "updated_at": now_utc(),
                "source_message_id": source_message_id,
            },
            "$setOnInsert": {
                "created_at": now_utc(),
            }
        },
        upsert=True
    )


async def get_payment_record(payment_id: str):
    return await payments_col.find_one({"_id": payment_id})


async def set_payment_status(payment_id: str, status: str):
    await payments_col.update_one(
        {"_id": payment_id},
        {"$set": {"status": status, "updated_at": now_utc()}},
        upsert=False
    )


async def get_waiting_payment_user_ids():
    waiting_users = []
    for uid, state in user_states.items():
        if state.get("status") == "waiting_payment":
            waiting_users.append(uid)
    return waiting_users


# ================== PAYMENT FLOW ==================
async def mulai_flow_payment():
    try:
        print("🚀 Mengirim /start ke bot payment...")
        await client.send_message(PAYMENT_BOT, "/start")
        await asyncio.sleep(DELAYS["START"])
        return True
    except Exception as error:
        print(f"Error kirim /start ke payment bot: {error}")
        return False


async def get_latest_payment_bot_message_id():
    try:
        messages = await client.get_messages(PAYMENT_BOT, limit=1)
        if messages:
            return messages[0].id
    except Exception as error:
        print(f"Error get_latest_payment_bot_message_id: {error}")
    return 0


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
                            await asyncio.sleep(random.uniform(0.25, 0.55))
                            print(f"✅ KLIK → {button.text} (Attempt {attempt})")
                            await msg.click(row_index, col_index)
                            await asyncio.sleep(DELAYS["POST_CLICK_BUFFER"])
                            return True

            await asyncio.sleep(0.7)
        except Exception as error:
            print(f"Error klik '{teks}': {error}")
            await asyncio.sleep(0.7)

    print(f"❌ GAGAL menemukan tombol: '{teks}'")
    return False


async def build_bayar_text(event, payment_id: str | None, total_harga_idr: int) -> str:
    settings_data = await get_settings()

    bayar_text = await render_template(
        settings_data.get("bayar_text", default_settings()["bayar_text"]),
        event
    )

    kurs = settings_data.get("kurs", 0)
    if kurs > 0 and total_harga_idr > 0:
        total_harga_myr = total_harga_idr / kurs
        bayar_text += f"\n\n💱 Estimasi harga:\nRM{total_harga_myr:.2f}"

    if total_harga_idr > 0:
        bayar_text += f"\n\n💰 Total IDR:\n{format_rupiah(total_harga_idr)}"

    if payment_id:
        bayar_text += f"\n\n🧾 ID Transaksi:\n`{payment_id}`"

    return bayar_text


async def smart_forward_qris(event, user_id, min_message_id=0):
    print("⏳ Menunggu QRIS muncul...")
    await asyncio.sleep(WAIT_QRIS_INITIAL_DELAY_SECONDS)

    state = get_order_state(user_id)
    selected_pakets = state.get("selected_pakets", [])
    total_harga_idr = state.get("total_harga_idr", 0)

    seen_message_ids = set()
    seen_payment_ids = set()

    for attempt in range(1, WAIT_QRIS_MAX_ATTEMPTS + 1):
        try:
            print(f"🔁 Cek QRIS attempt {attempt}/{WAIT_QRIS_MAX_ATTEMPTS}")
            messages = await client.get_messages(PAYMENT_BOT, limit=50)

            qris_photo_msg = None
            qris_info_msg = None

            for msg in messages:
                if min_message_id and msg.id <= min_message_id:
                    continue

                if msg.id in seen_message_ids:
                    continue

                text = msg.text or ""
                text_lower = normalize_text(text)

                if is_qris_message(msg):
                    if qris_info_msg is None:
                        qris_info_msg = msg

                if msg.photo and (
                    extract_payment_id(text)
                    or any(keyword in text_lower for keyword in ["qris", "qr", "scan", "bayar", "pembayaran"])
                ):
                    qris_photo_msg = msg
                    break

            # fallback: jika info text ketemu tapi foto tidak punya caption / terpisah
            if not qris_photo_msg and qris_info_msg:
                for msg in messages:
                    if min_message_id and msg.id <= min_message_id:
                        continue
                    if msg.id in seen_message_ids:
                        continue
                    if msg.id == qris_info_msg.id:
                        continue
                    if msg.photo:
                        qris_photo_msg = msg
                        break

            if qris_photo_msg:
                qris_photo_text = qris_photo_msg.text or ""
                payment_id = extract_payment_id(qris_photo_text)
                amount_text = extract_payment_amount(qris_photo_text)
                expiry_text = extract_payment_expiry(qris_photo_text)

                if (not payment_id) and qris_info_msg:
                    info_text = qris_info_msg.text or ""
                    payment_id = extract_payment_id(info_text)
                    amount_text = amount_text or extract_payment_amount(info_text)
                    expiry_text = expiry_text or extract_payment_expiry(info_text)

                if payment_id:
                    if payment_id in seen_payment_ids:
                        print(f"⚠️ Payment ID {payment_id} sudah pernah dicek di sesi ini")
                        await asyncio.sleep(WAIT_QRIS_INTERVAL_SECONDS)
                        continue

                    existing = await get_payment_record(payment_id)
                    if existing:
                        print(f"⚠️ Payment ID {payment_id} sudah ada di database, skip QRIS lama")
                        seen_payment_ids.add(payment_id)
                        seen_message_ids.add(qris_photo_msg.id)
                        if qris_info_msg:
                            seen_message_ids.add(qris_info_msg.id)
                        await asyncio.sleep(WAIT_QRIS_INTERVAL_SECONDS)
                        continue

                await qris_photo_msg.forward_to(event.chat_id)

                if qris_info_msg and qris_info_msg.id != qris_photo_msg.id:
                    try:
                        await qris_info_msg.forward_to(event.chat_id)
                    except Exception as info_error:
                        print(f"Error forward qris info: {info_error}")

                if payment_id:
                    await create_or_update_payment_record(
                        payment_id=payment_id,
                        user_id=user_id,
                        selected_pakets=selected_pakets,
                        total_harga_idr=total_harga_idr,
                        amount_text=amount_text,
                        expiry_text=expiry_text,
                        source_message_id=qris_photo_msg.id
                    )

                bayar_text = await build_bayar_text(event, payment_id, total_harga_idr)
                await event.reply(bayar_text)

                updated_state = get_order_state(user_id)
                updated_state["status"] = "waiting_payment"
                updated_state["payment_id"] = payment_id
                updated_state["selected_pakets"] = selected_pakets
                updated_state["total_harga_idr"] = total_harga_idr
                set_order_state(user_id, updated_state)
                user_last_event[user_id] = event

                print("✅ QRIS berhasil diforward!")
                return True

        except Exception as error:
            print(f"Error cek QRIS attempt {attempt}: {error}")

        await asyncio.sleep(WAIT_QRIS_INTERVAL_SECONDS)

    await event.reply(
        "⚠️ QRIS belum berhasil kami ambil otomatis.\n"
        "Mohon tunggu sebentar atau ulangi order ya kak."
    )
    return False


async def forward_bukti_transfer(event):
    settings_data = await get_settings()

    user_id = event.sender_id
    state = get_order_state(user_id)

    if state.get("status") != "waiting_payment":
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


async def send_join_to_target_user(source_event, target_user_id: int):
    target_event = user_last_event.get(target_user_id)
    if not target_event:
        print(f"⚠️ user_last_event tidak ditemukan untuk user {target_user_id}")
        return False

    try:
        if source_event.message.media:
            await client.send_file(
                target_event.chat_id,
                source_event.message.media,
                caption=source_event.message.text or ""
            )
        else:
            await client.send_message(target_event.chat_id, source_event.message.text or "")

        settings_data = await get_settings()
        thanks_text = await render_template(
            settings_data.get("thanks_text", default_settings()["thanks_text"]),
            target_event
        )
        await target_event.reply(thanks_text)

        state = get_order_state(target_user_id)
        payment_id_state = state.get("payment_id")
        if payment_id_state:
            await set_payment_status(payment_id_state, "completed")

        clear_order_state(target_user_id)
        return True

    except Exception as error:
        print(f"Error send_join_to_target_user: {error}")
        return False


async def route_payment_bot_message(event):
    text = event.message.text or ""

    if not is_join_message(text):
        return

    print("DEBUG JOIN TEXT:", text[:300])

    payment_id = extract_payment_id(text)
    print("DEBUG JOIN PAYMENT ID:", payment_id)

    target_user_id = None

    if payment_id:
        record = await get_payment_record(payment_id)
        if record:
            target_user_id = record.get("user_id")

    if target_user_id is None:
        waiting_users = await get_waiting_payment_user_ids()
        if len(waiting_users) == 1:
            target_user_id = waiting_users[0]
        else:
            print("⚠️ Pesan join ambigu, payment_id tidak ditemukan atau lebih dari satu user waiting_payment.")
            return

    await send_join_to_target_user(event, target_user_id)


async def proses_order_otomatis_core(event, sender, sender_id, selected_pakets):
    total_selected = len(selected_pakets)
    is_paket_hemat = total_selected >= 2
    menu_awal = "Paket Hemat" if is_paket_hemat else "VIP SATUAN"
    total_harga_idr = hitung_total_harga_idr(selected_pakets)

    state = get_order_state(sender_id)
    queue_message_id = state.get("queue_message_id")

    if queue_message_id:
        try:
            await client.edit_message(
                event.chat_id,
                queue_message_id,
                "⚡ Pesanan kamu sedang diproses..."
            )
        except Exception as error:
            print(f"Error edit queue message: {error}")

    state.update({
        "status": "processing_order",
        "selected_pakets": selected_pakets,
        "total_harga_idr": total_harga_idr,
        "payment_id": None
    })
    set_order_state(sender_id, state)

    print("DEBUG MENU AWAL:", menu_awal)

    if is_paket_hemat:
        await event.reply(f"⚡ Memproses **{total_selected} paket** via **Paket Hemat**...")
    else:
        await event.reply(f"⚡ Memproses **{selected_pakets[0]}** via **VIP Satuan**...")

    try:
        last_bot_msg_id = await get_latest_payment_bot_message_id()

        started = await mulai_flow_payment()
        if not started:
            clear_order_state(sender_id)
            await event.reply("❌ Gagal memulai bot payment.")
            return

        menu_found = await klik_tombol(PAYMENT_BOT, menu_awal)
        if not menu_found:
            clear_order_state(sender_id)
            await event.reply(f"❌ Menu '{menu_awal}' tidak ditemukan di bot payment.")
            return

        await asyncio.sleep(DELAYS["MENU_AWAL"])

        for paket in selected_pakets:
            print("DEBUG: klik paket", paket)
            paket_found = await klik_tombol(PAYMENT_BOT, paket)
            if not paket_found:
                clear_order_state(sender_id)
                await event.reply(f"❌ Paket '{paket}' tidak ditemukan.")
                return
            await asyncio.sleep(DELAYS["PAKET"])

        print("DEBUG: klik tombol Gabung Sekarang")
        gabung_found = await klik_tombol(PAYMENT_BOT, "Gabung Sekarang")
        if not gabung_found:
            clear_order_state(sender_id)
            await event.reply("❌ Tombol 'Gabung Sekarang' tidak ditemukan.")
            return

        await asyncio.sleep(DELAYS["GABUNG"])
        qris_ok = await smart_forward_qris(event, sender_id, min_message_id=last_bot_msg_id)

        if not qris_ok:
            clear_order_state(sender_id)
            return

        username = getattr(sender, "username", None) or "Unknown"
        await save_history(sender_id, username, selected_pakets)

    except Exception as error:
        print(f"❌ Error proses_order_otomatis_core: {error}")
        clear_order_state(sender_id)
        await event.reply("❌ Terjadi error saat memproses pesanan.")


async def order_worker():
    global current_checkout_user_id

    while True:
        event, sender, sender_id, selected_pakets = await order_queue.get()
        try:
            current_checkout_user_id = sender_id
            await proses_order_otomatis_core(event, sender, sender_id, selected_pakets)
        except Exception as error:
            print(f"Error order_worker: {error}")
            try:
                await event.reply("❌ Terjadi error pada worker order.")
            except Exception:
                pass
        finally:
            current_checkout_user_id = None
            queued_user_ids.discard(sender_id)
            order_queue.task_done()


async def enqueue_order(event, sender, sender_id, selected_pakets):
    if sender_id in queued_user_ids:
        await event.reply("⏳ Pesanan kamu sudah masuk antrian ya kak.")
        return

    queued_user_ids.add(sender_id)

    queue_size_before = order_queue.qsize()
    await order_queue.put((event, sender, sender_id, selected_pakets))

    posisi = queue_size_before + 1
    if current_checkout_user_id is not None:
        posisi += 1

    queue_message = None

    if posisi <= 1:
        queue_message = await event.reply("✅ Pesanan kamu masuk antrian dan akan segera diproses.")
    else:
        queue_message = await event.reply(
            f"✅ Pesanan kamu masuk antrian.\nPosisi antrian saat ini: **{posisi}**"
        )

    state = get_order_state(sender_id)
    state["queue_message_id"] = queue_message.id if queue_message else None
    state["selected_pakets"] = selected_pakets
    state["total_harga_idr"] = hitung_total_harga_idr(selected_pakets)
    if "status" not in state:
        state["status"] = "queued"
    set_order_state(sender_id, state)


# ================== TIMEOUT CHECKER ==================
async def check_expired_orders():
    while True:
        try:
            now = now_utc()
            cursor = pending_col.find({})
            all_pending = await cursor.to_list(length=None)

            for data in all_pending:
                user_id = data.get("user_id")
                expired_at = data.get("expired_at")

                if not user_id or not expired_at:
                    continue

                if getattr(expired_at, "tzinfo", None) is not None:
                    expired_at = expired_at.replace(tzinfo=None)

                if now >= expired_at:
                    expired_text = (
                        f"❌ Konfirmasi pesanan sudah kadaluarsa karena tidak ada respon dalam "
                        f"{format_timeout_text(CONFIRM_TIMEOUT_MINUTES)}.\n"
                        f"Silakan order ulang ya kak."
                    )

                    try:
                        message_id = data.get("message_id")
                        if message_id:
                            await client.edit_message(int(user_id), message_id, expired_text)
                        else:
                            await client.send_message(int(user_id), expired_text)
                    except Exception as error:
                        print(f"Error edit/kirim expired ke {user_id}: {error}")
                        try:
                            await client.send_message(int(user_id), expired_text)
                        except Exception as error2:
                            print(f"Error fallback kirim expired ke {user_id}: {error2}")

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
        "• `.order [nama paket]` - Fallback manual order\n"
        "• `.sendjoin` - Kirim manual link join ke user waiting_payment tunggal\n"
        "• `.sendjoinid [user_id]` - Kirim manual link join ke user tertentu\n\n"
        "**Keyword customer:** `harga`\n"
        "**Konfirmasi order customer:** `ya / iya / jadi / lanjut / oke` atau `tidak / tak / batal / gajadi / cancel`\n"
        f"**Timeout konfirmasi:** {format_timeout_text(CONFIRM_TIMEOUT_MINUTES)}\n"
        "**Variabel:** `{mention}`, `{name}`, `{id}`"
    )


# ================== COMMAND SETTINGS ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]setkurs\s+([\d\.]+)$"))
async def setkurs_handler(event):
    if not event.out:
        return

    settings_data = await get_settings()
    kurs_input = event.pattern_match.group(1).replace(".", "")
    settings_data["kurs"] = int(kurs_input)
    await save_settings(settings_data)

    tampil = f"{settings_data['kurs']:,}".replace(",", ".")
    await event.reply(f"✅ Kurs MYR berhasil diatur ke: **{tampil}**")


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


# ================== COMMAND MANUAL SEND JOIN ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]sendjoin$"))
async def sendjoin_handler(event):
    if not event.out:
        return

    if not event.is_reply:
        await event.reply("❌ Reply ke pesan link join dengan `.sendjoin`")
        return

    replied_message = await event.get_reply_message()
    if not replied_message:
        await event.reply("❌ Pesan reply tidak ditemukan.")
        return

    waiting_users = await get_waiting_payment_user_ids()

    if not waiting_users:
        await event.reply("❌ Tidak ada user yang sedang menunggu pembayaran.")
        return

    if len(waiting_users) > 1:
        await event.reply(
            "❌ Ada lebih dari satu user yang sedang menunggu pembayaran.\n"
            "Gunakan `.sendjoinid USER_ID` agar tidak salah kirim."
        )
        return

    target_user_id = waiting_users[0]

    try:
        if replied_message.media:
            await client.send_file(
                target_user_id,
                replied_message.media,
                caption=replied_message.text or ""
            )
        else:
            await client.send_message(target_user_id, replied_message.text or "")

        if target_user_id in user_last_event:
            target_event = user_last_event[target_user_id]
            settings_data = await get_settings()
            thanks_text = await render_template(
                settings_data.get("thanks_text", default_settings()["thanks_text"]),
                target_event
            )
            await target_event.reply(thanks_text)

        state = get_order_state(target_user_id)
        payment_id_state = state.get("payment_id")
        if payment_id_state:
            await set_payment_status(payment_id_state, "completed")

        clear_order_state(target_user_id)

        await event.reply(f"✅ Link join berhasil dikirim ke user: `{target_user_id}`")

    except Exception as error:
        await event.reply(f"❌ Gagal kirim link join manual: {error}")


@client.on(events.NewMessage(pattern=r"(?i)^[.]sendjoinid\s+(\d+)$"))
async def sendjoinid_handler(event):
    if not event.out:
        return

    if not event.is_reply:
        await event.reply("❌ Reply ke pesan link join dengan `.sendjoinid USER_ID`")
        return

    replied_message = await event.get_reply_message()
    if not replied_message:
        await event.reply("❌ Pesan reply tidak ditemukan.")
        return

    target_user_id = int(event.pattern_match.group(1))

    state = get_order_state(target_user_id)
    if state.get("status") != "waiting_payment":
        await event.reply("❌ User ini tidak sedang dalam status waiting_payment.")
        return

    try:
        if replied_message.media:
            await client.send_file(
                target_user_id,
                replied_message.media,
                caption=replied_message.text or ""
            )
        else:
            await client.send_message(target_user_id, replied_message.text or "")

        if target_user_id in user_last_event:
            target_event = user_last_event[target_user_id]
            settings_data = await get_settings()
            thanks_text = await render_template(
                settings_data.get("thanks_text", default_settings()["thanks_text"]),
                target_event
            )
            await target_event.reply(thanks_text)

        payment_id_state = state.get("payment_id")
        if payment_id_state:
            await set_payment_status(payment_id_state, "completed")

        clear_order_state(target_user_id)

        await event.reply(f"✅ Link join berhasil dikirim ke user: `{target_user_id}`")

    except Exception as error:
        await event.reply(f"❌ Gagal kirim link join manual: {error}")


# ================== COMMAND STATS ==================
@client.on(events.NewMessage(pattern=r"(?i)^[.]stats$"))
async def stats_handler(event):
    if not event.out:
        return

    total_users = await count_users()
    total_waiting = sum(1 for state in user_states.values() if state.get("status") == "waiting_payment")
    total_history = await count_history()
    total_pending = await count_pending_confirmations()

    await event.reply(
        f"📊 **STATISTIK BOT**\n\n"
        f"• Total user database: **{total_users}**\n"
        f"• User waiting payment: **{total_waiting}**\n"
        f"• Pending konfirmasi order: **{total_pending}**\n"
        f"• User dalam antrian checkout: **{len(queued_user_ids)}**\n"
        f"• Sedang checkout: **{current_checkout_user_id if current_checkout_user_id else '-'}**\n"
        f"• Total history tersimpan: **{total_history}**"
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

    await enqueue_order(event, sender, event.sender_id, selected_pakets)


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
    now = now_utc()
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

        first_chat_skip_messages.add(event.id)
        print(f"✅ Auto harga terkirim sekali ke user {user_id}")

    except Exception as error:
        print(f"Error kirim harga first chat: {error}")


# ================== PRIVATE PHOTO HANDLER ==================
@client.on(events.NewMessage(incoming=True, func=lambda event: event.is_private and bool(event.photo)))
async def photo_handler(event):
    if event.id in first_chat_skip_messages:
        first_chat_skip_messages.discard(event.id)
        return

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
    if event.id in first_chat_skip_messages:
        first_chat_skip_messages.discard(event.id)
        return

    settings_data = await get_settings()

    await add_user_to_db(event)

    try:
        sender = await event.get_sender()
        sender_username = (getattr(sender, "username", "") or "").lower()
    except Exception:
        sender = None
        sender_username = ""

    # pesan dari bot payment utama
    if sender_username == PAYMENT_BOT.lower():
        await route_payment_bot_message(event)
        return

    # fallback: kalau ada link telegram di chat private, coba route juga
    text_raw = event.message.text or ""
    if "t.me/" in text_raw and sender_username == PAYMENT_BOT.lower():
        await route_payment_bot_message(event)
        return

    text = normalize_text(text_raw)
    sender_id = event.sender_id

    if not text:
        return

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

    pending = await get_pending_confirmation(sender_id)
    if pending:
        if is_affirmative(text):
            selected_pakets = pending.get("pakets", [])
            await delete_pending_confirmation(sender_id)
            await enqueue_order(event, sender, sender_id, selected_pakets)
            return

        if is_negative(text):
            try:
                message_id = pending.get("message_id")
                if message_id:
                    await client.edit_message(sender_id, message_id, "❌ Pesanan dibatalkan.")
                else:
                    await event.reply("❌ Pesanan dibatalkan.")
            except Exception:
                await event.reply("❌ Pesanan dibatalkan.")

            await delete_pending_confirmation(sender_id)
            return

    # kalau user lagi waiting payment lalu kirim kata-kata konfirmasi lagi
    state = get_order_state(sender_id)
    if state.get("status") == "waiting_payment":
        if is_affirmative(text):
            await event.reply("⏳ Pembayaran kamu sedang diproses ya kak, mohon tunggu sebentar 🙏")
            return

    if text.startswith(".") or text.startswith("/"):
        return

    print("DEBUG TEXT:", text)

    selected_pakets = [
        package_name
        for package_name, keywords in PAKET_MAPPING.items()
        if any(match_kata(text, keyword) for keyword in keywords)
    ]

    # hilangkan duplikat tetap urut
    selected_pakets = list(dict.fromkeys(selected_pakets))

    print("DEBUG SELECTED PAKETS:", selected_pakets)

    if not selected_pakets:
        print("DEBUG: tidak ada paket yang cocok")
        return

    nama_paket = ", ".join(selected_pakets)
    total_harga_idr = hitung_total_harga_idr(selected_pakets)
    kurs = settings_data.get("kurs", 0)

    extra_harga = ""

    if kurs > 0 and total_harga_idr > 0:
        total_harga_myr = total_harga_idr / kurs
        rupiah_text = format_rupiah(total_harga_idr)
        extra_harga = f"💰 Detail harga:\n{rupiah_text}\nRM{total_harga_myr:.2f}"
    elif total_harga_idr > 0:
        extra_harga = f"💰 Detail harga:\n{format_rupiah(total_harga_idr)}"

    if extra_harga:
        extra_harga = f"{extra_harga}\n\n"

    konfirmasi_text = (
        "🛒 Konfirmasi Pesanan\n\n"
        f"Halo kak, kamu memilih:\n"
        f"**{nama_paket}**\n\n"
        f"{extra_harga}"
        "Kalau sudah sesuai, balas salah satu:\n"
        "**ya / iya / jadi / lanjut / oke** — untuk lanjut proses\n"
        "**tidak / tak / batal / gajadi / cancel** — untuk batal\n\n"
        f"⏳ Konfirmasi berlaku selama {format_timeout_text(CONFIRM_TIMEOUT_MINUTES)}."
    )

    sent_message = await event.reply(konfirmasi_text)
    await create_pending_confirmation(sender_id, selected_pakets, sent_message.id)


# ================== MAIN ==================
async def main():
    timeout_task = None
    worker_task = None

    while True:
        try:
            print("🚀 WARUNG LENDIR ASSISTANT sedang berjalan...")
            print("🚀 Menghubungkan ke Telegram...")
            await client.start()
            print("✅ Bot berhasil terhubung ke Telegram!")

            if timeout_task is None or timeout_task.done():
                timeout_task = asyncio.create_task(check_expired_orders())

            if worker_task is None or worker_task.done():
                worker_task = asyncio.create_task(order_worker())

            await client.run_until_disconnected()

        except KeyboardInterrupt:
            print("\n🛑 Bot dihentikan manual.")
            break

        except Exception as error:
            print(f"❌ Koneksi terputus: {type(error).__name__} - {error}")
            print("🔄 Mencoba reconnect dalam 8 detik...")
            await asyncio.sleep(8)


if __name__ == "__main__":
    asyncio.run(main())
