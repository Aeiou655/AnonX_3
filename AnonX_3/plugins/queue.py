# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


from pyrogram import filters, types

from AnonX_3 import app, config, db, lang, queue, thumb
from AnonX_3.helpers import buttons, utils


def _as_formatted(result: str | dict) -> tuple[str, list[dict]]:
    if isinstance(result, dict):
        return result["text"], list(result.get("entities", []))
    return result, []


def _append_formatted(
    text: str,
    entities: list[dict],
    result: str | dict,
) -> tuple[str, list[dict]]:
    segment_text, segment_entities = _as_formatted(result)
    if not segment_text:
        return text, entities
    shift = utils.utf16_length(text)
    shifted_entities = []
    for entity in segment_entities:
        new_entity = dict(entity)
        new_entity["offset"] += shift
        shifted_entities.append(new_entity)
    return text + segment_text, entities + shifted_entities


@app.on_message(filters.command(["queue", "playing"]) & filters.group & ~app.bl_users)
@lang.language()
async def _queue_func(_, m: types.Message):
    if not await db.get_call(m.chat.id):
        return await m.reply_text(m.lang["not_playing"])

    lang_code = await db.get_lang(m.chat.id)
    _reply = await m.reply_text(m.lang["queue_fetching"])
    _queue = queue.get_queue(m.chat.id)
    _media = _queue[0]
    _thumb = await thumb.generate(_media) if config.THUMB_GEN else None
    queue_curr_tpl = await db.get_custom_text("queue_curr", m.lang["queue_curr"], lang_code)
    queue_curr_tpl = await utils.normalize_template_entities(
        "queue_curr", queue_curr_tpl, lang_code=lang_code
    )
    _formatted = utils.format_template(
        queue_curr_tpl,
        _media.url,
        _media.title[:50],
        _media.duration,
        _media.user,
    )
    _text, _entities = _as_formatted(_formatted)
    _queue.pop(0)

    if _queue:
        queue_item_tpl = await db.get_custom_text(
            "queue_item", m.lang["queue_item"], lang_code
        )
        queue_item_tpl = await utils.normalize_template_entities(
            "queue_item", queue_item_tpl, lang_code=lang_code
        )
        queue_text = ""
        queue_entities = []
        for i, media in enumerate(_queue, start=1):
            if i == 15:
                break
            queue_text, queue_entities = _append_formatted(
                queue_text,
                queue_entities,
                utils.format_template(
                queue_item_tpl,
                i + 1, media.title, media.duration
                ),
            )
        if queue_text:
            queue_entities.append({
                "type": "blockquote",
                "offset": 0,
                "length": utils.utf16_length(queue_text),
            })
            _text, _entities = _append_formatted(
                _text,
                _entities,
                {"text": queue_text, "entities": queue_entities},
            )

    _playing = await db.playing(m.chat.id)
    _buttons = buttons.queue_markup(
            m.chat.id,
            m.lang["playing"] if _playing else m.lang["paused"],
            _playing,
        )
    if _thumb:
        await utils.edit_media(
            _reply,
            media=types.InputMediaPhoto(
                media=_thumb,
                caption=_text,
                caption_entities=utils.pyrogram_entities(_entities) if _entities else None,
            ),
            reply_markup=_buttons,
        )
    else:
        await utils.edit_text(_reply, _text, entities=_entities or None, reply_markup=_buttons)




