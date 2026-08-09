# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import re

from pyrogram import filters, types

from AnonX_3 import app, db, lang
from AnonX_3.helpers import admin_check, utils


AI_DJ_MODES = {
    "chill",
    "party",
    "study",
    "workout",
    "myanmar",
    "romantic",
}


@app.on_message(filters.command(["autoplay", "aidj"]) & filters.group & ~app.bl_users)
@lang.language()
@admin_check
async def autoplay_hndlr(_, m: types.Message):
    is_aidj = m.command[0].lower() == "aidj"
    enabled = await db.get_autoplay(m.chat.id)
    current_mode = await db.get_aidj_mode(m.chat.id)
    if is_aidj:
        usage = (
            "<b>AI DJ:</b> "
            f"<code>{'ON' if enabled else 'OFF'}</code>\n"
            f"<b>Mode:</b> <code>{current_mode}</code>\n\n"
            "<code>/aidj on</code> · <code>/aidj off</code>\n"
            "<code>/aidj chill|party|study|workout|myanmar|romantic</code>"
        )
    else:
        usage = m.lang.get(
            "autoplay_usage",
            "<b>Usage:</b>\n\n<code>/{0}</code> or <code>/{0} [on|off]</code>",
        ).format(m.command[0])

    raw_arg = " ".join(m.command[1:]).strip().lower()
    arg = re.split(r"\s+", raw_arg, maxsplit=1)[0] if raw_arg else ""
    state_map = {
        "on": True,
        "off": False,
        "enable": True,
        "disable": False,
        "true": True,
        "false": False,
        "yes": True,
        "no": False,
        "1": True,
        "0": False,
    }
    if is_aidj and len(m.command) == 1:
        return await m.reply_text(usage)
    if len(m.command) == 1:
        new_state = not enabled
    elif arg in state_map:
        new_state = state_map[arg]
    elif is_aidj and arg in AI_DJ_MODES:
        new_state = True
        current_mode = arg
        await db.set_aidj_mode(m.chat.id, current_mode)
    else:
        return await m.reply_text(usage)

    await db.set_autoplay(m.chat.id, new_state)
    if new_state:
        if is_aidj:
            return await m.reply_text(
                "<b>AI DJ is ON.</b>\n"
                f"Mode: <code>{current_mode}</code>\n"
                "A matching track will continue automatically when the queue ends."
            )
        text = await db.get_custom_text_for_chat(
            m.chat.id,
            "autoplay_on",
            m.lang.get(
                "autoplay_on",
                "Autoplay is now ON. Similar songs will continue automatically when the queue ends.",
            ),
        )
        return await utils.reply_formatted(m, text, template_key="autoplay_on")
    if is_aidj:
        return await m.reply_text("<b>AI DJ is OFF.</b>")
    text = await db.get_custom_text_for_chat(
        m.chat.id,
        "autoplay_off",
        m.lang.get(
            "autoplay_off",
            "Autoplay is now OFF.",
        ),
    )
    return await utils.reply_formatted(m, text, template_key="autoplay_off")


