# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from pyrogram import filters, types

from AnonX_3 import anon, app, bot_api, db, lang, queue
from AnonX_3.helpers import buttons, can_manage_vc, utils


async def _rebuild_play_media_caption(chat_id: int, media):
    _lang = await lang.get_lang(chat_id)
    lang_code = await db.get_lang(chat_id)
    play_media_tpl = await db.get_custom_text("play_media", _lang["play_media"], lang_code)
    play_media_tpl = await utils.normalize_template_entities(
        "play_media", play_media_tpl, lang_code=lang_code
    )
    res = utils.format_template(play_media_tpl, media.url, media.title, media.duration, media.user)
    if isinstance(res, dict):
        text = res["text"]
        _, _, entities = utils.deserialize_entities(res["entities"])
        text, entities = utils.auto_link_title(text, entities, media.title, media.url)
        return text, entities
    return res, []


@app.on_message(filters.command(["resume"]) & filters.group & ~app.bl_users)
@lang.language()
@can_manage_vc
async def _resume(_, m: types.Message):
    if not await db.get_call(m.chat.id):
        return await m.reply_text(m.lang["not_playing"])

    if await db.playing(m.chat.id):
        return await m.reply_text(m.lang["play_not_paused"])

    await anon.resume(m.chat.id)
    lang_code = await db.get_lang(m.chat.id)
    tpl = await db.get_custom_text("play_resumed", m.lang["play_resumed"], lang_code)
    tpl = await utils.normalize_template_entities(
        "play_resumed", tpl, lang_code=lang_code
    )
    reply_res = utils.format_template(tpl, m.from_user.mention)

    media = queue.get_current(m.chat.id)
    if media and media.message_id:
        original_text, original_entities = await _rebuild_play_media_caption(m.chat.id, media)
        if isinstance(reply_res, dict):
            reply_text = reply_res["text"]
            reply_entities = list(reply_res["entities"])
        else:
            reply_text = reply_res
            reply_entities = []

        prefix = "\n\n"
        shift = utils.utf16_length(original_text) + utils.utf16_length(prefix)
        for ent in reply_entities:
            ent["offset"] += shift

        full_text = f"{original_text}{prefix}{reply_text}"
        full_entities = (original_entities or []) + reply_entities
        keyboard = buttons.controls(m.chat.id, status=m.lang["playing"])
        try:
            await bot_api.edit_message_caption(
                chat_id=m.chat.id,
                message_id=media.message_id,
                caption=full_text,
                caption_entities=full_entities or None,
                reply_markup=keyboard,
            )
        except Exception:
            pass
    else:
        text = reply_res["text"] if isinstance(reply_res, dict) else reply_res
        entities = reply_res.get("entities") if isinstance(reply_res, dict) else None
        await utils.reply_text(
            m,
            text=text,
            entities=entities,
            reply_markup=buttons.controls(m.chat.id, status=m.lang["playing"]),
        )


