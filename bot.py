# bot.py
# Pyrogram Telegram bot — interactive QR gen/scan + URL shortener (button-driven)
# Requirements: pyrogram, tgcrypto, pillow, qrcode, requests, python-dotenv, pyzbar

# bot.py
# Pyrogram Telegram bot — interactive QR gen/scan + URL shortener (button-driven)
# Requirements: pyrogram, tgcrypto, pillow, qrcode, requests, python-dotenv, pyzbar

# bot.py
# Single-file Pyrogram bot — QR gen/scan, URL shortener, admin controls, ChatBase chat, web status.
# Uses MongoDB (db: quicklink_bot) if provided, else falls back to local JSON storage.

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
load_dotenv("/etc/secrets/.env")
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
API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH")
QUICKLINK_API_KEY = os.getenv("QUICKLINK_API_KEY")
QUICKLINK_ENDPOINT = os.getenv("QUICKLINK_ENDPOINT", "https://quick-link-url-shortener.vercel.app/api/v1/st")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))
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
        print("Warning: Mongo not available:", e)
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
    except Exception:
        pass
    return default


def save_storage_local(data: Dict[str, Any]):
    try:
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


LOCAL = load_storage_local()


def register_user_db(user_id: int):
    if mongo_ok:
        try:
            DB["users"].update_one({"_id": user_id}, {"$setOnInsert": {"_id": user_id, "added": time.time()}}, upsert=True)
        except Exception:
            pass
    else:
        if user_id not in LOCAL["users"]:
            LOCAL["users"].append(user_id)
            save_storage_local(LOCAL)


def inc_stat_db(key: str, n: int = 1):
    if mongo_ok:
        try:
            DB["stats"].update_one({"_id": "global"}, {"$inc": {f"counts.{key}": n}}, upsert=True)
        except Exception:
            pass
    else:
        LOCAL["stats"][key] = LOCAL["stats"].get(key, 0) + n
        save_storage_local(LOCAL)


def push_short_url_db(url: str):
    ts = int(time.time())
    if mongo_ok:
        try:
            DB["urls"].insert_one({"url": url, "ts": ts})
        except Exception:
            pass
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
        except Exception:
            pass
    return LOCAL["stats"]


def get_last_urls_db(n: int = 5) -> List[Dict[str, Any]]:
    if mongo_ok:
        try:
            docs = DB["urls"].find().sort("ts", -1).limit(n)
            return [{"url": d.get("url"), "ts": d.get("ts")} for d in docs]
        except Exception:
            pass
    return LOCAL["last_urls"][:n]


def get_all_users_db() -> List[int]:
    if mongo_ok:
        try:
            return [d["_id"] for d in DB["users"].find({}, {"_id": 1})]
        except Exception:
            pass
    return LOCAL["users"][:]


def set_feature_db(key: str, val: bool):
    if mongo_ok:
        try:
            DB["config"].update_one({"_id": "features"}, {"$set": {key: val}}, upsert=True)
        except Exception:
            pass
    else:
        LOCAL["features"][key] = val
        save_storage_local(LOCAL)


def get_features_db() -> Dict[str, bool]:
    if mongo_ok:
        try:
            doc = DB["config"].find_one({"_id": "features"}) or {}
            return {**{"shorten": True, "qrgen": True, "qrscan": True, "broadcast": True}, **doc}
        except Exception:
            pass
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
    except Exception:
        pass


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
    except Exception:
        return []


def fallback_scan_qr_api(file_path: str) -> List[str]:
    try:
        with open(file_path, "rb") as f:
            files = {"file": ("qr.png", f, "image/png")}
            r = requests.post("https://api.qrserver.com/v1/read-qr-code/", files=files, timeout=30)
            j = r.json()
            texts = []
            for item in j:
                for symbol in item.get("symbol", []):
                    data = symbol.get("data")
                    if data:
                        texts.append(data)
            return texts
    except Exception:
        return []


# -------------------------
# QuickLink shorten helper
# -------------------------
def quicklink_shorten(long_url: str, alias: str = "") -> Dict[str, Any]:
    params = {"api": QUICKLINK_API_KEY, "url": long_url, "alias": alias or ""}
    try:
        r = requests.get(QUICKLINK_ENDPOINT, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        return {"status": "error", "message": r.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# -------------------------
# Chatbase support (simple)
# -------------------------
def chatbase_query(user_text: str) -> str:
    if not CHATBASE_API_KEY or not CHATBASE_BOT_ID:
        return "Chatbase not configured."
    payload = {"messages": [{"content": user_text, "role": "user"}], "chatbotId": CHATBASE_BOT_ID}
    headers = {"Authorization": f"Bearer {CHATBASE_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post("https://www.chatbase.co/api/v1/chat", headers=headers, json=payload, timeout=20)
        jr = r.json()
        # Chatbase response shape can vary; try common keys
        text = jr.get("text") or jr.get("message") or jr.get("response") or ""
        if not text:
            # try to parse choices/messages
            if "messages" in jr and isinstance(jr["messages"], list) and jr["messages"]:
                return jr["messages"][-1].get("content","")
        return text or "No response from Chatbase."
    except Exception as e:
        return f"Chatbase error: {e}"


# -------------------------
# Pyrogram client
# -------------------------
app = Client("quicklink_bot", bot_token=BOT_TOKEN, api_id=API_ID or None, api_hash=API_HASH or None)

INTERACTIVE: Dict[int, Dict[str, Any]] = {}

# format uptime: YYYY:MM:DD:HH:MM:SS:ms
START_TS = time.time()


def uptime_str():
    now = datetime.utcnow()
    ms = int(now.microsecond / 1000)
    return now.strftime(f"%Y:%m:%d:%H:%M:%S:{ms:03d}")


# ---------- /start ----------
@app.on_message(filters.command("start"))
async def start_cmd(_, msg: Message):
    register_user_db(msg.from_user.id)
    features = get_features_db()
    txt = (
        f"👋 *QuickLink Utilities Bot*\n\n"
        f"Commands available:\n"
        f"/qrgen - Generate QR (interactive)\n"
        f"/qrscan - Scan QR (send image within 60s)\n"
        f"/shorten - Shorten URL (interactive alias or skip)\n"
        f"/state - Show bot stats\n"
        f"/chat - Ask support (Chatbase)\n"
        f"/owner - Owner info\n\n"
        f"Uptime: `{uptime_str()}`"
    )
    await msg.reply_text(txt)


# ---------- /state ----------
@app.on_message(filters.command("state"))
async def state_cmd(_, msg: Message):
    register_user_db(msg.from_user.id)
    stats = get_stats_db()
    last5 = get_last_urls_db(5)
    last_text = "\n".join([f"- {i['url']}" for i in last5]) if last5 else "No recent URLs"
    txt = (
        f"📊 *Bot State*\n\n"
        f"Shortens: {stats.get('shorten',0)}\n"
        f"QR Generated: {stats.get('qrgen',0)}\n"
        f"QR Scanned: {stats.get('qrscan',0)}\n\n"
        f"Last 5 shortened URLs:\n{last_text}"
    )
    await msg.reply_text(txt)


# ---------- /chat (Chatbase) ----------
@app.on_message(filters.command("chat"))
async def chat_cmd(_, msg: Message):
    register_user_db(msg.from_user.id)
    if len(msg.command) < 2:
        return await msg.reply_text("Usage: /chat <your message>")
    user_msg = msg.text.split(" ",1)[1]
    await msg.reply_text("💬 Asking support...", quote=True)
    res = await asyncio.get_event_loop().run_in_executor(None, partial(chatbase_query, user_msg))
    await msg.reply_text(f"🧠 Support: {res}")


# ---------- Admin /admin toggles ----------
def feature_keyboard():
    f = get_features_db()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Shorten: {'ON' if f.get('shorten') else 'OFF'}", callback_data="ft|shorten")],
        [InlineKeyboardButton(f"QRGen: {'ON' if f.get('qrgen') else 'OFF'}", callback_data="ft|qrgen")],
        [InlineKeyboardButton(f"QRScan: {'ON' if f.get('qrscan') else 'OFF'}", callback_data="ft|qrscan")],
        [InlineKeyboardButton(f"Broadcast: {'ON' if f.get('broadcast') else 'OFF'}", callback_data="ft|broadcast")],
    ])
    return kb


@app.on_message(filters.command("admin"))
async def admin_cmd(_, msg: Message):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply_text("❌ You are not the owner.")
    await msg.reply_text("Admin control panel — toggle features:", reply_markup=feature_keyboard())


@app.on_callback_query(filters.regex(r"^ft\|"))
async def feature_toggle(_, cq):
    user_id = cq.from_user.id
    if user_id != OWNER_ID:
        return await cq.answer("Not allowed", show_alert=True)
    key = cq.data.split("|",1)[1]
    # flip
    current = get_features_db().get(key, True)
    set_feature_db(key, not current)
    await cq.message.edit_text("Feature toggled.", reply_markup=feature_keyboard())
    await cq.answer("Toggled")


# ---------- /broadcast (owner only) with paced sending to finish within ~300s ----------
@app.on_message(filters.command("broadcast"))
async def broadcast_start(_, msg: Message):
    if msg.from_user.id != OWNER_ID:
        return await msg.reply_text("❌ Only owner can broadcast.")
    if not get_features_db().get("broadcast", True):
        return await msg.reply_text("Broadcast feature disabled by owner.")
    await msg.reply_text("Send the broadcast content (text or photo). You have 5 minutes to send it.")

    try:
        bmsg = await app.listen(msg.chat.id, timeout=300)
    except asyncio.TimeoutError:
        return await msg.reply_text("Timeout. Broadcast cancelled.")

    text = bmsg.caption or bmsg.text or ""
    photo_path = None
    if bmsg.photo:
        photo_path = await bmsg.download()
    # confirm
    INTERACTIVE[msg.from_user.id] = {"bc_text": text, "bc_photo": photo_path}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm send", callback_data="bc|confirm")],
                               [InlineKeyboardButton("❌ Cancel", callback_data="bc|cancel")]])
    await msg.reply_text("Preview saved. Confirm to send to all users (will complete within ~5 minutes).", reply_markup=kb)


@app.on_callback_query(filters.regex(r"^bc\|"))
async def broadcast_cb(_, cq):
    uid = cq.from_user.id
    if uid != OWNER_ID:
        return await cq.answer("Not allowed", show_alert=True)
    action = cq.data.split("|",1)[1]
    state = INTERACTIVE.get(uid)
    if not state:
        return await cq.answer("Nothing to send", show_alert=True)
    if action == "cancel":
        INTERACTIVE.pop(uid, None)
        return await cq.message.edit_text("Broadcast cancelled.")
    await cq.message.edit_text("Broadcast starting...")

    users = get_all_users_db()
    total = len(users)
    if total == 0:
        INTERACTIVE.pop(uid, None)
        return await cq.message.edit_text("No registered users to send.")

    # compute base interval so total_time ~ 300 seconds
    base_interval = 300.0 / total
    # clamp to reasonable bounds to avoid insane tiny waits
    interval = max(0.05, min(2.0, base_interval))  # min 50ms, max 2s
    # send with handling for FloodWait
    sent = 0
    failed = 0
    start_t = time.time()
    for i, user in enumerate(users):
        try:
            if state["bc_photo"]:
                await app.send_photo(user, photo=state["bc_photo"], caption=state["bc_text"])
            else:
                await app.send_message(user, state["bc_text"])
            sent += 1
        except FloodWait as e:
            wait = min(int(e.x) + 2, 300)
            await asyncio.sleep(wait)
            try:
                if state["bc_photo"]:
                    await app.send_photo(user, photo=state["bc_photo"], caption=state["bc_text"])
                else:
                    await app.send_message(user, state["bc_text"])
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        # small jitter
        await asyncio.sleep(interval + random.uniform(0, 0.25))
    end_t = time.time()
    INTERACTIVE.pop(uid, None)
    # save last broadcast ts
    if mongo_ok:
        try:
            DB["meta"].update_one({"_id":"last_broadcast"}, {"$set":{"ts":int(time.time())}}, upsert=True)
        except Exception:
            pass
    else:
        LOCAL["last_broadcast"] = int(time.time()); save_storage_local(LOCAL)
    await cq.message.edit_text(f"Broadcast completed. Sent: {sent}, Failed: {failed}. Time: {int(end_t-start_t)}s")


# ---------- QR generation/interactive flows ----------
QR_TYPES = [("Text","text"),("Link","link"),("WiFi","wifi"),("Email","email"),("Phone","phone"),
            ("WhatsApp","whatsapp"),("UPI","upi"),("SMS","message")]

@app.on_message(filters.command("qrgen"))
async def qrgen_start(_, msg: Message):
    if not get_features_db().get("qrgen", True):
        return await msg.reply_text("QR generation is disabled by owner.")
    register_user_db(msg.from_user.id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"qrtype|{val}") ] for label,val in QR_TYPES])
    await msg.reply_text("Choose QR type:", reply_markup=kb)


@app.on_callback_query(filters.regex(r"^qrtype\|"))
async def qrtype_cb(_, cq):
    user_id = cq.from_user.id
    _, qrtype = cq.data.split("|",1)
    INTERACTIVE[user_id] = {"flow":"qrgen", "type": qrtype, "data": {}}
    await cq.answer()
    prompts = {"text":"Send text:", "link":"Send link (https://...)", "wifi":"Send SSID", "email":"Send 'To' email",
               "phone":"Send phone (+country)", "whatsapp":"Send phone (no +)", "upi":"Send UPI ID", "message":"Send phone for SMS"}
    await cq.message.edit_text(prompts.get(qrtype,"Send input:"))


@app.on_message(filters.private & ~filters.edited_message)
async def private_flow_handler(_, msg: Message):
    uid = msg.from_user.id
    if uid not in INTERACTIVE:
        return
    state = INTERACTIVE[uid]
    if state.get("flow") == "qrgen":
        await handle_qrgen_step(msg, state)
    elif state.get("flow") == "shorten":
        await handle_shorten_step(msg, state)
    elif state.get("flow") == "qrscan_wait":
        state["file_msg"] = msg


async def handle_qrgen_step(msg: Message, state: Dict):
    uid = msg.from_user.id
    typ = state["type"]; d = state["data"]
    if typ in ("text","link"):
        if "content" not in d:
            d["content"] = msg.text or ""
            qr_text = d["content"].strip()
            png = build_qr_png_bytes(qr_text, size=1000)
            await msg.reply_photo(png, caption=f"Type:{typ}\nEncoded:`{qr_text}`")
            inc_stat_db("qrgen"); push_short_url_db("")  # only bump stat; last_urls unchanged
            return INTERACTIVE.pop(uid, None)
    if typ == "wifi":
        if "ssid" not in d:
            d["ssid"] = msg.text or ""; await msg.reply_text("Send Password (or - for open)"); return
        if "password" not in d:
            d["password"] = msg.text or ""
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔒 WPA/WPA2", callback_data="wifisec|WPA")],
                                       [InlineKeyboardButton("🔑 WEP", callback_data="wifisec|WEP")],
                                       [InlineKeyboardButton("🔓 NONE", callback_data="wifisec|NONE")]])
            await msg.reply_text("Choose security:", reply_markup=kb); return
    if typ == "email":
        if "to" not in d:
            d["to"] = msg.text or ""; await msg.reply_text("Enter Subject (or - skip)"); return
        if "subject" not in d:
            d["subject"] = msg.text if (msg.text and msg.text!="-") else ""; await msg.reply_text("Enter Body (or - skip)"); return
        if "body" not in d:
            d["body"] = msg.text if (msg.text and msg.text!="-") else ""
            mailto = f"mailto:{d['to']}?subject={urllib.parse.quote(d['subject'])}&body={urllib.parse.quote(d['body'])}"
            png = build_qr_png_bytes(mailto, size=1000); await msg.reply_photo(png, caption=f"Type:email\nEncoded:`{mailto}`")
            inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
    if typ == "phone":
        d["phone"] = msg.text or ""; tel = f"tel:{d['phone']}"; png = build_qr_png_bytes(tel,1000)
        await msg.reply_photo(png, caption=f"Type:phone\nEncoded:`{tel}`"); inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
    if typ == "whatsapp":
        if "number" not in d:
            d["number"] = msg.text or ""; await msg.reply_text("Enter message (or - skip)"); return
        if "message" not in d:
            d["message"] = msg.text if (msg.text and msg.text!="-") else ""
            wa = f"https://wa.me/{d['number']}?text={urllib.parse.quote(d['message'])}"
            png = build_qr_png_bytes(wa,1000); await msg.reply_photo(png, caption=f"Type:whatsapp\nEncoded:`{wa}`")
            inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
    if typ == "upi":
        if "pa" not in d:
            d["pa"] = msg.text or ""; await msg.reply_text("Enter payee name (or - skip)"); return
        if "pn" not in d:
            d["pn"] = msg.text if (msg.text and msg.text!="-") else ""; await msg.reply_text("Enter amount (or - skip)"); return
        if "am" not in d:
            d["am"] = msg.text if (msg.text and msg.text!="-") else ""
            upi = f"upi://pay?pa={urllib.parse.quote(d['pa'])}&pn={urllib.parse.quote(d['pn'])}&am={urllib.parse.quote(d['am'])}"
            png = build_qr_png_bytes(upi,1000); await msg.reply_photo(png, caption=f"Type:upi\nEncoded:`{upi}`"); inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)
    if typ == "message":
        if "phone" not in d:
            d["phone"] = msg.text or ""; await msg.reply_text("Enter SMS text"); return
        if "text" not in d:
            d["text"] = msg.text or ""
            smsto = f"SMSTO:{d['phone']}:{d['text']}"
            png = build_qr_png_bytes(smsto,1000); await msg.reply_photo(png, caption=f"Type:sms\nEncoded:`{smsto}`"); inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)


@app.on_callback_query(filters.regex(r"^wifisec\|"))
async def wifisec_cb(_, cq):
    uid = cq.from_user.id; await cq.answer()
    _, sec = cq.data.split("|",1)
    st = INTERACTIVE.get(uid)
    if not st or st.get("flow")!="qrgen": return await cq.message.edit_text("Session expired.")
    d = st["data"]; ssid = d.get("ssid",""); pwd = d.get("password","")
    secv = sec if sec!="NONE" else ""
    wifi_text = f"WIFI:T:{secv};S:{ssid};P:{pwd};;"
    png = build_qr_png_bytes(wifi_text,1000)
    await cq.message.edit_text("✅ Generated QR — sending..."); await cq.message.reply_photo(png, caption=f"Type:wifi\nEncoded:`{wifi_text}`")
    inc_stat_db("qrgen"); return INTERACTIVE.pop(uid, None)


# ---------- /qrscan ----------
@app.on_message(filters.command("qrscan"))
async def qrscan_start(_, msg: Message):
    if not get_features_db().get("qrscan", True):
        return await msg.reply_text("QR scan disabled by owner.")
    register_user_db(msg.from_user.id)
    uid = msg.from_user.id; INTERACTIVE[uid] = {"flow":"qrscan_wait"}
    prompt = await msg.reply_text("📸 Send QR image within 60s. I'll try local decode first.")
    try:
        got = await app.listen(chat_id=msg.chat.id, timeout=60)
    except asyncio.TimeoutError:
        INTERACTIVE.pop(uid, None); return await prompt.edit_text("⏰ Timeout — no image.")
    if not (got.photo or got.document):
        INTERACTIVE.pop(uid, None); return await prompt.edit_text("No image.")
    fpath = temp_path_for("qrscan",".png"); await got.download(file_name=fpath)
    await prompt.edit_text("🔎 Scanning locally...")
    loop = asyncio.get_event_loop()
    local_res = await loop.run_in_executor(None, partial(local_scan_qr, fpath))
    inc_stat_db("qrscan"); save_storage_local(LOCAL)
    if local_res:
        INTERACTIVE.pop(uid, None); await prompt.edit_text(f"✅ Local decode:\n`{chr(10).join(local_res)}`"); asyncio.create_task(schedule_delete(fpath,delay=300)); return
    # fallback offer
    INTERACTIVE[uid]["pending_file"] = fpath
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes — external (api.qrserver.com)", callback_data="qrfb|yes")],
                               [InlineKeyboardButton("❌ No — cancel", callback_data="qrfb|no")]])
    await prompt.edit_text("Local failed. Use external decoder?", reply_markup=kb)


@app.on_callback_query(filters.regex(r"^qrfb\|"))
async def qrfallback_cb(_, cq):
    await cq.answer()
    uid = cq.from_user.id; action = cq.data.split("|",1)[1]
    st = INTERACTIVE.get(uid)
    if not st or "pending_file" not in st: return await cq.message.edit_text("Session expired.")
    fpath = st["pending_file"]
    if action == "no":
        INTERACTIVE.pop(uid, None)
        try:
            os.remove(fpath)
        except Exception:
            pass
 
        except: pass; return await cq.message.edit_text("Cancelled.")
    await cq.message.edit_text("🔁 External decoding...")
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, partial(fallback_scan_qr_api, fpath))
    INTERACTIVE.pop(uid, None)
    if res: await cq.message.edit_text(f"✅ External decode:\n`{chr(10).join(res)}`")
    else: await cq.message.edit_text("❌ External decode failed.")
    asyncio.create_task(schedule_delete(fpath,delay=300))


# ---------- /shorten ----------
@app.on_message(filters.command("shorten"))
async def shorten_start(_, msg: Message):
    if not get_features_db().get("shorten", True):
        return await msg.reply_text("Shorten disabled by owner.")
    register_user_db(msg.from_user.id)
    uid = msg.from_user.id; INTERACTIVE[uid] = {"flow":"shorten","state":"wait_url"}
    await msg.reply_text("🔗 Send the long URL (http/https):")


async def handle_shorten_step(msg: Message, state: Dict):
    uid = msg.from_user.id; st = state.get("state")
    if st == "wait_url":
        text = (msg.text or "").strip()
        if not text.startswith("http"): await msg.reply_text("Send valid URL."); return
        state["long_url"] = text; state["state"] = "wait_alias"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Skip (random)", callback_data="alias|skip")]])
        await msg.reply_text("Optional: send alias or press Skip", reply_markup=kb); return
    if st == "wait_alias":
        alias_text = (msg.text or "").strip()
        if alias_text == "": await msg.reply_text("Type alias or press Skip."); return
        await msg.reply_text("🔧 Shortening...")
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, partial(quicklink_shorten, state["long_url"], alias_text))
        inc_stat_db("shorten"); push_short_url_db(res.get("shortenedUrl") or res.get("shortUrl") or "")
        if res.get("status") == "success": await msg.reply_text(f"✅ Shortened:\n{res.get('shortenedUrl') or res.get('shortUrl')}")
        else: await msg.reply_text(f"❌ Error: {res.get('message','Unknown')}")
        INTERACTIVE.pop(uid, None)


@app.on_callback_query(filters.regex(r"^alias\|"))
async def alias_cb(_, cq):
    await cq.answer()
    uid = cq.from_user.id; action = cq.data.split("|",1)[1]
    state = INTERACTIVE.get(uid)
    if not state or state.get("flow")!="shorten": return await cq.message.edit_text("Session expired.")
    if action == "skip":
        await cq.message.edit_text("🔧 Shortening random alias...")
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, partial(quicklink_shorten, state["long_url"], ""))
        inc_stat_db("shorten"); push_short_url_db(res.get("shortenedUrl") or res.get("shortUrl") or "")
        if res.get("status")=="success": await cq.message.edit_text(f"✅ Shortened:\n{res.get('shortenedUrl') or res.get('shortUrl')}")
        else: await cq.message.edit_text(f"❌ Error: {res.get('message','Unknown')}")
        INTERACTIVE.pop(uid, None)


# -------------------------
# Small web server for uptime Ping (aiohttp)
# -------------------------
async def web_index(request):
    # today 16:00 IST last deploy
    now_utc = datetime.utcnow()
    ist = now_utc + timedelta(hours=5, minutes=30)
    last_deploy = ist.replace(hour=16, minute=0, second=0, microsecond=0)
    up = uptime_str()
    # show stats from DB/local
    stats = get_stats_db()
    last5 = get_last_urls_db(5)
    last5_html = "<br>".join([f"{i['url']}" for i in last5]) if last5 else "No recent URLs"
    html = f"""
    <html><head><title>QuickLink Bot Status</title></head><body style="font-family:Arial;background:#011627;color:#e6eef8;padding:20px;">
      <h2>QuickLink Bot — Status</h2>
      <p>Uptime: {up}</p>
      <p>Shortens: {stats.get('shorten',0)} | QRGen: {stats.get('qrgen',0)} | QRScan: {stats.get('qrscan',0)}</p>
      <p>Last deploy (today 16:00 IST): {last_deploy.strftime('%Y-%m-%d %H:%M:%S')}</p>
      <p>Contact: <a href="https://instagram.com/deepdey.official" target="_blank">@deepdey.official</a></p>
      <p>Owner: <a href="https://t.me/deepdeyiit" target="_blank">@deepdeyiit</a></p>
      <h3>Last 5 Shortened URLs</h3><p>{last5_html}</p>
    </body></html>
    """
    return web.Response(text=html, content_type="text/html")


async def run_web():
    app_web = web.Application()
    app_web.add_routes([web.get('/', web_index)])
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()


# -------------------------
# Run both web + bot
# -------------------------
async def main():
    # try ensure storage sync initial
    if mongo_ok:
        # ensure config doc exists
        try:
            DB["config"].update_one({"_id":"features"}, {"$setOnInsert":{"shorten":True,"qrgen":True,"qrscan":True,"broadcast":True}}, upsert=True)
        except Exception:
            pass
    else:
        save_storage_local(LOCAL)

    await run_web()
    await app.start()
    print("Bot and web server started. Uptime:", uptime_str())
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopping...")
