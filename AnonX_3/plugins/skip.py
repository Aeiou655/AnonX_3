# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from pyrogram import filters, types

from AnonX_3 import anon, app, db, lang
from AnonX_3.helpers import can_manage_vc, utils


@app.on_message(filters.command(["skip", "next"]) & filters.group & ~app.bl_users)
@lang.language()
@can_manage_vc
async def _skip(_, m: types.Message):
    if not await db.get_call(m.chat.id):
        return await m.reply_text(m.lang["not_playing"])

    await anon.play_next(m.chat.id)
    lang_code = await db.get_lang(m.chat.id)
    tpl = await db.get_custom_text("play_skipped", m.lang["play_skipped"], lang_code)
    tpl = await utils.normalize_template_entities(
        "play_skipped", tpl, lang_code=lang_code
    )
    try:
        res = utils.format_template(tpl, m.from_user.mention)
    except Exception:
        tpl = m.lang["play_skipped"]
        res = utils.format_template(tpl, m.from_user.mention)
    if isinstance(res, dict):
        await utils.reply_text(m, res["text"], entities=res["entities"])
    else:
        await utils.reply_text(m, res)


