# bot.py
# Single-file Pyrogram bot — QR gen/scan, URL shortener, admin controls, ChatBase chat, web status.
# Uses MongoDB (db: quicklink_bot) if provided, else falls back to local JSON storage.
import requests
import os
import io
import json
import time
import asyncio
import tempfile
import secrets
import urllib.parse
from datetime import datetime, timedelta
from functools import partial
from typing import Dict, Any, List, Optional
import random

from dotenv import load_dotenv
load_dotenv("/etc/secrets/.env") # Load .env file if it exists
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
import qrcode
import requests
from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode
from aiohttp import web
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ServerSelectionTimeoutError
from pyrogram.errors import FloodWait

# -------------------------
# Load env
# -------------------------
load_dotenv()
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

# --- MODIFIED: Handle missing API_ID/HASH ---
# We load them as strings first to check if they exist
API_ID_STR = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")
# --- End Modification ---

QUICKLINK_API_KEY = os.getenv("QUICKLINK_API_KEY")
QUICKLINK_ENDPOINT = os.getenv("QUICKLINK_ENDPOINT", "https://quick-link-url-shortener.vercel.app/api/v1/st")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PORT = int(os.getenv("PORT", "10000")) # Use 10000 as default
MONGO_URI = os.getenv("MONGO_URI")  # if provided, use mongo
CHATBASE_API_KEY = os.getenv("CHATBASE_API_KEY")
CHATBASE_BOT_ID = os.getenv("CHATBASE_BOT_ID")

if not BOT_TOKEN or not OWNER_ID:
    raise RuntimeError("Set TG_BOT_TOKEN and OWNER_ID in .env")

# -------------------------
# Storage: prefer MongoDB (db=quicklink_bot), fallback to local JSON in temp
# -------------------------
DB = None
mongo_ok = False
if MONGO_URI:
    try:
        mc = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mc.server_info()  # raise if cannot connect
        DB = mc["quicklink_bot"]  # DB name as requested
        mongo_ok = True
    except Exception as e:
        print(f"Warning: Mongo not available: {e}")
        mongo_ok = False

STORAGE_FILE = os.path.join(tempfile.gettempdir(), "quicklink_bot_storage.json")


def load_storage_local() -> Dict[str, Any]:
    default = {
        "users": [],
        "stats": {"shorten": 0, "qrgen": 0, "qrscan": 0},
        "last_urls": [],
        "features": {"shorten": True, "qrgen": True, "qrscan": True, "broadcast": True},
        "last_broadcast": None
    }
    try:
        if os.path.exists(STORAGE_FILE):
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                for k in default:
                    if k not in d:
                        d[k] = default[k]
                return d
    except Exception as e:
        print(f"Warning: Could not load local storage: {e}")
    return default


def save_storage_local(data: Dict[str, Any]):
    try:
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warning: Could not save local storage: {e}")


LOCAL = load_storage_local()


def register_user_db(user_id: int):
    if mongo_ok:
        try:
            DB["users"].update_one({"_id": user_id}, {"$setOnInsert": {"_id": user_id, "added": time.time()}}, upsert=True)
        except Exception as e:
            print(f"DB Error (register_user_db): {e}")
    else:
        if user_id not in LOCAL["users"]:
            LOCAL["users"].append(user_id)
            save_storage_local(LOCAL)


def inc_stat_db(key: str, n: int = 1):
    if mongo_ok:
        try:
            DB["stats"].update_one({"_id": "global"}, {"$inc": {f"counts.{key}": n}}, upsert=True)
        except Exception as e:
            print(f"DB Error (inc_stat_db): {e}")
    else:
        LOCAL["stats"][key] = LOCAL["stats"].get(key, 0) + n
        save_storage_local(LOCAL)


def push_short_url_db(url: str):
    ts = int(time.time())
    if mongo_ok:
        try:
            DB["urls"].insert_one({"url": url, "ts": ts})
        except Exception as e:
            print(f"DB Error (push_short_url_db): {e}")
    else:
        LOCAL["last_urls"].insert(0, {"url": url, "ts": ts})
        LOCAL["last_urls"] = LOCAL["last_urls"][:20]
        save_storage_local(LOCAL)


def get_stats_db() -> Dict[str, Any]:
    if mongo_ok:
        try:
            s = DB["stats"].find_one({"_id": "global"}) or {}
            counts = s.get("counts", {})
            return {"shorten": counts.get("shorten", 0), "qrgen": counts.get("qrgen", 0), "qrscan": counts.get("qrscan", 0)}
        except Exception as e:
            print(f"DB Error (get_stats_db): {e}")
    return LOCAL["stats"]


def get_last_urls_db(n: int = 5) -> List[Dict[str, Any]]:
    if mongo_ok:
        try:
            docs = DB["urls"].find().sort("ts", -1).limit(n)
            # Match your /state format: "original -> short"
            # Assuming 'url' field stores the short URL
            # We can't get the original URL from here, so we just list the short ones.
            # Your bot.py doesn't store the original URL, so this is the best we can do.
            return [{"url": d.get("url"), "ts": d.get("ts")} for d in docs]
        except Exception as e:
            print(f"DB Error (get_last_urls_db): {e}")
    # Same for local
    return LOCAL["last_urls"][:n]


def get_all_users_db() -> List[int]:
    if mongo_ok:
        try:
            return [d["_id"] for d in DB["users"].find({}, {"_id": 1})]
        except Exception as e:
            print(f"DB Error (get_all_users_db): {e}")
    return LOCAL["users"][:]


def set_feature_db(key: str, val: bool):
    if mongo_ok:
        try:
            DB["config"].update_one({"_id": "features"}, {"$set": {key: val}}, upsert=True)
        except Exception as e:
            print(f"DB Error (set_feature_db): {e}")
    else:
        LOCAL["features"][key] = val
        save_storage_local(LOCAL)


def get_features_db() -> Dict[str, bool]:
    if mongo_ok:
        try:
            doc = DB["config"].find_one({"_id": "features"}) or {}
            # Match your /admin command list
            default_features = {"shorten": True, "qrgen": True, "qrscan": True, "broadcast": True, "chat": True}
            # Merge defaults with DB
            doc.pop("_id", None)
            default_features.update(doc)
            return default_features
        except Exception as e:
            print(f"DB Error (get_features_db): {e}")
    # Update local default to include 'chat'
    if "chat" not in LOCAL["features"]:
        LOCAL["features"]["chat"] = True
    return LOCAL["features"]


# -------------------------
# Temp helpers
# -------------------------
def temp_path_for(prefix: str, suffix: str = ".png"):
    return os.path.join(tempfile.gettempdir(), f"{prefix}_{secrets.token_hex(8)}{suffix}")


async def schedule_delete(path: str, delay: int = 300):
    await asyncio.sleep(delay)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"Warning: Could not delete temp file {path}: {e}")


# -------------------------
# QR helpers
# -------------------------
def build_qr_png_bytes(data: str, size: int = 1000) -> bytes:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    img = img.resize((size, size), Image.LANCZOS)
    bio = io.BytesIO()
    img.save(bio, format="PNG", optimize=True)
    bio.seek(0)
    return bio.read()


def local_scan_qr(file_path: str) -> List[str]:
    try:
        img = Image.open(file_path).convert("RGB")
        decoded = zbar_decode(img)
        results = [d.data.decode("utf-8") for d in decoded if d and d.data]
        return results
    except Exception as e:
        print(f"Error (local_scan_qr): {e}")
        return []


def fallback_scan_qr_api(file_path: str) -> List[str]:
    # Using the API you requested: goqr.me/api/
    # This is a different API than your code had, but matches your prompt.
    try:
        with open(file_path, "rb") as f:
            files = {"file": ("qr.png", f, "image/png")}
            # This API is simple and requires no auth
            r = requests.post("https://api.qrserver.com/v1/read-qr-code/", files=files, timeout=30)
            j = r.json()
            texts = []
            for item in j:
                for symbol in item.get("symbol", []):
                    data = symbol.get("data")
                    if data and not symbol.get("error"):
                        texts.append(data)
            return texts
    except Exception as e:
        print(f"Error (fallback_scan_qr_api): {e}")
        return []


# -------------------------
# QuickLink shorten helper
# -------------------------
def quicklink_shorten(long_url: str, alias: str = "") -> Dict[str, Any]:
    # Using the endpoint from your .env.example
    params = {"api": QUICKLINK_API_KEY, "url": long_url, "alias": alias or ""}
    try:
        r = requests.get(QUICKLINK_ENDPOINT, params=params, timeout=15)
        if r.status_code == 200:
            return r.json() # Expects {"status": "success", "shortenedUrl": "..."}
        print(f"Error: QuickLink API returned status {r.status_code}: {r.text}")
        return {"status": "error", "message": r.text}
    except Exception as e:
        print(f"Error (quicklink_shorten): {e}")
        return {"status": "error", "message": str(e)}


# -------------------------
# Chatbase support (simple)
# -------------------------
def chatbase_query(user_text: str) -> str:
    if not CHATBASE_API_KEY or not CHATBASE_BOT_ID:
        return "Chatbase AI support is not configured by the admin."
    
    # Payload matches your feature list
    payload = {"messages": [{"content": user_text, "role": "user"}], "chatbotId": CHATBASE_BOT_ID}
    headers = {"Authorization": f"Bearer {CHATBASE_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post("https://www.chatbase.co/api/v1/chat", headers=headers, json=payload, timeout=20)
        jr = r.json()
        
        # Extract the text response
        text = jr.get("text") # This is the common response key
        if not text:
             # Fallback logic from your original code
            if "messages" in jr and isinstance(jr["messages"], list) and jr["messages"]:
                text = jr["messages"][-1].get("content","")
        
        return text or "The AI returned an empty response."
    except Exception as e:
        print(f"Error (chatbase_query): {e}")
        return f"Chatbase AI error: {e}"


# -------------------------
# Pyrogram client
# --- MODIFIED: Smart Client Initialization ---
# -------------------------

# Initialize Pyrogram Client
if API_ID_STR and API_HASH:
    print("Initializing Pyrogram with API_ID and API_HASH.")
    app = Client(
        "quicklink_bot",
        bot_token=BOT_TOKEN,
        api_id=int(API_ID_STR),
        api_hash=API_HASH
    )
else:
    print("Initializing Pyrogram with bot_token only (API_ID/API_HASH not found).")
    app = Client(
        "quicklink_bot",
        bot_token=BOT_TOKEN
    )
# --- End Modification ---


INTERACTIVE: Dict[int, Dict[str, Any]] = {}

# format uptime: YYYY:MM:DD:HH:MM:SS:ms
START_TS = time.time()


def uptime_str():
    now = datetime.utcnow()
    ms = int(now.microsecond / 1000)
    # Simple uptime string
    total_seconds = int(time.time() - START_TS)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s"


# ---------- /start ----------
@app.on_message(filters.command("start"))
async def start_cmd(_, msg: Message):
    register_user_db(msg.from_user.id)
    features = get_features_db()
    txt = (
        f"👋 *QuickLink Utilities Bot*\n\n"
        f"Hi {msg.from_user.first_name}! I can shorten links, generate/scan QRs, and more.\n\n"
        f"**Available Commands:**\n"
        f"/shortner - Shorten a long URL\n"
        f"/qrgen - Generate a QR code (Text, WiFi, etc.)\n"
        f"/qrscan - Scan a QR code from an image\n"
        f"/chat - Talk to the support AI\n"
        f"/state - Show bot usage stats\n"
        f"/owner - View bot owner's info\n\n"
        f"Bot Uptime: `{uptime_str()}`"
    )
    await msg.reply_text(txt)


# ---------- /state (Bot Stats) ----------
@app.on_message(filters.command("state"))
async def state_cmd(_, msg: Message):
    register_user_db(msg.from_user.id)
    stats = get_stats_db()
    last5 = get_last_urls_db(5) # This just gets the short URL
    
    last_text = ""
    if last5:
        for i, item in enumerate(last5):
            # We don't have the original URL, so we just show the short one
            last_text += f"{i+1}. {item['url']}\n"
    else:
        last_text = "No recent URLs"

    txt = (
        f"📊 *Quicklink Bot Stats*\n\n"
        f"• Total URLs Shortened: {stats.get('shorten',0)}\n"
        f"• QR Generated: {stats.get('qrgen',0)}\n"
        f"• QR Scanned: {stats.get('qrscan',0)}\n\n"
        f"🕐 *Recent Shortened URLs:*\n{last_text}"
    )
    await msg.reply_text(txt)


# ---------- /chat (Chatbase) ----------
@app.on_message(filters.command("chat"))
async def chat_cmd(_, msg: Message):
    if not get_features_db().get("chat", True):
        return await msg.reply_text("⚠️ This feature is temporarily disabled by the admin.")
    
    register_user_db(msg.from_user.id)
    if len(msg.command) < 2:
        return await msg.reply_text("Usage: `/chat <your message>`\nExample: `/chat How does the URL shortener work?`")
    
    user_msg = msg.text.split(" ",1)[1]
    await msg.reply_text("💬 Asking AI... (this may take a moment)", quote=True)
    # Run the blocking network request in an executor
    res = await asyncio.get_event_loop().run_in_executor(None, partial(chatbase_query, user_msg))
    await msg.reply_text(f"🧠 **AI Support:**\n\n{res}")


# ---------- Admin /admin toggles (Owner Panel) ----------
def feature_keyboard():
    f = get_features_db()
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Shortener: {'✅ ON' if f.get('shorten') else '❌ OFF'}", callback_data="ft|shorten"),
            InlineKeyboardButton(f"QR Gen: {'✅ ON' if f.get('qrgen') else '❌ OFF'}", callback_data="ft|qrgen")
        ],
        [
            InlineKeyboardButton(f"QR Scan: {'✅ ON' if f.get('qrscan') else '❌ OFF'}", callback_data="ft|qrscan"),
            InlineKeyboardButton(f"Chat AI: {'✅ ON' if f.get('chat') else '❌ OFF'}", callback_data="ft|chat")
        ],
        [
            InlineKeyboardButton(f"Broadcast: {'✅ ON' if f.get('broadcast') else '❌ OFF'}", callback_data="ft|broadcast")
        ],
    ])
    return kb


@app.on_message(filters.command("admin"))
async def admin_cmd(_, msg: Message):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply_text("❌ You are not the owner.")
    await msg.reply_text("🔑 *Admin Control Panel*\nToggle features on or off for all users:", reply_markup=feature_keyboard())


@app.on_callback_query(filters.regex(r"^ft\|"))
async def feature_toggle(_, cq):
    user_id = cq.from_user.id
    if user_id != OWNER_ID:
        return await cq.answer("Not allowed", show_alert=True)
    
    key = cq.data.split("|",1)[1]
    features = get_features_db()
    # Flip the boolean value
    new_val = not features.get(key, True)
    set_feature_db(key, new_val)
    
    # Edit message with new keyboard
    await cq.message.edit_text("🔑 *Admin Control Panel*\nSettings updated!", reply_markup=feature_keyboard())
    await cq.answer(f"{key.title()} is now {'ON' if new_val else 'OFF'}")


# ---------- /broadcast (owner only) ----------
@app.on_message(filters.command("broadcast"))
async def broadcast_start(_, msg: Message):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply_text("❌ Only owner can broadcast.")
    if not get_features_db().get("broadcast", True):
        return await msg.reply_text("⚠️ Broadcast feature disabled by admin.")
    
    await msg.reply_text("📨 *New Broadcast*\nSend the message you want to broadcast (text, photo, or video). You have 5 minutes.",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="bc|cancel_listen")]]))

    try:
        # Wait for the next message from the admin
        bmsg = await app.listen(msg.chat.id, timeout=300, filters=~filters.command(["broadcast"]))
        if bmsg.text and bmsg.text == "/cancel":
             return await msg.reply_text("Broadcast cancelled.")
    except asyncio.TimeoutError:
        return await msg.reply_text("⏰ Timeout. Broadcast cancelled.")
    
    # Check if user cancelled with the button
    if INTERACTIVE.get(msg.from_user.id, {}).get("flow") == "broadcast_cancelled":
        INTERACTIVE.pop(msg.from_user.id, None)
        return await msg.reply_text("Broadcast cancelled.")

    text = bmsg.caption or bmsg.text or ""
    file_id = None
    file_type = None

    if bmsg.photo:
        file_id = bmsg.photo.file_id
        file_type = "photo"
    elif bmsg.video:
        file_id = bmsg.video.file_id
        file_type = "video"
    elif bmsg.document:
        file_id = bmsg.document.file_id
        file_type = "document"
    
    # Store message info for confirmation
    INTERACTIVE[msg.from_user.id] = {
        "flow": "broadcast_confirm",
        "bc_text": text, 
        "bc_file_id": file_id,
        "bc_file_type": file_type
    }
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Send Now", callback_data="bc|confirm")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="bc|cancel")]
    ])
    await msg.reply_text("Message preview captured. Are you sure you want to send this to all users?", reply_markup=kb)

@app.on_callback_query(filters.regex(r"^bc\|cancel_listen$"))
async def broadcast_cancel_listen(_, cq):
    if cq.from_user.id != OWNER_ID:
        return await cq.answer("Not allowed", show_alert=True)
    INTERACTIVE[cq.from_user.id] = {"flow": "broadcast_cancelled"}
    await cq.message.edit_text("Broadcast cancelled.")


@app.on_callback_query(filters.regex(r"^bc\|(confirm|cancel)$"))
async def broadcast_cb(_, cq):
    uid = cq.from_user.id
    if uid != OWNER_ID:
        return await cq.answer("Not allowed", show_alert=True)
    
    action = cq.data.split("|",1)[1]
    state = INTERACTIVE.get(uid)
    
    if not state or state.get("flow") != "broadcast_confirm":
        return await cq.answer("Nothing to send or session expired.", show_alert=True)
    
    if action == "cancel":
        INTERACTIVE.pop(uid, None)
        return await cq.message.edit_text("Broadcast cancelled.")

    await cq.message.edit_text("🚀 Broadcast starting... This may take several minutes.\nI will send a final report when done.")

    users = get_all_users_db()
    total = len(users)
    if total == 0:
        INTERACTIVE.pop(uid, None)
        return await cq.message.edit_text("No registered users to send.")

    # Get message content from state
    text = state.get("bc_text")
    file_id = state.get("bc_file_id")
    file_type = state.get("bc_file_type")

    # Define the send function
    async def send_message(user_id):
        if file_type == "photo":
            await app.send_photo(user_id, photo=file_id, caption=text)
        elif file_type == "video":
            await app.send_video(user_id, video=file_id, caption=text)
        elif file_type == "document":
            await app.send_document(user_id, document=file_id, caption=text)
        else:
            await app.send_message(user_id, text)

    # compute base interval
    base_interval = 300.0 / total # Aim to finish in ~5 minutes
    interval = max(0.05, min(2.0, base_interval))  # 50ms min, 2s max

    sent = 0
    failed = 0
    start_t = time.time()
    
    for user in users:
        try:
            await send_message(user)
            sent += 1
        except FloodWait as e:
            wait = min(int(e.x) + 2, 300)
            print(f"FloodWait: sleeping for {wait}s")
            await asyncio.sleep(wait)
            try:
                await send_message(user)
                sent += 1
            except Exception as e2:
                print(f"Broadcast failed to user {user} after FloodWait: {e2}")
                failed += 1
        except Exception as e3:
            # Catch common errors like "user blocked bot"
            print(f"Broadcast failed to user {user}: {e3}")
            failed += 1
        
        # Sleep between sends to avoid hitting limits
        await asyncio.sleep(interval) 
        
    end_t = time.time()
    INTERACTIVE.pop(uid, None)
    
    # Store last broadcast time
    if mongo_ok:
        try:
            DB["config"].update_one({"_id":"last_broadcast"}, {"$set":{"ts":int(time.time())}}, upsert=True)
        except Exception: pass
    else:
        LOCAL["last_broadcast"] = int(time.time()); save_storage_local(LOCAL)
        
    await cq.message.edit_text(f"✅ *Broadcast Completed*\n\nSent: {sent}\nFailed: {failed}\nTotal Users: {total}\nTime Taken: {int(end_t-start_t)}s")


# ---------- QR generation/interactive flows ----------
# Matches your feature list
QR_TYPES = [
    ("Text", "text"), ("Link", "link"), ("WiFi", "wifi"), ("Email", "email"),
    ("Phone", "phone"), ("WhatsApp", "whatsapp"), ("UPI", "upi"), ("SMS", "message")
]

@app.on_message(filters.command("qrgen"))
async def qrgen_start(_, msg: Message):
    if not get_features_db().get("qrgen", True):
        return await msg.reply_text("⚠️ This feature is temporarily disabled by the admin.")
    
    register_user_db(msg.from_user.id)
    
    # Create 4x2 button layout
    buttons = []
    row = []
    for label, val in QR_TYPES:
        row.append(InlineKeyboardButton(label, callback_data=f"qrtype|{val}"))
        if len(row) == 2: # 2 buttons per row
            buttons.append(row)
            row = []
    if row: # Add any remaining buttons
        buttons.append(row)
        
    kb = InlineKeyboardMarkup(buttons)
    await msg.reply_text("❓ *What do you want to generate?*", reply_markup=kb)


@app.on_callback_query(filters.regex(r"^qrtype\|"))
async def qrtype_cb(_, cq):
    user_id = cq.from_user.id
    _, qrtype = cq.data.split("|",1)
    
    INTERACTIVE[user_id] = {"flow":"qrgen", "type": qrtype, "data": {}}
    await cq.answer()
    
    # Prompts based on your feature list
    prompts = {
        "text": "Enter the text you want to encode:",
        "link": "Enter the website link (e.g., https://google.com):",
        "wifi": "Enter the Network Name (SSID):",
        "email": "Enter the 'To' Email Address:",
        "phone": "Enter the Phone Number (with country code, e.g., +123456...):",
        "whatsapp": "Enter the WhatsApp Number (with country code, no +):",
# We must define all commands here to exclude them from the private message handler
ALL_COMMANDS = [
    "start", "state", "chat", "admin", "broadcast", 
    "qrgen", "qrscan", "shortner", "owner"
]

@app.on_message(filters.private & ~filters.command(ALL_COMMANDS)) # Catches all non-command messages
async def private_flow_handler(_, msg: Message):
    uid = msg.from_user.id
    if uid not in INTERACTIVE:
        # User is not in a flow
        await msg.reply_text("I'm not sure what you mean. Try /start to see available commands.")
        return
    
    state = INTERACTIVE.get(uid)
    if not state:
        return # Safeguard

    if state.get("flow") == "qrgen":
        await handle_qrgen_step(msg, state)
    elif state.get("flow") == "shorten":
        await handle_shorten_step(msg, state)
    # qrscan_wait is handled by `app.listen` in the /qrscan command itself
    # broadcast is handled by `app.listen` in the /broadcast command


async def handle_qrgen_step(msg: Message, state: Dict):
    uid = msg.from_user.id
    typ = state["type"]; d = state["data"]
    try:
        # --- Text / Link ---
        if typ in ("text","link"):
            if "content" not in d:
                d["content"] = msg.text or ""
                qr_text = d["content"].strip()
                if not qr_text:
                    await msg.reply_text("Please send valid text/link.")
                    return
                png = build_qr_png_bytes(qr_text, size=1000)
                await msg.reply_photo(png, caption=f"✅ *QR Generated Successfully!*\nType: {typ.title()}\n\n`{qr_text}`")
                inc_stat_db("qrgen");
                return INTERACTIVE.pop(uid, None)
        
        # --- WiFi ---
        if typ == "wifi":
            if "ssid" not in d:
                d["ssid"] = msg.text or ""; await msg.reply_text("Enter Password (or send `-` for an open network):"); return
            if "password" not in d:
                d["password"] = msg.text or ""
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔐 WPA/WPA2", callback_data="wifisec|WPA")],
                    [InlineKeyboardButton("🧩 WEP", callback_data="wifisec|WEP")],
                    [InlineKeyboardButton("🔓 None", callback_data="wifisec|NONE")]
                ])
                await msg.reply_text("Choose Security Type:", reply_markup=kb); return
        
        # --- Email ---
        if typ == "email":
            if "to" not in d:
                d["to"] = msg.text or ""; await msg.reply_text("Enter Subject (or send `-` to skip):"); return
            if "subject" not in d:
                d["subject"] = msg.text if (msg.text and msg.text!="-") else ""; await msg.reply_text("Enter Body (or send `-` to skip):"); return
            if "body" not in d:
                d["body"] = msg.text if (msg.text and msg.text!="-") else ""
                mailto = f"mailto:{d['to']}?subject={urllib.parse.quote(d['subject'])}&body={urllib.parse.quote(d['body'])}"
                png = build_qr_png_bytes(mailto, size=1000)
                await msg.reply_photo(png, caption=f"✅ *QR Generated Successfully!*\nType: Email")
                inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
        
        # --- Phone ---
        if typ == "phone":
            d["phone"] = msg.text or ""; tel = f"tel:{d['phone']}"; png = build_qr_png_bytes(tel,1000)
            await msg.reply_photo(png, caption=f"✅ *QR Generated Successfully!*\nType: Phone\n\n`{tel}`"); inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
        
        # --- WhatsApp ---
        if typ == "whatsapp":
            if "number" not in d:
                d["number"] = msg.text or ""; await msg.reply_text("Enter pre-filled message (or send `-` to skip):"); return
            if "message" not in d:
                d["message"] = msg.text if (msg.text and msg.text!="-") else ""
                wa = f"https://wa.me/{d['number']}?text={urllib.parse.quote(d['message'])}"
                png = build_qr_png_bytes(wa,1000)
                await msg.reply_photo(png, caption=f"✅ *QR Generated Successfully!*\nType: WhatsApp")
                inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
        
        # --- UPI ---
        if typ == "upi":
            if "pa" not in d: # Payee Address (UPI ID)
                d["pa"] = msg.text or ""; await msg.reply_text("Enter Payee Name (or send `-` to skip):"); return
            if "pn" not in d: # Payee Name
                d["pn"] = msg.text if (msg.text and msg.text!="-") else ""; await msg.reply_text("Enter Amount (e.g., 50.00) (or send `-` to skip):"); return
            if "am" not in d: # Amount
                d["am"] = msg.text if (msg.text and msg.text!="-") else ""; await msg.reply_text("Enter Note/Remarks (or send `-` to skip):"); return
            if "tn" not in d: # Transaction Note
                d["tn"] = msg.text if (msg.text and msg.text!="-") else ""
                upi = f"upi://pay?pa={urllib.parse.quote(d['pa'])}&pn={urllib.parse.quote(d['pn'])}&am={urllib.parse.quote(d['am'])}&tn={urllib.parse.quote(d['tn'])}"
                png = build_qr_png_bytes(upi,1000)
                await msg.reply_photo(png, caption=f"✅ *QR Generated Successfully!*\nType: UPI")
                inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)

        # --- SMS / Message ---
        if typ == "message":
            if "phone" not in d:
                d["phone"] = msg.text or ""; await msg.reply_text("Enter the SMS text you want to pre-fill:"); return
            if "text" not in d:
                d["text"] = msg.text or ""
                smsto = f"SMSTO:{d['phone']}:{d['text']}"
                png = build_qr_png_bytes(smsto,1000)
                await msg.reply_photo(png, caption=f"✅ *QR Generated Successfully!*\nType: SMS\n\n`{smsto}`"); inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
                
    except Exception as e:
        print(f"Error in handle_qrgen_step (uid {uid}, type {typ}): {e}")
        await msg.reply_text("An error occurred. Flow cancelled.")
        INTERACTIVE.pop(uid, None)


@app.on_callback_query(filters.regex(r"^wifisec\|"))
async def wifisec_cb(_, cq):
    uid = cq.from_user.id; await cq.answer()
    _, sec = cq.data.split("|",1)
    
    st = INTERACTIVE.get(uid)
    if not st or st.get("flow")!="qrgen": return await cq.message.edit_text("Session expired.")
    
    d = st["data"]; ssid = d.get("ssid",""); pwd = d.get("password","")
    # Handle 'None' security
    if sec == "NONE":
        secv = "nopass" # 'nopass' is the correct type for no password
        pwd = "" # Password must be empty
    else:
        secv = sec # WPA or WEP
        
    wifi_text = f"WIFI:T:{secv};S:{ssid};P:{pwd};;"
    png = build_qr_png_bytes(wifi_text,1000)
    await cq.message.delete() # Delete the "Choose Security" message
    await cq.message.reply_photo(png, caption=f"✅ *QR Generated Successfully!*\nType: WiFi\nSSID: {ssid}")
    inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)


# ---------- /qrscan ----------
@app.on_message(filters.command("qrscan"))
async def qrscan_start(_, msg: Message):
    if not get_features_db().get("qrscan", True):
        return await msg.reply_text("⚠️ This feature is temporarily disabled by the admin.")
    
    register_user_db(msg.from_user.id)
    uid = msg.from_user.id; INTERACTIVE[uid] = {"flow":"qrscan_wait"}
    
    prompt = await msg.reply_text("📸 Send QR image within 60s.")
    # We don't need a live timer, just a timeout on the listener
    
    try:
        # We use app.listen to wait for the *next* message in this chat
        got = await app.listen(chat_id=msg.chat.id, timeout=60, filters=filters.photo | filters.document)
    except asyncio.TimeoutError:
        INTERACTIVE.pop(uid, None); return await prompt.edit_text("⏰ Timeout — no image received. Scan cancelled.")
    
    # Check if the received message is actually a photo or document
    if not (got.photo or (got.document and got.document.mime_type and "image" in got.document.mime_type)):
        INTERACTIVE.pop(uid, None); return await prompt.edit_text("That's not an image. Scan cancelled. Try /qrscan again.")

    fpath = temp_path_for("qrscan",".png")
    try:
        await prompt.edit_text("Downloading image...")
        await got.download(file_name=fpath)
    except Exception as e:
        print(f"Error downloading file: {e}")
        INTERACTIVE.pop(uid, None)
        return await prompt.edit_text("Error downloading file. Please try again.")

    await prompt.edit_text("🔎 Scanning locally (using zbar)...")
    loop = asyncio.get_event_loop()
    local_res = await loop.run_in_executor(None, partial(local_scan_qr, fpath))
    inc_stat_db("qrscan")
    
    if local_res:
        INTERACTIVE.pop(uid, None)
        await prompt.edit_text(f"✅ *Local Decode Success:*\n\n`{chr(10).join(local_res)}`")
        asyncio.create_task(schedule_delete(fpath,delay=300)); 
        return
    
    # Fallback offer (as requested)
    INTERACTIVE[uid] = {"flow": "qrscan_fallback", "pending_file": fpath}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Use Partner API", callback_data="qrfb|yes")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="qrfb|no")]
    ])
    await prompt.edit_text("❌ Couldn’t decode locally.\n\nDo you want to use our trusted partner API for better scanning? (It’s free & 99.9% accurate)", reply_markup=kb)


@app.on_callback_query(filters.regex(r"^qrfb\|"))
async def qrfallback_cb(_, cq):
    await cq.answer()
    uid = cq.from_user.id; action = cq.data.split("|",1)[1]
    
    st = INTERACTIVE.get(uid)
    if not st or st.get("flow") != "qrscan_fallback" or "pending_file" not in st: 
        return await cq.message.edit_text("Session expired.")
    
    fpath = st["pending_file"]
    
    if action == "no":
        INTERACTIVE.pop(uid, None)
        try: os.remove(fpath)
        except Exception: pass
        return await cq.message.edit_text("Scan cancelled.")
    
    await cq.message.edit_text("🔁 Scanning with external API (api.qrserver.com)...")
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, partial(fallback_scan_qr_api, fpath))
    INTERACTIVE.pop(uid, None)
    
    if res: 
        await cq.message.edit_text(f"✅ *External Decode Success:*\n\n`{chr(10).join(res)}`")
    else: 
        await cq.message.edit_text("❌ External decode also failed. Could not read QR code.")
        
    asyncio.create_task(schedule_delete(fpath,delay=300))


# ---------- /shortner (URL Shortener) ----------
@app.on_message(filters.command("shortner")) # Using "shortner" as requested
async def shorten_start(_, msg: Message):
    if not get_features_db().get("shorten", True):
        return await msg.reply_text("⚠️ This feature is temporarily disabled by the admin.")
    
    register_user_db(msg.from_user.id)
    uid = msg.from_user.id
    INTERACTIVE[uid] = {"flow":"shorten","state":"wait_url"}
    await msg.reply_text("🔗 Send me the long URL you want to shorten (must start with `http://` or `https://`):")


async def handle_shorten_step(msg: Message, state: Dict):
    uid = msg.from_user.id; st = state.get("state")
    try:
        if st == "wait_url":
            text = (msg.text or "").strip()
            if not (text.startswith("http://") or text.startswith("https://")): 
                await msg.reply_text("Invalid URL. Please send a valid URL (must start with `http://` or `https://`).")
                return
            
            state["long_url"] = text
            state["state"] = "wait_alias"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Skip (Use Random Alias)", callback_data="alias|skip")],
                [InlineKeyboardButton("Enter Alias Manually", callback_data="alias|manual")]
            ])
            await msg.reply_text("Do you want to use a custom alias or skip?", reply_markup=kb)
            return
            
        if st == "wait_alias_manual":
            alias_text = (msg.text or "").strip()
            if not alias_text: 
                await msg.reply_text("Alias cannot be empty. Please send a valid alias (e.g., `my-link`) or /cancel.")
                return
            
            await msg.reply_text("⏳ Shortening with custom alias...")
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, partial(quicklink_shorten, state["long_url"], alias_text))
            
            short_url = res.get("shortenedUrl") or res.get("shortUrl") or ""
            if res.get("status") == "success" and short_url:
                inc_stat_db("shorten"); push_short_url_db(short_url)
                await msg.reply_text(f"✅ *Shortened Successfully!*\n\n🔗 *Short URL:* {short_url}\n📄 *Original:* {state['long_url']}")
            else: 
                await msg.reply_text(f"❌ *Error:*\n{res.get('message','Unknown error occurred.')}")
            
            INTERACTIVE.pop(uid, None)
            
    except Exception as e:
        print(f"Error in handle_shorten_step (uid {uid}): {e}")
        await msg.reply_text("An error occurred. Flow cancelled.")
        INTERACTIVE.pop(uid, None)


@app.on_callback_query(filters.regex(r"^alias\|"))
async def alias_cb(_, cq):
    await cq.answer()
    uid = cq.from_user.id; action = cq.data.split("|",1)[1]
    
    state = INTERACTIVE.get(uid)
    if not state or state.get("flow")!="shorten": 
        return await cq.message.edit_text("Session expired.")
    
    if action == "skip":
        await cq.message.edit_text("⏳ Shortening with random alias...")
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, partial(quicklink_shorten, state["long_url"], "")) # Empty alias for random
        
        short_url = res.get("shortenedUrl") or res.get("shortUrl") or ""
        if res.get("status")=="success" and short_url: 
            inc_stat_db("shorten"); push_short_url_db(short_url)
            await cq.message.edit_text(f"✅ *Shortened Successfully!*\n\n🔗 *Short URL:* {short_url}\n📄 *Original:* {state['long_url']}")
        else: 
            await cq.message.edit_text(f"❌ *Error:*\n{res.get('message','Unknown error occurred.')}")
            
        INTERACTIVE.pop(uid, None)

    elif action == "manual":
        state["state"] = "wait_alias_manual"
        await cq.message.edit_text("OK, please send the custom alias you want to use (e.g., `my-link`).")


# ---------- /owner (Owner Info) ----------
@app.on_message(filters.command("owner"))
async def owner_cmd(_, msg: Message):
    register_user_db(msg.from_user.id)
    # Matches your feature list
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📷 Instagram", url="https://instagram.com/deepdey.official")],
            [InlineKeyboardButton("📱 Telegram", url="https://t.me/deepdeyiit")],
            [InlineKeyboardButton("🎥 YouTube (IRL)", url="https://youtube.com/@deepdeyiit")]
        ]
    )
    await msg.reply_text("👑 *Owner:* Deep Dey", reply_markup=kb)


# -------------------------
# Web Dashboard / Ping Page (aiohttp)
# -------------------------
async def web_index(request):
    now_utc = datetime.utcnow()
    # Get current IST time
    ist = now_utc + timedelta(hours=5, minutes=30)
    
    # Get last deploy time (approximated, as we can't know for sure)
    # Let's just show the bot's start time in IST
    start_time_ist = datetime.fromtimestamp(START_TS) + timedelta(hours=5, minutes=30)

    up = uptime_str()
    stats = get_stats_db()
    
    html = f"""
    <html>
    <head>
        <title>QuickLink Bot Status</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f0f0f; color: #f0f0f0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #1a1a1a; padding: 25px; border-radius: 10px; box-shadow: 0 4px 15px rgba(0,0,0,0.4); }}
            h2 {{ color: #00aaff; border-bottom: 2px solid #00aaff; padding-bottom: 5px; }}
            p {{ line-height: 1.6; }}
            a {{ color: #00aaff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .status {{ color: #00cc66; font-weight: bold; }}
            .footer {{ margin-top: 20px; font-size: 0.9em; color: #888; }}
        </style>
    </head>
    <body>
      <div class="container">
        <h2>Quicklink Bot Status</h2>
        <p><span class="status">✅ Quicklink Bot is Live</span></p>
        <p><b>Bot Uptime:</b> {up}</p>
        <p><b>Stats:</b> {stats.get('shorten',0)} Shortens | {stats.get('qrgen',0)} QR Gens | {stats.get('qrscan',0)} QR Scans</p>
        <p><b>Bot Started:</b> {start_time_ist.strftime('%Y-%m-%d %I:%M:%S %p')} IST</p>
        <p><b>Uptime Ping:</b> 📡 UptimeRobot Ping Enabled</p>
        
        <p class="footer">
            Contact Owner: 
            <a href="https://instagram.com/deepdey.official" target="_blank">@deepdey.official</a>
        </p>
      </div>
    </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")


async def run_web():
    app_web = web.Application()
    app_web.add_routes([web.get('/', web_index)])
    runner = web.AppRunner(app_web)
    await runner.setup()
    # Binds to 0.0.0.0 and the PORT from env var
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    try:
        await site.start()
        print(f"Web server started successfully on port {PORT}.")
    except Exception as e:
        print(f"Error starting web server on port {PORT}: {e}")


# -------------------------
# Run both web + bot
# -------------------------
async def main():
    # try ensure storage sync initial
    if mongo_ok:
        try:
            # Ensure all features from the keyboard are in the config
            DB["config"].update_one(
                {"_id":"features"}, 
                {"$setOnInsert":{
                    "shorten":True, "qrgen":True, "qrscan":True, "broadcast":True, "chat":True
                }}, 
                upsert=True
            )
        except Exception:
            pass
    else:
        save_storage_local(LOCAL)

    print("Starting web server and Pyrogram bot...")
    
    try:
        # We start the web server first, as it's needed for Render to not time out
        await run_web()
        print("Web server is running. Now starting Pyrogram bot...")
        await app.start()
        print(f"Bot started successfully! Uptime: {uptime_str()}")
        
        # Keep the script running
        await asyncio.Event().wait()
        
    except Exception as e:
        print(f"CRITICAL ERROR in main: {e}")
    finally:
        # Ensure bot stops if main loop exits
        if app.is_connected:
            await app.stop()
        print("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped manually.")
    except Exception as e:
        print(f"Main loop crashed: {e}")


