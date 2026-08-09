# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from pyrogram import filters, types

from AnonX_3 import app, db, lang
from AnonX_3.helpers import admin_check, buttons, utils


@app.on_message(filters.command(["lang", "language"]) & ~app.bl_users)
@lang.language()
async def _lang(_, m: types.Message):
    current = await db.get_lang(m.chat.id)
    keyboard = buttons.lang_markup(current)
    await utils.reply_text(m, m.lang["lang_choose"], reply_markup=keyboard)


@app.on_callback_query(filters.regex(r"^lang(?:_change|uage)") & ~app.bl_users)
@lang.language()
@admin_check
async def _lang_cb(_, query: types.CallbackQuery):
    data = query.data.split()
    if data[0] == "language":
        current = await db.get_lang(query.message.chat.id)
        keyboard = buttons.lang_markup(current)
        return await utils.edit_callback_text(
            query.message,
            query.lang["lang_choose"],
            reply_markup=keyboard,
            ignore_stale=True,
        )

    _lang = data[1]
    current = await db.get_lang(query.message.chat.id)
    if current == _lang:
        return await query.answer(
            query.lang["lang_same"].format(current), show_alert=False
        )

    await query.answer(query.lang["lang_change"].format(_lang), show_alert=False)
    await db.set_lang(query.message.chat.id, _lang)
    await db.ensure_custom_text_language(_lang)
    await utils.edit_callback_text(
        query.message,
        query.lang["lang_changed"].format(_lang),
        ignore_stale=True,
    )


