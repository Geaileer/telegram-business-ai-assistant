"""
Telegram Business AI-автоответчик (Google Gemini)
Style: Respectful Son to Father (Уважительный сын)
Debug Mode: Логирование всех входящих бизнес-сообщений
"""

import asyncio
import logging
import os
import re
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatAction
from aiogram.types import Message
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PRIMARY_MODEL = "gemini-3.6-flash"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в .env")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("business_bot")

SYSTEM_PROMPT = (
    "Ты — сдержанный, вежливый и внимательный сын. "
    "Отвечаешь в Telegram своему отцу от собственного имени.\n\n"
    "ТВОЙ СТИЛЬ ОБЩЕНИЯ И ХАРАКТЕР:\n"
    "- Обращайся к собеседнику с полным уважением и теплотой («Папа», «Да, папа»).\n"
    "- Общайся спокойно, грамотно, собранно и по делу. Без дерзости, сленга и фамильярности.\n"
    "- Всегда проявляй готовность помочь или выполнить поручение.\n"
    "- Пиши естественным языком, как взрослеющий ответственный сын.\n\n"
    "ПРАВИЛО ФОРМАТА:\n"
    "Отвечай кратко и емко (1-3 предложения), чтобы сообщение звучало естественно и не обрывалось. "
    "Никаких разметок (*, _), спецсимволов, смайликов или служебного текста!"
)

router = Router(name="business_router")
gemini_client = genai.Client(api_key=GEMINI_API_KEY.split(",")[0].strip())
_host_id_cache: Dict[str, int] = {}

REQUEST_INTERVAL = 4.0
_last_request_time = 0.0


async def wait_for_rate_limit():
    global _last_request_time
    now = asyncio.get_event_loop().time()
    elapsed = now - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        wait_time = REQUEST_INTERVAL - elapsed
        await asyncio.sleep(wait_time)
    _last_request_time = asyncio.get_event_loop().time()


async def get_host_id(bot: Bot, business_connection_id: str) -> Optional[int]:
    if business_connection_id in _host_id_cache:
        return _host_id_cache[business_connection_id]

    try:
        connection = await bot.get_business_connection(business_connection_id)
        host_id = connection.user.id
        _host_id_cache[business_connection_id] = host_id
        logger.info("Успешно получен Host ID аккаунта: %d", host_id)
        return host_id
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось получить business connection: %s", exc)
        return None


def clean_gemini_response(text: str) -> str:
    if not text:
        return ""

    lines = text.splitlines()
    clean_lines = []
    for line in lines:
        if re.search(r"^\s*(\*|\-)?\s*(Draft|Option|Version|Alternative|\d+\.)", line, re.IGNORECASE):
            continue
        clean_lines.append(line)

    result = "\n".join(clean_lines).strip()
    result = re.sub(r"[\*\_~`#]", "", result)
    return result


def _generate_sync(prompt: str) -> str:
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.6,
        max_output_tokens=300,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    response = gemini_client.models.generate_content(
        model=PRIMARY_MODEL,
        contents=prompt,
        config=config,
    )
    return response.text or ""


async def ask_gemini(user_text: str, sender_info: str) -> str:
    prompt = f"Отец ({sender_info}) пишет: {user_text}"

    await wait_for_rate_limit()

    try:
        raw_text = await asyncio.to_thread(_generate_sync, prompt)
        cleaned = clean_gemini_response(raw_text)
        if cleaned:
            return cleaned
    except APIError as exc:
        if exc.code == 429:
            logger.warning("Рейт-лимит 429. Ждем 10 секунд...")
            await asyncio.sleep(10.0)
            try:
                raw_text = await asyncio.to_thread(_generate_sync, prompt)
                return clean_gemini_response(raw_text) or "Да, папа, я на связи."
            except Exception:  # noqa: BLE001
                pass
        logger.error("Gemini APIError [%s]: %s", exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при вызове Gemini API: %s", exc)

    return "Да, папа, я на связи. Чуть позже напишу подробнее."


@router.business_message()
async def handle_business_message(message: Message, bot: Bot) -> None:
    # Логируем абсолютно ВСЕ входящие события Telegram Business
    logger.info(
        "ПОЛУЧЕНО БИЗНЕС-СООБЩЕНИЕ | ConnID: %s | FromID: %s | Text: %s",
        message.business_connection_id,
        message.from_user.id if message.from_user else "None",
        message.text,
    )

    if not message.business_connection_id or not message.from_user or not message.text:
        logger.warning("Сообщение пропущено: отсутствует connection_id, sender или text")
        return

    host_id = await get_host_id(bot, message.business_connection_id)
    if host_id is not None and message.from_user.id == host_id:
        logger.info("Сообщение пропущено: отправитель является владельцем аккаунта (Host ID: %d)", host_id)
        return

    user = message.from_user
    sender_info = f"Имя: {user.first_name or ''} {user.last_name or ''}, username: @{user.username or 'нет'}"

    try:
        await bot.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.TYPING,
            business_connection_id=message.business_connection_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось отправить статус печати: %s", exc)

    reply_text = await ask_gemini(message.text, sender_info)

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            text=reply_text,
            business_connection_id=message.business_connection_id,
        )
        logger.info("Ответ успешно отправлен пользователю @%s", user.username)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при отправке ответа в Telegram: %s", exc)


async def main() -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен. Ожидание бизнес-сообщений...")

    while True:
        try:
            await dp.start_polling(bot, allowed_updates=["business_message", "business_connection", "message"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Сетевой сбой: %s. Переподключение через 5 секунд...", exc)
            await asyncio.sleep(5.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")