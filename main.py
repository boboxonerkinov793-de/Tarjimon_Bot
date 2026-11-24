import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from deep_translator import GoogleTranslator, MyMemoryTranslator
from fastapi import FastAPI, Header, HTTPException, Request
from pypinyin import Style, pinyin

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # token logga tushmasligi uchun
log = logging.getLogger("tarjimon")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

# Ixtiyoriy: agar shu o'zgaruvchi qo'yilsa, tarjima Claude orqali bajariladi (sifatliroq)
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")

API = f"https://api.telegram.org/bot{TOKEN}"
CJK = re.compile(r"[\u3400-\u9fff]")  # xitoy iyeroglif belgilari
MAX_LEN = 4500


@asynccontextmanager
async def lifespan(app: FastAPI):
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


def google_translate(text: str, src: str, tgt: str) -> str:
    return GoogleTranslator(source=src, target=tgt).translate(text)


def mymemory_translate(text: str, src: str, tgt: str) -> str:
    codes = {"uz": "uz-UZ", "zh-CN": "zh-CN"}
    return MyMemoryTranslator(source=codes[src], target=codes[tgt]).translate(text)


async def claude_translate(text: str, to_uz: bool) -> str:
    direction = (
        "Chinese to Uzbek (Latin script)" if to_uz else "Uzbek to Simplified Chinese"
    )
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 2000,
                "system": (
                    f"You are a professional translator. Translate the user's message "
                    f"from {direction}. Output only the translation, nothing else."
                ),
                "messages": [{"role": "user", "content": text}],
            },
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()


async def translate(text: str):
    """Xitoycha bo'lsa -> o'zbekcha, aks holda o'zbekcha -> xitoycha."""
    to_uz = bool(CJK.search(text))
    src, tgt = ("zh-CN", "uz") if to_uz else ("uz", "zh-CN")

    out = None
    if ANTHROPIC_KEY:
        try:
            out = await claude_translate(text, to_uz)
        except Exception:
            log.exception("Claude xatosi, zaxira tarjimonga o'tildi")
    if out is None:
        try:
            out = await asyncio.to_thread(google_translate, text, src, tgt)
        except Exception:
            log.exception("Google xatosi, MyMemory'ga o'tildi")
    if out is None:
        out = await asyncio.to_thread(mymemory_translate, text, src, tgt)

    py = None
    if not to_uz:
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
    if x_telegram_bot_api_secret_token != SECRET:
        raise HTTPException(status_code=403)

    chat_id = None
    try:
        update = await request.json()
        message = update.get("message")
        if not message:
            return {"ok": True}

        # Faqat egasiga javob beradi
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
            out, py = await translate(text)
            await send(chat_id, out if not py else f"{out}\n\n{py}")
    except Exception:
        log.exception("Xatolik")
        if chat_id:
            try:
                await send(chat_id, "Tarjima vaqtincha ishlamadi. Birozdan keyin qayta urinib ko'ring.")
            except Exception:
                pass
    return {"ok": True}
