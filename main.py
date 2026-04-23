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
    ]

    return any(keyword in text_lower for keyword in keywords)

async def send_join_to_target_user(source_event, target_user_id: int):
    target_event = user_last_event.get(target_user_id)
    if not target_event:
        print(f"⚠️ user_last_event tidak ditemukan untuk user {target_user_id}")
        return False

    try:
        # Jika pesan mengandung media, kita cek jika itu adalah WebPage dan kirim hanya teks link
        if source_event.message.media:
            if isinstance(source_event.message.media, MessageMediaWebPage):
                # Jika itu adalah WebPage, hanya kirim teks link
                await client.send_message(target_event.chat_id, source_event.message.text or "")
            else:
                # Jika bukan WebPage, kirim file
                await client.send_file(
                    target_event.chat_id,
                    source_event.message.media,
                    caption=source_event.message.text or ""
                )
        else:
            # Jika hanya teks, kirimkan teks link
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


# ================== FIRST CHAT HANDLER ==================
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

    except Exception as error:
        print(f"Error kirim harga first chat: {error}")

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
