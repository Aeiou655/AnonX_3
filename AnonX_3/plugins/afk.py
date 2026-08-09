# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import time

from pyrogram import enums, filters, types

from AnonX_3 import app, db, lang


AFK_NOTICE_COOLDOWN = 15
notice_cache = {}


def _format_afk_time(started_at: int) -> str:
    elapsed = max(int(time.time()) - int(started_at), 0)
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


async def _reply_afk_notice(message: types.Message, user: types.User) -> None:
    afk = await db.get_afk(user.id)
    if not afk:
        return

    key = (message.chat.id, message.from_user.id, user.id)
    now = time.time()
    if notice_cache.get(key, 0) > now:
        return

    notice_cache[key] = now + AFK_NOTICE_COOLDOWN
    duration = _format_afk_time(afk["since"])
    reason = afk.get("reason") or message.lang["afk_reason_default"]
    await message.reply_text(
        message.lang["afk_notice"].format(user.mention, duration, reason),
        quote=True,
    )


@app.on_message(filters.command(["afk"]) & ~app.bl_users)
@lang.language()
async def set_afk(_, message: types.Message):
    if not message.from_user or message.from_user.is_bot:
        return

    reason = " ".join(message.command[1:]).strip() if len(message.command) > 1 else ""
    await db.set_afk(message.from_user.id, reason)
    text = (
        message.lang["afk_set_reason"].format(reason)
        if reason
        else message.lang["afk_set"]
    )
    await message.reply_text(text, quote=True)


@app.on_message(~filters.service & ~app.bl_users, group=30)
@lang.language()
async def unset_afk(_, message: types.Message):
    if not message.from_user or message.from_user.is_bot:
        return

    if message.text and message.text.startswith("/"):
        command = message.text.split(maxsplit=1)[0][1:]
        if command.split("@", 1)[0].lower() == "afk":
            return

    afk = await db.get_afk(message.from_user.id)
    if not afk:
        return

    await db.clear_afk(message.from_user.id)
    await message.reply_text(
        message.lang["afk_back"].format(_format_afk_time(afk["since"])),
        quote=True,
    )


@app.on_message(filters.group & ~filters.service & ~app.bl_users, group=31)
@lang.language()
async def afk_watcher(_, message: types.Message):
    if not message.from_user or message.from_user.is_bot:
        return

    targets = {}

    if message.reply_to_message and message.reply_to_message.from_user:
        replied = message.reply_to_message.from_user
        if not replied.is_bot:
            targets[replied.id] = replied

    source_text = message.text or message.caption or ""
    entities = list(message.entities or [])
    entities.extend(message.caption_entities or [])
    for entity in entities:
        if entity.type == enums.MessageEntityType.TEXT_MENTION and entity.user:
            if not entity.user.is_bot:
                targets[entity.user.id] = entity.user
        elif entity.type == enums.MessageEntityType.MENTION and source_text:
            username = source_text[entity.offset + 1 : entity.offset + entity.length]
            try:
                user = await app.get_users(username)
            except Exception:
                continue
            if not user.is_bot:
                targets[user.id] = user

    for user in targets.values():
        if user.id == message.from_user.id:
            continue
        await _reply_afk_notice(message, user)


