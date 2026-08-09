# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import os
import platform
import sys

import psutil
from pyrogram import __version__, filters, types
from pytgcalls import __version__ as pytgver

from AnonX_3 import app, config, db, lang, logger, userbot
from AnonX_3.helpers import utils
from AnonX_3.plugins import all_modules


_PROCESS_CPU_WARMUP_DONE = False


@app.on_message(filters.command(["stats"]) & filters.group & ~app.bl_users)
@lang.language()
async def _stats(_, m: types.Message):
    photo = await db.get_bot_image("ping_img")
    try:
        sent = await m.reply_photo(
            photo=photo,
            caption=m.lang["stats_fetching"],
        )
    except Exception as ex:
        logger.warning("Failed to send stats photo, falling back to text: %s", ex)
        sent = await m.reply_text(m.lang["stats_fetching"])

    pid = os.getpid()
    _utext = m.lang["stats_user"].format(
        app.name,
        len(userbot.clients),
        config.AUTO_LEAVE,
        len(db.blacklisted),
        len(app.bl_users),
        len(app._sudo_ids),
        len(await db.get_chats()),
        len(await db.get_users()),
    )
    if m.from_user.id in app._sudo_ids:
        global _PROCESS_CPU_WARMUP_DONE
        process = psutil.Process(pid)
        storage = psutil.disk_usage("/")
        if not _PROCESS_CPU_WARMUP_DONE:
            process.cpu_percent(interval=None)
            _PROCESS_CPU_WARMUP_DONE = True
        with process.oneshot():
            rss_mb = process.memory_info().rss / 1024**2
            process_cpu = process.cpu_percent(interval=None)
        _utext += m.lang["stats_sudo"].format(
            len(all_modules),
            platform.system(),
            f"{rss_mb:.2f}",
            round(psutil.virtual_memory().total / (1024.0**3)),
            process_cpu,
            psutil.cpu_count(),
            f"{storage.used / (1024.0**3):.2f}",
            f"{storage.total / (1024.0**3):.2f}",
            sys.version.split()[0],
            __version__,
            pytgver,
        )
    await utils.edit_caption(sent, caption=_utext)


