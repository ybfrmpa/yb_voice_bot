import os
import hmac
import hashlib
import base64
import asyncio
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import database as db

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRYPTO_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
TELNYX_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_CONN_ID = os.getenv("TELNYX_CONNECTION_ID")
CALLER_ID = os.getenv("CALLER_ID_NUMBER")
BASE_URL = os.getenv("PUBLIC_BASE_URL")
CALL_COST = float(os.getenv("CALL_COST_USD", "0.50"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    bal = await db.get_balance(message.from_user.id)
    text = (
        f"🤖 **YB_VoiceBot Ready**\n\n"
        f"💰 Balance: **${bal:.2f}**\n"
        f"📞 Rate: **${CALL_COST:.2f} per call**\n\n"
        f"**Commands:**\n"
        f"/deposit `<amount>` — Add funds via Crypto\n"
        f"/call `<+1234567890>` `<message>` — Dispatch voice call\n"
        f"/balance — Check credits"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    bal = await db.get_balance(message.from_user.id)
    await message.answer(f"💰 Your balance: `${bal:.2f}`", parse_mode="Markdown")

@dp.message(Command("deposit"))
async def cmd_deposit(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        return await message.answer("Usage: `/deposit 10`", parse_mode="Markdown")
    
    try:
        amount = float(parts[1])
        if amount < 1.0:
            return await message.answer("Minimum deposit is $1.00.")
    except ValueError:
        return await message.answer("Invalid amount format.")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN},
            json={
                "currency_type": "fiat",
                "fiat": "USD",
                "amount": str(amount),
                "description": f"YB_VoiceBot Top-up ({message.from_user.id})",
                "payload": str(message.from_user.id)
            }
        )
        data = resp.json()

    if not data.get("ok"):
        return await message.answer("❌ Failed to create crypto invoice.")

    invoice = data["result"]
    await db.save_invoice(str(invoice["invoice_id"]), message.from_user.id, amount)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Pay ${amount:.2f} via Crypto", url=invoice["bot_invoice_url"])]
    ])
    await message.answer("Click below to complete your payment:", reply_markup=kb)

@dp.message(Command("call"))
async def cmd_call(message: types.Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return await message.answer("Usage: `/call +15551234567 Hello world message`", parse_mode="Markdown")

    destination = parts[1]
    tts_message = parts[2]
    user_id = message.from_user.id

    bal = await db.get_balance(user_id)
    if bal < CALL_COST:
        return await message.answer(f"❌ Low balance (${bal:.2f}). Use /deposit first.")

    await db.update_balance(user_id, -CALL_COST)
    status_msg = await message.answer(f"📡 Dialing `{destination}`...", parse_mode="Markdown")

    client_state_str = f"{user_id}|{status_msg.message_id}|{tts_message}"
    encoded_state = base64.b64encode(client_state_str.encode("utf-8")).decode("utf-8")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.telnyx.com/v2/calls",
            headers={"Authorization": f"Bearer {TELNYX_KEY}"},
            json={
                "connection_id": TELNYX_CONN_ID,
                "to": destination,
                "from": CALLER_ID,
                "webhook_url": f"{BASE_URL}/webhook/telnyx",
                "client_state": encoded_state
            }
        )

    if resp.status_code not in (200, 201):
        await db.update_balance(user_id, CALL_COST)
        await status_msg.edit_text(f"❌ Carrier rejected call: `{resp.text}`", parse_mode="Markdown")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook/cryptopay")
async def cryptopay_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("crypto-pay-api-signature", "")

    secret = hashlib.sha256(CRYPTO_TOKEN.encode()).digest()
    expected_sig = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_sig):
        raise HTTPException(status_code=401, detail="Unauthorized HMAC signature")

    payload = await request.json()
    if payload.get("update_type") == "invoice_paid":
        invoice_id = str(payload["payload"]["invoice_id"])
        result = await db.mark_invoice_paid(invoice_id)
        if result:
            user_id, amount = result
            await bot.send_message(user_id, f"✅ **Deposit Confirmed!** Added `${amount:.2f}` to balance.", parse_mode="Markdown")

    return {"ok": True}

@app.post("/webhook/telnyx")
async def telnyx_webhook(request: Request):
    data = await request.json()
    event = data.get("data", {})
    event_type = event.get("event_type")
    payload = event.get("payload", {})
    call_control_id = payload.get("call_control_id")

    encoded_state = payload.get("client_state", "")
    if not encoded_state:
        return {"ok": True}

    try:
        decoded = base64.b64decode(encoded_state).decode("utf-8")
        user_id, msg_id, tts_text = decoded.split("|", 2)
        user_id, msg_id = int(user_id), int(msg_id)
    except Exception:
        return {"ok": True}

    async with httpx.AsyncClient() as client:
        if event_type == "call.answered":
            await bot.edit_message_text("📞 **Connected.** Prompting interactive menu...", chat_id=user_id, message_id=msg_id, parse_mode="Markdown")
            menu_prompt = f"{tts_text}. Press 1 to confirm, or press 2 to cancel."
            await client.post(
                f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/gather_using_speak",
                headers={"Authorization": f"Bearer {TELNYX_KEY}"},
                json={"payload": menu_prompt, "voice": "female", "language": "en-US", "minimum_digits": 1, "maximum_digits": 1}
            )
        elif event_type == "call.gather.ended":
            digits = payload.get("digits", "")
            if digits == "1":
                resp_text = "Thank you. Confirmed successfully. Goodbye!"
                await bot.send_message(user_id, "✅ User pressed **1 (Confirmed)**.", parse_mode="Markdown")
            elif digits == "2":
                resp_text = "Request cancelled. Goodbye!"
                await bot.send_message(user_id, "❌ User pressed **2 (Cancelled)**.", parse_mode="Markdown")
            else:
                resp_text = "No valid input received. Goodbye!"
                await bot.send_message(user_id, f"⚠️ Invalid input (`{digits}`).", parse_mode="Markdown")

            await client.post(
                f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/speak",
                headers={"Authorization": f"Bearer {TELNYX_KEY}"},
                json={"payload": resp_text, "voice": "female", "language": "en-US"}
            )
        elif event_type == "call.speak.ended":
            await client.post(
                f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/hangup",
                headers={"Authorization": f"Bearer {TELNYX_KEY}"}
            )
        elif event_type == "call.hangup":
            await bot.edit_message_text("✅ **Call Finished & Cleared.**", chat_id=user_id, message_id=msg_id, parse_mode="Markdown")

    return {"ok": True}
