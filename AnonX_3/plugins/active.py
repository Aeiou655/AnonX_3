# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import os
import asyncio

from pyrogram import enums, filters, types

from AnonX_3 import app, db, lang, logger, queue, userbot


def _peak_line(label: str, data: dict | None, fallback: str) -> str:
    if not data:
        return f"\n<b>{label}:</b> {fallback}"
    return (
        f"\n<b>{label}:</b> {data['date']} {int(data['hour']):02d}:00"
        f" - groups: {int(data['groups'])}"
    )


GROUP_CHAT_TYPES = {
    enums.ChatType.GROUP,
    enums.ChatType.SUPERGROUP,
}


async def _sync_live_group_count() -> tuple[int, int]:
    before = set(await db.get_chats())
    synced_ids = set(before)

    async def _save_group(chat_id: int, *, source: str, is_admin: bool | None = None) -> None:
        if chat_id in synced_ids:
            return
        synced_ids.add(chat_id)
        await db.add_chat(chat_id)
        await db.touch_audience(
            peer_id=chat_id,
            peer_type="group",
            source=source,
            is_admin=is_admin,
            is_active=True,
        )

    async def _process_bot_dialog(dialog) -> None:
        chat = getattr(dialog, "chat", None)
        chat_id = getattr(chat, "id", None)
        chat_type = getattr(chat, "type", None)
        if not isinstance(chat_id, int) or chat_type not in GROUP_CHAT_TYPES:
            return
        is_admin = None
        try:
            member = await app.get_chat_member(chat_id, app.id)
            status = str(getattr(member, "status", "")).split(".")[-1].lower()
            is_admin = status in {"administrator", "owner"}
        except Exception:
            pass
        await _save_group(chat_id, source="gp_command", is_admin=is_admin)

    try:
        async for dialog in app.get_dialogs():
            await _process_bot_dialog(dialog)
    except Exception as ex:
        if "BOT_METHOD_INVALID" not in str(ex):
            logger.warning("/gp bot dialog scan failed: %s", ex)

    for ub in userbot.clients:
        try:
            async for dialog in ub.get_dialogs():
                chat = getattr(dialog, "chat", None)
                chat_id = getattr(chat, "id", None)
                chat_type = getattr(chat, "type", None)
                if not isinstance(chat_id, int) or chat_type not in GROUP_CHAT_TYPES:
                    continue
                if chat_id in synced_ids:
                    continue
                try:
                    member = await app.get_chat_member(chat_id, app.id)
                    if member is None:
                        continue
                except Exception:
                    continue
                finally:
                    await asyncio.sleep(0.05)
                status = str(getattr(member, "status", "")).split(".")[-1].lower()
                await _save_group(
                    chat_id,
                    source="gp_userbot_dialog",
                    is_admin=status in {"administrator", "owner"},
                )
        except Exception as ex:
            logger.warning(
                "/gp userbot dialog scan failed for %s: %s",
                getattr(ub, "name", "unknown"),
                ex,
            )

    total_groups = len(await db.get_chats())
    return total_groups, max(0, total_groups - len(before))


@app.on_message(filters.command(["gp"]) & app.sudoers, group=-1)
@lang.language()
async def _group_count(_, m: types.Message):
    sent = await m.reply_text(m.lang.get("gp_fetching", "Detecting served groups..."))
    group_count, synced = await _sync_live_group_count()
    user_count = len(await db.get_users())
    if synced:
        text = m.lang.get(
            "gp_count_synced",
            "Groups: <b>{0}</b>\nUsers: <b>{1}</b>\nNewly detected groups: <b>{2}</b>",
        ).format(group_count, user_count, synced)
    else:
        text = m.lang.get(
            "gp_count",
            "Groups: <b>{0}</b>\nUsers: <b>{1}</b>",
        ).format(group_count, user_count)
    await sent.edit_text(text)


@app.on_message(filters.command(["ac", "activevc"]) & app.sudoers, group=-1)
@lang.language()
async def _activevc(_, m: types.Message):
    if not db.active_calls:
        return await m.reply_text(m.lang["vc_empty"])

    if m.command[0] == "ac":
        return await m.reply_text(m.lang["vc_count"].format(len(db.active_calls)))

    sent = await m.reply_text(m.lang["vc_fetching"])
    text = ""

    for i, chat in enumerate(db.active_calls):
        playing = queue.get_current(chat)
        text += f"\n{i+1}. <code>{chat}</code>\n    ➜ {playing.title[:25]}"

    today_peak = await db.get_today_peak_hour()
    last_day_peak = await db.get_last_day_peak_hour()
    text += _peak_line("Today Peak", today_peak, "collecting data...")
    text += _peak_line("Last Day Peak", last_day_peak, "no data yet")

    if len(text) < 4000:
        return await sent.edit_text(m.lang["vc_list"] + text)

    with open("activevc.txt", "w") as f:
        f.write(text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    f.close()
    await sent.edit_media(
        media=types.InputMediaDocument(
            media="activevc.txt",
            caption=m.lang["vc_list"],
        )
    )
    os.remove("activevc.txt")
