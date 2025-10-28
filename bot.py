# bot.py
# Pyrogram Telegram bot — interactive QR gen/scan + URL shortener (button-driven)
# Requirements: pyrogram, tgcrypto, pillow, qrcode, requests, python-dotenv, pyzbar

# bot.py
# Pyrogram Telegram bot — interactive QR gen/scan + URL shortener (button-driven)
# Requirements: pyrogram, tgcrypto, pillow, qrcode, requests, python-dotenv, pyzbar

import os
import io
import asyncio
import tempfile
import secrets
import urllib.parse
from functools import partial
from typing import Dict, Any

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    Message,
)
import qrcode
import requests
from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode

load_dotenv()

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
API_ID = int(os.getenv("TG_API_ID", 0)) or None
API_HASH = os.getenv("TG_API_HASH") or None
QUICKLINK_API_KEY = os.getenv("QUICKLINK_API_KEY")
QUICKLINK_ENDPOINT = os.getenv("QUICKLINK_ENDPOINT", "https://quick-link-url-shortener.vercel.app/api/v1/st")

if not BOT_TOKEN:
    raise RuntimeError("Set TG_BOT_TOKEN in .env")

# Per-user state storage for interactive flows
USER_STATE: Dict[int, Dict[str, Any]] = {}

# Helper: temp file helpers
def temp_path_for(prefix: str, suffix: str = ".png"):
    name = f"{prefix}_{secrets.token_hex(8)}{suffix}"
    return os.path.join(tempfile.gettempdir(), name)

async def schedule_delete(path: str, delay: int = 300):
    await asyncio.sleep(delay)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

# Helper: build QR image bytes high quality
def build_qr_png_bytes(data: str, size: int = 1000) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    # Resize to high-quality square
    img = img.resize((size, size), Image.LANCZOS)
    bio = io.BytesIO()
    img.save(bio, format="PNG", optimize=True)
    bio.seek(0)
    return bio.read()

# Helper: attempt local scan using pyzbar
def local_scan_qr(file_path: str):
    try:
        img = Image.open(file_path).convert("RGB")
        decoded = zbar_decode(img)
        results = [d.data.decode("utf-8") for d in decoded if d and d.data]
        return results
    except Exception:
        return []

# Helper: fallback third-party API (api.qrserver.com)
def fallback_scan_qr_api(file_path: str):
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

# Helper: create short url using GET style
def quicklink_shorten(long_url: str, alias: str = "") -> Dict[str, Any]:
    # Use query-string based GET (bot-style)
    params = {
        "api": QUICKLINK_API_KEY,
        "url": long_url,
        "alias": alias or ""
    }
    try:
        resp = requests.get(QUICKLINK_ENDPOINT, params=params, timeout=15)
        try:
            return resp.json()
        except Exception:
            return {"status": "error", "message": "Invalid response from quicklink"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Initialize Pyrogram client
app = Client("quicklink_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------- /start ----------
@app.on_message(filters.command("start"))
async def start_cmd(_, msg: Message):
    text = (
        "👋 *QuickLink Utilities*\n\n"
        "Commands available:\n"
        "/qrgen - Generate QR (interactive)\n"
        "/qrscan - Scan QR (upload within 60s)\n"
        "/shorten - Shorten URL (interactive alias or skip)\n\n"
        "Use buttons & prompts — the bot will guide you step by step."
    )
    await msg.reply_text(text)

# ---------- /qrgen interactive ----------
QR_TYPES = [
    ("Text", "text"),
    ("Link", "link"),
    ("WiFi", "wifi"),
    ("Email", "email"),
    ("Phone", "phone"),
    ("WhatsApp", "whatsapp"),
    ("UPI", "upi"),
    ("SMS", "message"),
]

@app.on_message(filters.command("qrgen"))
async def qrgen_start(_, msg: Message):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"qrtype|{val}")] for label, val in QR_TYPES]
    )
    await msg.reply_text(
        "Choose QR type 👇\n(You will be prompted for fields after selection.)",
        reply_markup=keyboard,
    )

# Callback for qr type selection and following prompts
@app.on_callback_query(filters.regex(r"^qrtype\|"))
async def qrtype_cb(_, cq):
    user_id = cq.from_user.id
    _, qrtype = cq.data.split("|", 1)
    USER_STATE[user_id] = {"flow": "qrgen", "type": qrtype, "data": {}}
    await cq.answer()
    # Based on type, ask first field
    if qrtype in ("text", "link"):
        await cq.message.edit_text(f"Enter the *{qrtype}* content (send as a message):")
    elif qrtype == "wifi":
        await cq.message.edit_text("Enter WiFi *SSID* (network name):")
    elif qrtype == "email":
        await cq.message.edit_text("Enter email *To* address:")
    elif qrtype == "phone":
        await cq.message.edit_text("Enter the phone number (with country code, e.g. +9199...):")
    elif qrtype == "whatsapp":
        await cq.message.edit_text("Enter phone number for WhatsApp (country code, no +):")
    elif qrtype == "upi":
        await cq.message.edit_text("Enter UPI ID (e.g. deep@upi):")
    elif qrtype == "message":
        await cq.message.edit_text("Enter phone number for SMS (with country code):")
    else:
        await cq.message.edit_text("Enter the required content:")

# Generic message handler to drive the interactive qrgen flows
@app.on_message(filters.private & ~filters.edited)
async def generic_private_handler(_, msg: Message):
    user_id = msg.from_user.id
    state = USER_STATE.get(user_id)
    if not state:
        return  # no interactive flow for this user
    if state.get("flow") == "qrgen":
        await handle_qrgen_step(msg, state)
    elif state.get("flow") == "qrscan_wait":
        # this is the image upload during qrscan
        # store message in state for scanning handler
        state["image_msg"] = msg
    elif state.get("flow") == "shorten":
        await handle_shorten_step(msg, state)

# QRGEN step handler
async def handle_qrgen_step(msg: Message, state: Dict):
    user_id = msg.from_user.id
    qrtype = state["type"]
    data = state["data"]

    # Mapping steps per type
    if qrtype in ("text", "link"):
        if "content" not in data:
            data["content"] = msg.text
            # finalize
            qr_text = data["content"]
            await send_generated_qr(msg, qr_text, qrtype)
            USER_STATE.pop(user_id, None)
            return
    elif qrtype == "wifi":
        # WiFi steps: SSID -> Password -> Security choose via buttons
        if "ssid" not in data:
            data["ssid"] = msg.text
            await msg.reply_text("Enter WiFi *Password* (send plain text, send `-` for open networks):")
            return
        if "password" not in data:
            data["password"] = msg.text
            # security options as buttons
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔒 WPA/WPA2", callback_data="wifisec|WPA")],
                [InlineKeyboardButton("🔑 WEP", callback_data="wifisec|WEP")],
                [InlineKeyboardButton("🔓 NONE (Open)", callback_data="wifisec|NONE")],
            ])
            await msg.reply_text("Select WiFi security type:", reply_markup=kb)
            return
    elif qrtype == "email":
        if "to" not in data:
            data["to"] = msg.text
            await msg.reply_text("Enter *Subject* for the email (send text or `-` to skip):")
            return
        if "subject" not in data:
            data["subject"] = msg.text if msg.text != "-" else ""
            await msg.reply_text("Enter *Body* for the email (send text or `-` to skip):")
            return
        if "body" not in data:
            data["body"] = msg.text if msg.text != "-" else ""
            mailto = f"mailto:{data['to']}?subject={urllib.parse.quote(data['subject'])}&body={urllib.parse.quote(data['body'])}"
            await send_generated_qr(msg, mailto, "email")
            USER_STATE.pop(user_id, None)
            return
    elif qrtype == "phone":
        if "phone" not in data:
            data["phone"] = msg.text
            tel = f"tel:{data['phone']}"
            await send_generated_qr(msg, tel, "phone")
            USER_STATE.pop(user_id, None)
            return
    elif qrtype == "whatsapp":
        if "number" not in data:
            data["number"] = msg.text
            await msg.reply_text("Enter message text to prefill (or send `-` to skip):")
            return
        if "message" not in data:
            data["message"] = msg.text if msg.text != "-" else ""
            wa = f"https://wa.me/{data['number']}?text={urllib.parse.quote(data['message'])}"
            await send_generated_qr(msg, wa, "whatsapp")
            USER_STATE.pop(user_id, None)
            return
    elif qrtype == "upi":
        if "pa" not in data:
            data["pa"] = msg.text
            await msg.reply_text("Enter payee name (or `-` to skip):")
            return
        if "pn" not in data:
            data["pn"] = msg.text if msg.text != "-" else ""
            await msg.reply_text("Enter amount (or `-` to skip):")
            return
        if "am" not in data:
            data["am"] = msg.text if msg.text != "-" else ""
            upi = f"upi://pay?pa={urllib.parse.quote(data['pa'])}&pn={urllib.parse.quote(data['pn'])}&am={urllib.parse.quote(data['am'])}"
            await send_generated_qr(msg, upi, "upi")
            USER_STATE.pop(user_id, None)
            return
    elif qrtype == "message":
        if "phone" not in data:
            data["phone"] = msg.text
            await msg.reply_text("Enter SMS body text:")
            return
        if "text" not in data:
            data["text"] = msg.text
            smsto = f"SMSTO:{data['phone']}:{data['text']}"
            await send_generated_qr(msg, smsto, "sms")
            USER_STATE.pop(user_id, None)
            return

# Callback for wifi security selection
@app.on_callback_query(filters.regex(r"^wifisec\|"))
async def wifisec_cb(_, cq):
    user_id = cq.from_user.id
    await cq.answer()
    _, sec = cq.data.split("|", 1)
    state = USER_STATE.get(user_id)
    if not state or state.get("flow") != "qrgen":
        return await cq.message.edit_text("Session expired or invalid.")
    state["data"]["security"] = sec
    # build wifi string now
    ssid = state["data"]["ssid"]
    pwd = state["data"]["password"]
    sec_value = sec if sec != "NONE" else ""
    wifi_text = f"WIFI:T:{sec_value};S:{ssid};P:{pwd};;"
    await send_generated_qr(cq.message, wifi_text, "wifi")
    USER_STATE.pop(user_id, None)

# send generated QR helper (message or chat)
async def send_generated_qr(trigger_msg: Message, qr_text: str, qrtype: str):
    user = trigger_msg.from_user
    chat_id = trigger_msg.chat.id
    # loading message
    sent = await trigger_msg.reply_text("🔧 Generating QR... please wait")
    png_bytes = build_qr_png_bytes(qr_text, size=1000)
    bio = io.BytesIO(png_bytes)
    bio.name = "qr.png"
    await asyncio.sleep(0.4)
    await sent.edit_text("✅ Done — sending QR")
    await trigger_msg.reply_photo(photo=bio, caption=f"Type: {qrtype}\nEncoded: `{qr_text}`")
    # schedule deletion of the file-less temporary buffer is handled by GC; if file was created, schedule deletion

# ---------- /qrscan flow ----------
@app.on_message(filters.command("qrscan"))
async def qrscan_start(_, msg: Message):
    user_id = msg.from_user.id
    USER_STATE[user_id] = {"flow": "qrscan_wait", "image_msg": None}
    prompt = await msg.reply_text("📸 Please send the QR image within *60 seconds*. I will try to decode locally first.")
    try:
        # Wait for next message from same user that contains photo
        def check_photo(m):
            return m.from_user.id == user_id and (m.photo or m.document)
        got = await app.listen(chat_id=msg.chat.id, filters=filters.photo | filters.document, timeout=60)
    except asyncio.TimeoutError:
        USER_STATE.pop(user_id, None)
        return await prompt.edit_text("⏰ Timeout — you didn't send an image.")
    # download file
    file_msg: Message = got
    fpath = temp_path_for("qrscan", ".png")
    await file_msg.download(file_name=fpath)
    # try local scan
    await prompt.edit_text("🔎 Scanning locally...")
    local_res = await asyncio.get_event_loop().run_in_executor(None, partial(local_scan_qr, fpath))
    if local_res:
        USER_STATE.pop(user_id, None)
        await prompt.edit_text(f"✅ Local decode successful:\n`{chr(10).join(local_res)}`")
        asyncio.create_task(schedule_delete(fpath, delay=300))
        return
    # else offer third-party
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes — use external scan (GoQR)", callback_data="qrfallback|yes")],
        [InlineKeyboardButton("❌ No — cancel", callback_data="qrfallback|no")],
    ])
    await prompt.edit_text("❌ Local decoding failed. Use external API to try decoding?", reply_markup=kb)
    # store path in state for callback
    USER_STATE[user_id]["pending_file"] = fpath
    asyncio.create_task(schedule_delete(fpath, delay=300))

@app.on_callback_query(filters.regex(r"^qrfallback\|"))
async def qrfallback_cb(_, cq):
    await cq.answer()
    user_id = cq.from_user.id
    action = cq.data.split("|", 1)[1]
    state = USER_STATE.get(user_id)
    if not state or "pending_file" not in state:
        return await cq.message.edit_text("Session expired or file missing.")
    fpath = state["pending_file"]
    if action == "no":
        USER_STATE.pop(user_id, None)
        return await cq.message.edit_text("Cancelled decoding.")
    # do fallback decode (third-party)
    await cq.message.edit_text("🔁 Trying external decoder (GoQR / api.qrserver.com)...")
    loop = asyncio.get_event_loop()
    res = await loop.run_in_executor(None, partial(fallback_scan_qr_api, fpath))
    USER_STATE.pop(user_id, None)
    if res:
        await cq.message.edit_text(f"✅ External decode success:\n`{chr(10).join(res)}`")
    else:
        await cq.message.edit_text("❌ External decode also failed.")

# ---------- /shorten interactive ----------
@app.on_message(filters.command("shorten"))
async def shorten_start(_, msg: Message):
    user_id = msg.from_user.id
    USER_STATE[user_id] = {"flow": "shorten", "state": "wait_url"}
    await msg.reply_text("🔗 Send the long URL you want to shorten:")

async def handle_shorten_step(msg: Message, state: Dict):
    user_id = msg.from_user.id
    st = state.get("state")
    if st == "wait_url":
        text = (msg.text or "").strip()
        if not text.startswith("http"):
            await msg.reply_text("Please send a valid URL starting with http/https.")
            return
        state["long_url"] = text
        # ask for alias with Skip button
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Skip (use random)", callback_data="alias|skip")],
        ])
        state["state"] = "wait_alias"
        await msg.reply_text("Optional: send an alias (single word). Or press Skip.", reply_markup=kb)
        return
    if st == "wait_alias":
        alias_text = (msg.text or "").strip()
        if alias_text == "":
            # user may have pressed skip button instead of typing; handle via callback separately
            await msg.reply_text("No alias provided — press Skip button or type an alias.")
            return
        # proceed with given alias
        long_url = state["long_url"]
        await msg.reply_text("🔧 Shortening... please wait")
        result = await asyncio.get_event_loop().run_in_executor(None, partial(quicklink_shorten, long_url, alias_text))
        USER_STATE.pop(user_id, None)
        if result.get("status") == "success":
            short = result.get("shortenedUrl") or result.get("shortUrl") or result.get("shortenedUrl")
            await msg.reply_text(f"✅ Shortened URL:\n{short}")
        else:
            await msg.reply_text(f"❌ Error: {result.get('message','Unknown error')}")

# callback for alias skip or button
@app.on_callback_query(filters.regex(r"^alias\|"))
async def alias_cb(_, cq):
    await cq.answer()
    user_id = cq.from_user.id
    action = cq.data.split("|", 1)[1]
    state = USER_STATE.get(user_id)
    if not state or state.get("flow") != "shorten":
        return await cq.message.edit_text("Session expired or invalid.")
    if action == "skip":
        long_url = state.get("long_url")
        await cq.message.edit_text("🔧 Shortening (random alias)...")
        result = await asyncio.get_event_loop().run_in_executor(None, partial(quicklink_shorten, long_url, ""))
        USER_STATE.pop(user_id, None)
        if result.get("status") == "success":
            short = result.get("shortenedUrl") or result.get("shortUrl")
            await cq.message.edit_text(f"✅ Shortened URL:\n{short}")
        else:
            await cq.message.edit_text(f"❌ Error: {result.get('message','Unknown error')}")

# ---------- run ----------
if __name__ == "__main__":
    print("Bot starting...")
    app.run()
