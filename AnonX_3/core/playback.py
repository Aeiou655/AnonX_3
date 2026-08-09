# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

from contextlib import suppress
import re

from pyrogram.errors import ChatSendMediaForbidden, ChatSendPhotosForbidden, MessageIdInvalid
from pyrogram.types import InputMediaPhoto, Message

from AnonX_3 import app, bot_api, config, db, lang, logger, thumb
from AnonX_3.helpers import buttons, utils


async def _claim_now_playing_card(message, media) -> None:
    """Revoke progress writers before play_media takes over the status card."""
    candidates = (
        message,
        getattr(media, "download_progress_message", None),
    )
    seen: set[tuple[int, int]] = set()
    for candidate in candidates:
        key = utils.download_progress_key(candidate)
        if key is None or key in seen:
            continue
        seen.add(key)
        # Publish the closed state and drain any Telegram edit already in
        # flight.  This is an event-driven ownership barrier, not a delay.
        await utils.close_download_progress(candidate, media)
        try:
            from AnonX_3 import yt

            yt.detach_download_progress(candidate)
        except Exception:
            # Other providers use the same Utilities ownership guard; YouTube
            # detachment is only an eager watcher cleanup.
            pass


async def _drop_orphaned_progress_card(chat_id: int, media, keep_id) -> None:
    """Delete the download-progress card when it is not the now-playing card.

    ``_claim_now_playing_card`` only revokes write ownership, which stops later
    progress callbacks from editing.  When the now-playing card lands on a
    *different* message — a direct→local failover, or the send fallback after a
    failed edit — the progress card is left on screen frozen at its last frame
    (typically "DOWNLOADING… 100.0%" with a live Cancel button).
    """
    progress = getattr(media, "download_progress_message", None)
    key = utils.download_progress_key(progress)
    if key is None:
        return
    progress_chat, progress_id = key
    try:
        keep = int(keep_id or 0)
    except (TypeError, ValueError):
        keep = 0
    if progress_id == keep and progress_chat in (0, chat_id):
        return
    target_chat = progress_chat or chat_id
    with suppress(Exception):
        await app.delete_messages(target_chat, progress_id)
    with suppress(Exception):
        await bot_api.delete_message(target_chat, progress_id)
    with suppress(Exception):
        media.download_progress_message = None


async def update_now_playing(chat_id: int, message: Message, media) -> None:
    _lang = await lang.get_lang(chat_id)
    quality_tier = getattr(media, "stream_tier", None)
    thumb_task = getattr(media, "_now_playing_thumb_task", None)
    if config.THUMB_GEN and thumb_task is not None:
        try:
            _thumb = await thumb_task
        except Exception as ex:
            logger.warning("Background thumbnail generation failed: %s", ex)
            _thumb = None
        finally:
            media._now_playing_thumb_task = None
    else:
        _thumb = (
            await thumb.generate(media, quality_tier=quality_tier)
            if config.THUMB_GEN
            else None
        )
    try:
        text, entities = await _now_playing_text(chat_id, media, _lang)
    except Exception as ex:
        logger.warning(
            "Now-playing template formatting failed in chat %s; using plain fallback: %s",
            chat_id,
            ex,
        )
        safe_title = utils.stringify_template_value(getattr(media, "title", "")) or "Unknown"
        safe_duration = utils.stringify_template_value(getattr(media, "duration", "")) or "Unknown"
        raw_user = utils.stringify_template_value(getattr(media, "user", "")) or "Unknown"
        safe_user = re.sub(r"<[^>]+>", "", raw_user).strip() or "Unknown"
        text = (
            "Now Playing\n\n"
            f"{safe_title[:120]}\n"
            f"Duration: {safe_duration}\n"
            f"Requested by: {safe_user}"
        )
        # An explicit empty list disables Bot API/Pyrogram HTML parsing. The
        # fallback contains untrusted media/user text and must remain literal.
        entities = []
    keyboard = buttons.controls(chat_id)

    async def _send_now_playing():
        sent = None
        try:
            if _thumb:
                sent = await utils.send_photo(
                    chat_id=chat_id,
                    photo=_thumb,
                    caption=text,
                    caption_entities=entities,
                    reply_markup=keyboard,
                )
            else:
                sent = await utils.send_message(
                    chat_id=chat_id,
                    text=text,
                    entities=entities,
                    reply_markup=keyboard,
                )
        except (ChatSendPhotosForbidden, ChatSendMediaForbidden, bot_api.ChatForbidden) as _ex:
            logger.warning(
                "Photo send forbidden in chat %s, falling back to text-only: %s",
                chat_id,
                _ex,
            )
            sent = await utils.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
                reply_markup=keyboard,
            )
        except Exception:
            if _thumb:
                try:
                    sent = await app.send_photo(
                        chat_id=chat_id,
                        photo=_thumb,
                        caption=text,
                    )
                except (ChatSendPhotosForbidden, ChatSendMediaForbidden, bot_api.ChatForbidden):
                    sent = await app.send_message(
                        chat_id=chat_id,
                        text=text,
                        entities=entities,
                        reply_markup=keyboard,
                    )
            else:
                sent = await app.send_message(
                    chat_id=chat_id,
                    text=text,
                )
        media.message_id = sent.id
        # Drop temporary searching / downloading / queued / play_next status
        # when a fresh now-playing card is posted (dynamic — when play starts).
        if message is not None and getattr(message, "id", None) != getattr(sent, "id", None):
            with suppress(Exception):
                await message.delete()
            with suppress(Exception):
                from AnonX_3 import bot_api

                await bot_api.delete_message(chat_id, message.id)
        # Also clear any extra status id stashed on media
        extra = int(getattr(media, "status_message_id", 0) or 0)
        if extra and extra != sent.id and (
            message is None or extra != getattr(message, "id", None)
        ):
            with suppress(Exception):
                await app.delete_messages(chat_id, extra)
            with suppress(Exception):
                from AnonX_3 import bot_api

                await bot_api.delete_message(chat_id, extra)
        try:
            media.status_message_id = sent.id
        except Exception:
            pass
        return sent

    try:
        await _claim_now_playing_card(message, media)
    except Exception as ex:
        logger.warning(
            "Failed to claim now-playing card, continuing anyway: %s",
            ex,
        )
    try:
        if _thumb:
            await utils.edit_media(
                message,
                media=InputMediaPhoto(
                    media=_thumb,
                    caption=text,
                    caption_entities=entities,
                ),
                reply_markup=keyboard,
            )
        else:
            await utils.edit_text(
                message, text, entities=entities, reply_markup=keyboard
            )
        media.message_id = message.id
    except (
        ChatSendMediaForbidden,
        ChatSendPhotosForbidden,
        MessageIdInvalid,
        bot_api.MessageToEditNotFound,
        bot_api.ChatForbidden,
    ):
        await _send_now_playing()
    except Exception as ex:
        logger.warning(
            "Falling back to a new now-playing message after edit failure: %s",
            ex,
        )
        await _send_now_playing()

    # Both branches above set media.message_id to whichever message ended up
    # carrying the card, so this one call covers the edit path and the send
    # fallback.  It runs outside the try so it can never trip the fallback.
    await _drop_orphaned_progress_card(
        chat_id, media, getattr(media, "message_id", 0)
    )


async def _now_playing_text(chat_id: int, media, _lang: dict):
    play_media_tpl = await db.get_custom_text_for_chat(
        chat_id, "play_media", _lang["play_media"]
    )
    play_media_tpl = await utils.normalize_template_entities(
        "play_media", play_media_tpl, lang_code=await db.get_lang(chat_id)
    )
    try:
        from AnonX_3.helpers._play import format_play_media_template

        play_media_res = format_play_media_template(play_media_tpl, media)
    except Exception:
        play_media_res = utils.format_template(
            play_media_tpl,
            media.url,
            media.title,
            media.duration,
            media.user,
            url=media.url or "",
            title=media.title or "",
            duration=media.duration or "",
            user=media.user or "",
        )
    if isinstance(play_media_res, dict):
        text = play_media_res["text"]
        _, _, entities = utils.deserialize_entities(play_media_res["entities"])
        text, entities = utils.auto_link_title(text, entities, media.title, media.url)
        return text, entities
    return play_media_res, None
