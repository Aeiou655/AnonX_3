# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import time
import psutil
import statistics
from collections import deque

from urllib.parse import urlparse

from pyrogram import filters, types
from AnonX_3 import app, anon, boot, bot_api, config, db, lang, logger
from AnonX_3.helpers import buttons


_CPU_WARMED = False
_METRICS_CACHE = {"at": 0.0, "cpu": 0.0, "ram": 0.0, "disk": 0.0}
_LATENCY_WINDOW = deque(maxlen=12)


def _format_uptime(seconds: int) -> str:
    parts = [
        f"{seconds % 60}s",
        f"{(seconds // 60) % 60}m",
        f"{(seconds // 3600) % 24}h",
        f"{seconds // 86400}days",
    ]
    return (f"{parts[-1]}, " if parts[-1][:-4] != "0" else "") + ":".join(reversed(parts[:-1]))


def _sample_metrics() -> tuple[float, float, float]:
    global _CPU_WARMED
    now = time.monotonic()
    if now - _METRICS_CACHE["at"] < 1.0:
        return _METRICS_CACHE["cpu"], _METRICS_CACHE["ram"], _METRICS_CACHE["disk"]

    if not _CPU_WARMED:
        psutil.cpu_percent(interval=None)
        _CPU_WARMED = True
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    _METRICS_CACHE.update({"at": now, "cpu": cpu, "ram": ram, "disk": disk})
    return cpu, ram, disk


def _format_pytgcalls_ping(ping: float | None, missing_text: str) -> str:
    if ping is None:
        return missing_text
    return f"{ping:.3f}ms"


def _looks_like_bad_http_photo_source(photo: str) -> bool:
    parsed = urlparse(photo)
    return parsed.scheme in {"http", "https"} and not parsed.netloc


def _is_invalid_photo_error(ex: Exception) -> bool:
    text = str(ex).lower()
    return (
        "wrong file identifier/http url specified" in text
        or "wrong file identifier" in text
        or "wrong http url specified" in text
        or "media_empty" in text
        or "photo_invalid" in text
    )


@app.on_message(filters.command(["alive", "ping"]) & ~app.bl_users)
@lang.language()
async def _ping(_, m: types.Message):
    start = time.perf_counter()
    sent = await bot_api.send_message(
        chat_id=m.chat.id,
        text=m.lang["pinging"],
        reply_to_message_id=m.id,
        fetch=False,
    )
    uptime = _format_uptime(int(time.time() - boot))
    raw_latency = round((time.perf_counter() - start) * 1000, 2)
    _LATENCY_WINDOW.append(raw_latency)
    latency = round(statistics.median(_LATENCY_WINDOW), 2)
    message_id = sent["message_id"]
    cpu, ram, disk = _sample_metrics()
    tg_ping = await anon.ping()
    caption = m.lang["ping_pong"].format(
        latency,
        uptime,
        cpu,
        ram,
        disk,
        _format_pytgcalls_ping(
            tg_ping,
            m.lang.get("ping_pytgcalls_measuring", "measuring"),
        ),
    )
    reply_markup = buttons.ping_markup(m.lang["support"])

    # Build image candidates: DB custom source first, then config default.
    if not db.bot_images:
        await db.get_bot_image("ping_img")
    candidates: list[tuple[str, str]] = []
    db_ping_img = db.bot_images.get("ping_img")
    if isinstance(db_ping_img, str) and db_ping_img.strip():
        candidates.append(("db", db_ping_img.strip()))
    default_ping_img = config.PING_IMG
    if default_ping_img:
        candidates.append(("default", default_ping_img))

    for source, photo in candidates:
        if _looks_like_bad_http_photo_source(photo):
            logger.warning("Skipping malformed /ping image URL from %s: %s", source, photo)
            if source == "db":
                try:
                    await db.set_bot_image("ping_img", "")
                except Exception as clear_ex:
                    logger.warning("Failed to clear malformed DB ping_img: %s", clear_ex)
            continue

        try:
            await bot_api.edit_message_media(
                chat_id=m.chat.id,
                message_id=message_id,
                media=types.InputMediaPhoto(
                    media=photo,
                    caption=caption,
                ),
                reply_markup=reply_markup,
                fetch=False,
            )
            return
        except RuntimeError as ex:
            if _is_invalid_photo_error(ex):
                logger.warning("Invalid /ping image source detected from %s: %s", source, ex)
                if source == "db":
                    try:
                        await db.set_bot_image("ping_img", "")
                        logger.warning("Cleared invalid DB ping_img source.")
                    except Exception as clear_ex:
                        logger.warning("Failed to clear invalid DB ping_img: %s", clear_ex)
                continue
            raise

    await bot_api.edit_message_text(
        chat_id=m.chat.id,
        message_id=message_id,
        text=caption,
        reply_markup=reply_markup,
    )


