# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from pyrogram import filters, types

from AnonX_3 import anon, app, db, lang
from AnonX_3.core.error_monitor import report_music_status
from AnonX_3.helpers import can_manage_vc, utils


@app.on_message(filters.command(["end", "stop"]) & filters.group & ~app.bl_users)
@lang.language()
@can_manage_vc
async def _stop(_, m: types.Message):
    if len(m.command) > 1:
        return

    call = await db.get_call(m.chat.id)
    await anon.stop(m.chat.id)
    if not call:
        return await m.reply_text(m.lang["not_playing"])

    await report_music_status(
        m.chat.id,
        "Admin က /stop သို့မဟုတ် /end နဲ့ music ကိုရပ်လိုက်ပါတယ်။",
        detail=f"Command by {m.from_user.id}",
        source="manual_stop",
    )

    lang_code = await db.get_lang(m.chat.id)
    tpl = await db.get_custom_text("play_stopped", m.lang["play_stopped"], lang_code)
    tpl = await utils.normalize_template_entities(
        "play_stopped", tpl, lang_code=lang_code
    )
    res = utils.format_template(tpl, m.from_user.mention)
    if isinstance(res, dict):
        await utils.reply_text(m, res["text"], entities=res["entities"])
    else:
        await utils.reply_text(m, res)


