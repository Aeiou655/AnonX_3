# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import re
from contextlib import suppress
from html import escape

from pyrogram import errors, filters, types

from AnonX_3 import (
    anon,
    app,
    bot_api,
    db,
    facebook,
    lang,
    logger,
    queue,
    tg,
    tiktok,
    yt,
)
from AnonX_3.helpers import admin_check, buttons, can_manage_vc, utils
from AnonX_3.helpers._play import cancel_play_request


def _clear_active_message_id(chat_id: int, message_id: int) -> None:
    media = queue.get_current(chat_id)
    if media and media.message_id == message_id:
        media.message_id = 0


async def get_status_text(query: types.CallbackQuery, key: str, fallback: str) -> str:
    lang_code = await db.get_lang(query.message.chat.id)
    template = await db.get_custom_text(key, fallback, lang_code)
    template = await utils.normalize_template_entities(
        key, template, lang_code=lang_code
    )
    return utils.format_template_text(template)


async def _rebuild_play_media_caption(chat_id: int, media):
    _lang = await lang.get_lang(chat_id)
    lang_code = await db.get_lang(chat_id)
    play_media_tpl = await db.get_custom_text(
        "play_media", _lang["play_media"], lang_code
    )
    play_media_tpl = await utils.normalize_template_entities(
        "play_media", play_media_tpl, lang_code=lang_code
    )
    try:
        from AnonX_3.helpers._play import format_play_media_template

        res = format_play_media_template(play_media_tpl, media)
    except Exception:
        res = utils.format_template(
            play_media_tpl,
            media.url,
            media.title,
            media.duration,
            media.user,
            url=getattr(media, "url", "") or "",
            title=getattr(media, "title", "") or "",
            duration=getattr(media, "duration", "") or "",
            user=getattr(media, "user", "") or "",
        )
    if isinstance(res, dict):
        text = res["text"]
        _, _, entities = utils.deserialize_entities(res["entities"])
        text, entities = utils.auto_link_title(text, entities, media.title, media.url)
        return text, entities
    return res, []


def sanitize_help_text(text: str) -> str:
    return text.replace("<b><u>", "<u><b>")


@app.on_callback_query(filters.regex("cancel_dl") & ~app.bl_users)
@lang.language()
async def cancel_dl(_, query: types.CallbackQuery):
    """Cancel every request branch and remove its transient status card."""
    msg_id = query.message.id if query.message else 0
    with suppress(errors.QueryIdInvalid):
        await query.answer()
    if not msg_id:
        return

    # Close the progress ownership gate before deletion/detach.  The local
    # cache worker may remain alive for a concurrent request, but it must not
    # revive this cancelled status card.
    with suppress(Exception):
        await utils.close_download_progress(query.message)

    # Delete first so repeated taps cannot generate stale "already cancelled"
    # banners while the request branches are shutting down.
    with suppress(Exception):
        await query.message.delete()

    results = await asyncio.gather(
        tg.cancel(query),
        yt.cancel(msg_id),
        tiktok.cancel(msg_id),
        facebook.cancel(msg_id),
        cancel_play_request(msg_id),
        return_exceptions=True,
    )
    cancelled = any(result is True for result in results)
    logger.info(
        "Play request cancel handled chat_id=%s message_id=%s cancelled=%s",
        getattr(getattr(query.message, "chat", None), "id", None),
        msg_id,
        int(cancelled),
    )


@app.on_callback_query(filters.regex("controls") & ~app.bl_users)
@lang.language()
@can_manage_vc
async def _controls(_, query: types.CallbackQuery):
    args = query.data.split()
    action, chat_id = args[1], int(args[2])
    qaction = len(args) == 4
    user = query.from_user.mention

    if action == "close":
        await query.answer()
        _clear_active_message_id(chat_id, query.message.id)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if not await db.get_call(chat_id):
        try:
            return await query.answer(query.lang["not_playing"], show_alert=False)
        except errors.QueryIdInvalid:
            try:
                await query.message.delete()
            except Exception:
                pass
            return

    if action == "status":
        return await query.answer()

    if action == "pause":
        if not await db.playing(chat_id):
            return await query.answer(
                query.lang["play_already_paused"], show_alert=False
            )
        await anon.pause(chat_id)
        await query.answer(query.lang["paused"], show_alert=False)
        status = await get_status_text(query, "paused_status", query.lang["paused"])
        if qaction:
            updated = await utils.edit_reply_markup(
                query.message,
                reply_markup=buttons.queue_markup(chat_id, status, False),
                ignore_stale=True,
            )
            if updated is None:
                _clear_active_message_id(chat_id, query.message.id)
                return
            return updated
        tpl = await db.get_custom_text_for_chat(
            chat_id, "play_paused", query.lang["play_paused"]
        )
        tpl = await utils.normalize_template_entities(
            "play_paused", tpl, lang_code=await db.get_lang(chat_id)
        )
        reply_res = utils.format_template(tpl, user)

    elif action == "resume":
        if await db.playing(chat_id):
            return await query.answer(query.lang["play_not_paused"], show_alert=False)
        await anon.resume(chat_id)
        await query.answer(query.lang["playing"], show_alert=False)
        status = await get_status_text(query, "playing_status", query.lang["playing"])
        if qaction:
            updated = await utils.edit_reply_markup(
                query.message,
                reply_markup=buttons.queue_markup(chat_id, status, True),
                ignore_stale=True,
            )
            if updated is None:
                _clear_active_message_id(chat_id, query.message.id)
                return
            return updated
        tpl = await db.get_custom_text_for_chat(
            chat_id, "play_resumed", query.lang["play_resumed"]
        )
        tpl = await utils.normalize_template_entities(
            "play_resumed", tpl, lang_code=await db.get_lang(chat_id)
        )
        reply_res = utils.format_template(tpl, user)

    elif action == "skip":
        await query.answer(query.lang["skipped"], show_alert=False)
        await anon.play_next(chat_id)
        status = query.lang["skipped"]
        tpl = await db.get_custom_text_for_chat(
            chat_id, "play_skipped", query.lang["play_skipped"]
        )
        tpl = await utils.normalize_template_entities(
            "play_skipped", tpl, lang_code=await db.get_lang(chat_id)
        )
        try:
            reply_res = utils.format_template(tpl, user)
        except Exception:
            tpl = query.lang["play_skipped"]
            reply_res = utils.format_template(tpl, user)

    elif action == "force":
        # `pos` is an internal 0-based queue index used by `force_add(remove=...)`.
        pos, media = queue.check_item(chat_id, args[3])
        if not media or pos == -1:
            await query.answer(query.lang["play_expired"], show_alert=False)
            return await utils.edit_callback_text(
                query.message, query.lang["play_expired"], ignore_stale=True
            )

        current = queue.get_current(chat_id)
        if not current:
            await query.answer(query.lang["play_expired"], show_alert=False)
            return await utils.edit_callback_text(
                query.message, query.lang["play_expired"], ignore_stale=True
            )
        await query.answer(query.lang["playing"], show_alert=False)
        await anon._delete_now_playing(chat_id, current)
        if media is not current:
            await anon._delete_now_playing(chat_id, media)
        # Dynamic: delete this queue card when user hits Play Now
        try:
            card_id = int(
                getattr(media, "message_id", 0)
                or getattr(media, "status_message_id", 0)
                or getattr(query.message, "id", 0)
                or 0
            )
            if card_id:
                await anon._delete_status_message(
                    chat_id, card_id, reason="force_play_now"
                )
        except Exception:
            pass
        anon.prefetch_manager.cancel(chat_id)
        queue.force_add(chat_id, media, remove=pos)
        media.message_id = 0
        try:
            media.status_message_id = 0
        except Exception:
            pass

        tpl = await db.get_custom_text_for_chat(chat_id, "play_next", query.lang["play_next"])
        if isinstance(tpl, dict):
            msg = await utils.send_message(
                chat_id, tpl["text"], entities=tpl.get("entities")
            )
        else:
            msg = await app.send_message(chat_id=chat_id, text=tpl)
        media.message_id = msg.id
        try:
            media.status_message_id = msg.id
        except Exception:
            pass
        return await anon.play_media(chat_id, msg, media)

    elif action == "replay":
        media = queue.get_current(chat_id)
        if not media:
            await query.answer(query.lang["play_expired"], show_alert=False)
            return await utils.edit_callback_text(
                query.message, query.lang["play_expired"], ignore_stale=True
            )
        media.user = user
        await anon.replay(chat_id)
        await query.answer(query.lang["replayed"], show_alert=False)
        status = query.lang["replayed"]
        tpl = await db.get_custom_text_for_chat(
            chat_id, "play_replayed", query.lang["play_replayed"]
        )
        tpl = await utils.normalize_template_entities(
            "play_replayed", tpl, lang_code=await db.get_lang(chat_id)
        )
        try:
            reply_res = utils.format_template(tpl, user)
        except Exception:
            tpl = query.lang["play_replayed"]
            reply_res = utils.format_template(tpl, user)

    elif action == "stop":
        await query.answer(query.lang["stopped"], show_alert=False)
        await anon.stop(chat_id)
        status = query.lang["stopped"]
        tpl = await db.get_custom_text_for_chat(
            chat_id, "play_stopped", query.lang["play_stopped"]
        )
        tpl = await utils.normalize_template_entities(
            "play_stopped", tpl, lang_code=await db.get_lang(chat_id)
        )
        try:
            reply_res = utils.format_template(tpl, user)
        except Exception:
            tpl = query.lang["play_stopped"]
            reply_res = utils.format_template(tpl, user)

    try:
        reply_text = reply_res["text"] if isinstance(reply_res, dict) else reply_res
        reply_entities = reply_res.get("entities") if isinstance(reply_res, dict) else None

        if action in ["skip", "replay", "stop"]:
            await utils.send_message(
                chat_id=chat_id,
                text=reply_text,
                entities=reply_entities,
            )
            try:
                await query.message.delete()
            except Exception:
                pass
        else:
            media = queue.get_current(chat_id)
            if not media:
                return await utils.edit_callback_text(
                    query.message, query.lang["play_expired"], ignore_stale=True
                )
            original_text, original_entities = await _rebuild_play_media_caption(chat_id, media)
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
            full_entities = utils.sanitize_entities_for_text(
                full_text,
                (original_entities or []) + reply_entities,
            )
            keyboard = buttons.controls(
                chat_id,
                status=status,
                status_style="danger" if action == "pause" else None,
            )
            try:
                if query.message.caption is not None:
                    await utils.edit_caption(
                        query.message,
                        caption=full_text,
                        caption_entities=full_entities or None,
                        reply_markup=keyboard,
                    )
                else:
                    await utils.edit_text(
                        query.message,
                        text=full_text,
                        entities=full_entities or None,
                        reply_markup=keyboard,
                    )
            except bot_api.MessageToEditNotFound:
                return
    except Exception as ex:
        logger.exception("controls callback failed for action=%s chat_id=%s: %s", action, chat_id, ex)


@app.on_callback_query(filters.regex("help") & ~app.bl_users)
@lang.language()
async def _help(_, query: types.CallbackQuery):
    data = query.data.split()
    if len(data) == 1:
        return await query.answer(url=f"https://t.me/{app.username}?start=help")

    if data[1] == "back":
        return await utils.edit_text(
            query.message,
            text=query.lang["help_menu"],
            reply_markup=buttons.help_markup(query.lang),
        )
    elif data[1] == "close":
        try:
            await query.message.delete()
            return await query.message.reply_to_message.delete()
        except Exception:
            return

    help_key = f"help_{data[1]}"
    help_text = sanitize_help_text(query.lang.get(help_key, help_key))
    try:
        await utils.edit_text(
            query.message,
            text=help_text,
            reply_markup=buttons.help_markup(query.lang, True),
        )
    except Exception:
        fallback_text = re.sub(r"</?([a-zA-Z][^>]*)>", "", help_text)
        await utils.edit_text(
            query.message,
            text=escape(fallback_text),
            reply_markup=buttons.help_markup(query.lang, True),
        )


@app.on_callback_query(filters.regex("settings") & ~app.bl_users)
@lang.language()
@admin_check
async def _settings_cb(_, query: types.CallbackQuery):
    cmd = query.data.split()
    if len(cmd) == 1:
        return await query.answer()

    chat_id = query.message.chat.id
    _admin = await db.get_play_mode(chat_id)
    _delete = await db.get_cmd_delete(chat_id)
    _autoplay = await db.get_autoplay(chat_id)
    _language = await db.get_lang(chat_id)

    if cmd[1] == "delete":
        _delete = not _delete
        await db.set_cmd_delete(chat_id, _delete)
        feedback = f"{query.lang['cmd_delete']}: {'ON' if _delete else 'OFF'}"
    elif cmd[1] == "play":
        _admin = not _admin
        await db.set_play_mode(chat_id, _admin)
        feedback = f"{query.lang['play_mode']}: {'ON' if _admin else 'OFF'}"
    elif cmd[1] == "autoplay":
        _autoplay = not _autoplay
        await db.set_autoplay(chat_id, _autoplay)
        feedback = (
            query.lang.get("autoplay_on" if _autoplay else "autoplay_off")
            or f"{query.lang.get('autoplay', 'Autoplay')}: {'ON' if _autoplay else 'OFF'}"
        )
    else:
        return await query.answer()
    await query.answer(feedback, show_alert=False)
    await utils.edit_reply_markup(
        query.message,
        reply_markup=buttons.settings_markup(
            query.lang,
            _admin,
            _delete,
            _autoplay,
            _language,
            chat_id,
        ),
        ignore_stale=True,
    )


@app.on_callback_query(filters.regex(r"^unmute \d+ -?\d+") & ~app.bl_users)
@lang.language()
@admin_check
async def _unmute_cb(_, query: types.CallbackQuery):
    parts = query.data.split()
    target_id = int(parts[1])
    chat_id = int(parts[2])

    try:
        chat = await app.get_chat(chat_id)
        perms = getattr(chat, "permissions", None)
        if isinstance(perms, types.ChatPermissions):
            unmute_perms = perms
        else:
            unmute_perms = types.ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True,
                can_invite_users=True,
            )
    except Exception:
        unmute_perms = types.ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_send_polls=True,
            can_invite_users=True,
        )

    try:
        await app.restrict_chat_member(chat_id, target_id, permissions=unmute_perms)
    except Exception as ex:
        await query.answer(f"Failed: {ex}", show_alert=False)
        return

    await query.answer(query.lang["unmute_done"].format("User"))
    try:
        await query.message.edit_text(
            query.message.text + "\n\n" + query.lang["mod_unmute_completed"],
            reply_markup=None,
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^unban \d+ -?\d+") & ~app.bl_users)
@lang.language()
@admin_check
async def _unban_cb(_, query: types.CallbackQuery):
    parts = query.data.split()
    target_id = int(parts[1])
    chat_id = int(parts[2])

    try:
        await app.unban_chat_member(chat_id, target_id)
    except Exception as ex:
        await query.answer(f"Failed: {ex}", show_alert=False)
        return

    await query.answer(query.lang["unban_done"].format("User"))
    try:
        await query.message.edit_text(
            query.message.text + "\n\n" + query.lang["mod_unban_completed"],
            reply_markup=None,
        )
    except Exception:
        pass
@app.on_callback_query(filters.regex(r"^ban \d+ -?\d+") & ~app.bl_users)
@lang.language()
@admin_check
async def _ban_cb(_, query: types.CallbackQuery):
    parts = query.data.split()
    target_id = int(parts[1])
    chat_id = int(parts[2])

    try:
        await app.ban_chat_member(chat_id, target_id)
    except Exception as ex:
        await query.answer(f"Failed: {ex}", show_alert=False)
        return

    await query.answer(query.lang["ban_done"].format("User"))
    try:
        await query.message.edit_text(
            query.message.text + "\n\n" + query.lang["mod_ban_completed"],
            reply_markup=None,
        )
    except Exception:
        pass
