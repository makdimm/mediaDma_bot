#!/usr/bin/env python3
"""Kie Image Bot — Telegram → Nano Banana 2 (Kie.ai) → генерация изображений"""

import asyncio
import base64
import io
import json
import logging
import os
import sys
import time

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
KIE_API_KEY = os.environ["KIE_API_KEY"]
ALLOWED_IDS = [int(x.strip()) for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()]

KIE_BASE = "https://api.kie.ai/api/v1"
KIE_UPLOAD_BASE = "https://kieai.redpandaai.co"

HEADERS = {
    "Authorization": f"Bearer {KIE_API_KEY}",
    "Content-Type": "application/json",
}

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Сборщик альбомов (медиа-групп)
_pending_albums: dict[str, dict] = {}
_ALBUM_TIMEOUT = 0.8  # секунд ждать остальные фото группы

# Подтверждения перед генерацией {user_id: {"prompt": ..., "photos": [...], "reply_to": msg}}
_pending_confirmations: dict[int, dict] = {}


async def kie_request(method: str, path: str, body: dict = None) -> dict:
    url = f"{KIE_BASE}{path}"
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.request(method, url, json=body) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise Exception(f"Kie.ai HTTP {resp.status}: {data.get('msg', 'Unknown error')}")
            if data.get("code") != 200:
                raise Exception(f"Kie.ai error {data['code']}: {data.get('msg', 'Unknown error')}")
            return data


async def upload_image(file_bytes: bytes, filename: str) -> str:
    b64 = base64.b64encode(file_bytes).decode()
    body = {
        "base64Data": b64,
        "uploadPath": "images/telegram",
        "fileName": filename,
    }
    url = f"{KIE_UPLOAD_BASE}/api/file-base64-upload"
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        async with session.post(url, json=body) as resp:
            data = await resp.json()
            if not data.get("success"):
                raise Exception(f"Upload failed: {data.get('msg', 'Unknown error')}")
            return data["data"]["downloadUrl"]


async def create_generation_task(prompt: str, image_urls: list[str] = None) -> str:
    payload = {
        "model": "nano-banana-2",
        "input": {
            "prompt": prompt,
            "image_input": image_urls or [],
            "aspect_ratio": "auto",
            "resolution": "1K",
            "output_format": "png",
        },
    }
    data = await kie_request("POST", "/jobs/createTask", payload)
    return data["data"]["taskId"]


async def wait_for_result(task_id: str, poll_interval: int = 5, timeout: int = 180) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = await kie_request("GET", f"/jobs/recordInfo?taskId={task_id}")
        record = data.get("data", {})
        state = record.get("state", "unknown")
        logger.info("Task %s state: %s", task_id, state)

        if state == "success":
            result_json = record.get("resultJson", "")
            if result_json:
                parsed = json.loads(result_json)
                urls = parsed.get("resultUrls", [])
                if urls:
                    return urls[0]
            raise Exception("Task completed but result URL is empty")
        elif state in ("failed", "fail"):
            err = record.get("failMsg") or record.get("failCode") or "Unknown error"
            raise Exception(f"Generation failed: {err}")
        elif state in ("waiting", "processing"):
            await asyncio.sleep(poll_interval)
        else:
            raise Exception(f"Unknown task status: {state}")

    raise Exception("Generation timed out")


async def download_and_send(url: str, msg: types.Message):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"Failed to download result: HTTP {resp.status}")
            img_bytes = await resp.read()
    await msg.reply_photo(BufferedInputFile(img_bytes, filename="result.png"))


async def process_generation(prompt: str, photos: list, reply_to: types.Message):
    """Загрузить фото, создать задачу, дождаться результата, отправить"""
    await bot.send_chat_action(reply_to.chat.id, "typing")
    status_msg = await reply_to.reply("⏳ Генерирую...")

    try:
        image_urls = []
        for idx, photo in enumerate(photos):
            file = await bot.get_file(photo.file_id)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, buf)
            buf.seek(0)
            dl_url = await upload_image(buf.read(), f"ref_{idx}.png")
            image_urls.append(dl_url)
            logger.info("Uploaded ref %d: %s", idx, dl_url)

        task_id = await create_generation_task(prompt, image_urls)
        logger.info("Task created: %s (images: %d)", task_id, len(image_urls))

        await status_msg.edit_text("🎨 Генерирую изображение... (ждём результат)")
        result_url = await wait_for_result(task_id)
        await download_and_send(result_url, reply_to)
        await status_msg.delete()
    except Exception as e:
        logger.exception("Error")
        await status_msg.edit_text(f"❌ Ошибка: {e}")


def make_confirm_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, генерировать",
                    callback_data=f"gen_yes:{user_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"gen_no:{user_id}",
                ),
            ]
        ]
    )


async def ask_confirmation(prompt: str, photos: list, reply_to: types.Message):
    """Спросить подтверждение перед генерацией"""
    user_id = reply_to.from_user.id
    _pending_confirmations[user_id] = {
        "prompt": prompt,
        "photos": photos,
        "reply_to": reply_to,
    }

    text = f"⚠️ Начинаем генерацию?\n\n📝 Промпт: {prompt[:100]}{'...' if len(prompt) > 100 else ''}"
    if photos:
        text += f"\n🖼️ Фото: {len(photos)} шт."

    await reply_to.reply(text, reply_markup=make_confirm_kb(user_id))


async def process_album(media_group_id: str):
    """Обработать собранный альбом после таймаута"""
    await asyncio.sleep(_ALBUM_TIMEOUT)

    album = _pending_albums.pop(media_group_id, None)
    if not album:
        return

    photos = album["photos"]
    prompt = album["prompt"]
    reply_to = album["msg"]

    if not prompt:
        await reply_to.reply("Добавь текстовый промпт к фото (в подпись первого фото)")
        return

    if not photos:
        return

    await ask_confirmation(prompt, photos, reply_to)


@dp.callback_query(lambda c: c.data and c.data.startswith("gen_"))
async def handle_confirmation(call: CallbackQuery):
    action, user_id_str = call.data.split(":")
    user_id = int(user_id_str)

    if call.from_user.id != user_id:
        await call.answer("Это не твоя кнопка")
        return

    pending = _pending_confirmations.pop(user_id, None)
    if not pending:
        await call.answer("Запрос устарел, отправь промпт заново")
        return

    await call.answer()
    await call.message.delete()

    if action == "gen_no":
        await pending["reply_to"].reply("❌ Отменено")
        return

    # gen_yes — запускаем генерацию
    await process_generation(
        pending["prompt"],
        pending["photos"],
        pending["reply_to"],
    )


@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id not in ALLOWED_IDS:
        logger.info("Unauthorized access attempt from user_id=%d username=%s", msg.from_user.id, msg.from_user.username or "-")
        await msg.reply("⛔ Нет доступа")
        return
    await msg.reply(
        "🎨 Привет! Я генератор изображений на Nano Banana 2.\n\n"
        "📝 Просто отправь промпт текстом — сгенерирую картинку.\n"
        "🖼️ С картинкой — отправь промпт + фото, сгенерирую на основе референса.\n"
        "📸 Несколько фото — отправь альбом с промптом, учту все."
    )


@dp.message()
async def handle_message(msg: types.Message):
    if msg.from_user.id not in ALLOWED_IDS:
        logger.info("Unauthorized access attempt from user_id=%d username=%s", msg.from_user.id, msg.from_user.username or "-")
        await msg.reply("⛔ Нет доступа")
        return

    # Промпт из текста или caption
    prompt = None
    if msg.text and not msg.text.startswith("/"):
        prompt = msg.text.strip()
    elif msg.caption and not msg.caption.startswith("/"):
        prompt = msg.caption.strip()

    # Если это медиа-группа — собираем все фото
    if msg.media_group_id:
        album = _pending_albums.get(msg.media_group_id)

        if album is None:
            album = {
                "photos": [],
                "prompt": prompt or "",
                "msg": msg,
            }
            _pending_albums[msg.media_group_id] = album
            asyncio.create_task(process_album(msg.media_group_id))

        if msg.photo:
            album["photos"].append(msg.photo[-1])
        elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
            album["photos"].append(msg.document)

        if prompt and not album["prompt"]:
            album["prompt"] = prompt

        return

    # Одиночное сообщение (не группа)
    if not prompt:
        await msg.reply("Отправь текстовый промпт (можно с картинкой)")
        return

    photos = []
    if msg.photo:
        photos.append(msg.photo[-1])
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        photos.append(msg.document)

    # Запрашиваем подтверждение
    await ask_confirmation(prompt, photos, msg)


async def main():
    logger.info("Бот запущен, ALLOWED_IDS=%s", ALLOWED_IDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлен")
        sys.exit(0)
