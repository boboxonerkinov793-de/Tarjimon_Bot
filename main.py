import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from deep_translator import GoogleTranslator
from fastapi import FastAPI, Header, HTTPException, Request
from pypinyin import Style, pinyin

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tarjimon")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

API = f"https://api.telegram.org/bot{TOKEN}"
CJK = re.compile(r"[\u3400-\u9fff]")  # xitoy iyeroglif belgilari
MAX_LEN = 4500


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Server ishga tushganda webhook'ni Telegram'ga o'zi ulaydi
    if BASE_URL:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{API}/setWebhook",
                json={
                    "url": f"{BASE_URL}/webhook",
                    "secret_token": SECRET,
                    "allowed_updates": ["message"],
                },
            )
            log.info("setWebhook: %s", r.text)
    yield


app = FastAPI(lifespan=lifespan)


def translate(text: str):
    """Xitoycha bo'lsa -> o'zbekcha, aks holda o'zbekcha -> xitoycha."""
    if CJK.search(text):
        out = GoogleTranslator(source="zh-CN", target="uz").translate(text)
        return out, None
    out = GoogleTranslator(source="uz", target="zh-CN").translate(text)
    py = " ".join(item[0] for item in pinyin(out, style=Style.TONE))
    return out, py


async def send(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text})


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    # Telegram'dan kelmagan so'rovlarni rad etadi
    if x_telegram_bot_api_secret_token != SECRET:
        raise HTTPException(status_code=403)

    try:
        update = await request.json()
        message = update.get("message")
        if not message:
            return {"ok": True}

        # Faqat egasiga javob beradi, boshqalarni e'tiborsiz qoldiradi
        if message.get("from", {}).get("id") != OWNER_ID:
            return {"ok": True}

        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()

        if not text:
            await send(chat_id, "Iltimos, matn yuboring.")
        elif text.startswith("/start"):
            await send(
                chat_id,
                "Salom! O'zbekcha yozing, xitoychaga o'giraman.\n"
                "Xitoycha yozing, o'zbekchaga o'giraman.",
            )
        elif len(text) > MAX_LEN:
            await send(chat_id, f"Matn juda uzun. {MAX_LEN} belgigacha yuboring.")
        else:
            out, py = await asyncio.to_thread(translate, text)
            reply = out if not py else f"{out}\n\n{py}"
            await send(chat_id, reply)
    except Exception:
        log.exception("Xatolik")
        # Telegram qayta-qayta yubormasligi uchun baribir 200 qaytaramiz
    return {"ok": True}
