# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import asyncio
import re
from urllib.parse import urlparse

from pyrogram import enums, filters, types

from AnonX_3 import app, bot_api, config, db, lang, logger
from AnonX_3.helpers import buttons, utils

_INVALID_START_SOURCES: set[str] = set()


def _is_invalid_photo_error(ex: Exception) -> bool:
    text = str(ex).lower()
    return (
        "wrong file identifier/http url specified" in text
        or "wrong file identifier" in text
        or "photo_invalid" in text
    )


def _is_peer_invalid_error(ex: Exception) -> bool:
    return "PEER_ID_INVALID" in str(ex)


def _normalize_photo_source(photo) -> str | None:
    if not isinstance(photo, str):
        return None
    value = photo.strip()
    return value or None


def _looks_like_bad_http_photo_source(photo: str) -> bool:
    parsed = urlparse(photo)
    return parsed.scheme in {"http", "https"} and not parsed.netloc


def _replace_start_placeholders(template: str, user_name: str, bot_name: str) -> str:
    mapping = {
        "0": user_name,
        "1": bot_name,
        "username": user_name,
        "botname": bot_name,
    }
    pattern = re.compile(
        r"\{(?P<key>0|1|username|botname)\}\}?"
        r"|\b(?P<key2>0|1|username|botname)\}",
        re.IGNORECASE,
    )

    def repl(match):
        key = (match.group("key") or match.group("key2")).lower()
        return mapping.get(key, match.group(0))

    return pattern.sub(repl, template)


def _prefer_pyrogram_start_markup(reply_markup, entities):
    converted, _ = utils.maybe_convert_bot_api_markup(reply_markup, entities)
    return converted


async def _send_start_photo(
    message: types.Message,
    photo: str,
    caption: str,
    caption_entities,
    reply_markup,
    quote: bool,
) -> tuple[bool, bool]:
    try:
        await utils.reply_photo(
            message,
            photo=photo,
            caption=caption,
            caption_entities=caption_entities,
            reply_markup=reply_markup,
            quote=quote,
        )
        return True, False
    except bot_api.ChatForbidden as ex:
        logger.warning("Skipping /start photo send due to chat restrictions: %s", ex)
        return False, False
    except Exception as ex:
        if _is_peer_invalid_error(ex):
            logger.warning("Skipping /start photo send due to invalid peer: %s", ex)
            return False, False
        if _is_invalid_photo_error(ex):
            logger.warning("Invalid /start image source detected: %s", ex)
            return False, True
        logger.exception("Failed to send /start photo with entities: %s", ex)
        try:
            await utils.reply_photo(
                message,
                photo=photo,
                caption=caption,
                reply_markup=reply_markup,
                quote=quote,
            )
            return True, False
        except bot_api.ChatForbidden as ex2:
            logger.warning("Skipping /start photo send due to chat restrictions: %s", ex2)
            return False, False
        except Exception as ex2:
            if _is_peer_invalid_error(ex2):
                logger.warning("Skipping /start photo send due to invalid peer: %s", ex2)
                return False, False
            if _is_invalid_photo_error(ex2):
                logger.warning("Invalid /start image source detected: %s", ex2)
                return False, True
            logger.exception("Failed to send /start photo without entities: %s", ex2)
            return False, False


@app.on_message(filters.command(["help"]) & filters.private & ~app.bl_users)
@lang.language()
async def _help(_, m: types.Message):
    await utils.reply_text(
        m,
        text=m.lang["help_menu"],
        reply_markup=buttons.help_markup(m.lang),
        quote=True,
    )


@app.on_message(filters.command(["start"]))
@lang.language()
async def start(_, message: types.Message):
    if (
        message.from_user
        and message.from_user.id in app.bl_users
        and message.from_user.id not in db.notified
    ):
        return await message.reply_text(message.lang["bl_user_notify"])

    if len(message.command) > 1 and message.command[1] == "help":
        return await _help(_, message)

    private = message.chat.type == enums.ChatType.PRIVATE
    lang_code = await db.get_lang(message.chat.id)
    user_name = (
        message.from_user.first_name
        if message.from_user and message.from_user.first_name
        else (message.chat.title or app.name)
    )
    template_lang_code = lang_code if private else None
    template = await db.get_custom_text(
        "start_pm", message.lang["start_pm"], template_lang_code
    )
    template = await utils.normalize_template_entities(
        "start_pm", template, lang_code=template_lang_code
    )
    if isinstance(template, dict):
        formatted = utils.format_template(
            template,
            user_name,
            app.name,
            username=user_name,
            botname=app.name,
        )
        _text = formatted["text"]
        _entities = formatted.get("entities")
    else:
        _text = _replace_start_placeholders(
            template,
            user_name,
            app.name,
        )
        _entities = None

    # Use the same branded /setwelcome template and private start buttons in
    # groups, so group /start matches the bot DM /start instead of start_gp.
    key = buttons.start_key(message.lang, True)
    key = _prefer_pyrogram_start_markup(key, _entities)
    image_candidates: list[tuple[str, str]] = []

    # Load bot image cache once and keep DB custom source separate from config fallback.
    if not db.bot_images:
        await db.get_bot_image("start_img")

    db_start_raw = db.bot_images.get("start_img")
    start_image_disabled = db_start_raw == "__disabled__"
    db_start_img = _normalize_photo_source(db_start_raw)
    if db_start_img and not start_image_disabled:
        image_candidates.append(("db", db_start_img))

    config_start_img = _normalize_photo_source(config.START_IMG)
    if config_start_img and not start_image_disabled:
        image_candidates.append(("config", config_start_img))

    sent = False
    for source, photo in image_candidates:
        if source == "db" and photo in _INVALID_START_SOURCES:
            continue

        if _looks_like_bad_http_photo_source(photo):
            logger.warning("Skipping malformed /start image URL from %s source: %s", source, photo)
            _INVALID_START_SOURCES.add(photo)
            if source == "db":
                try:
                    await db.set_bot_image("start_img", "")
                except Exception as clear_ex:
                    logger.warning("Failed to clear malformed DB start_img: %s", clear_ex)
            continue

        sent, invalid_photo = await _send_start_photo(
            message=message,
            photo=photo,
            caption=_text,
            caption_entities=_entities,
            reply_markup=key,
            quote=False,
        )
        if sent:
            break
        if invalid_photo:
            _INVALID_START_SOURCES.add(photo)
            if source == "db":
                try:
                    await db.set_bot_image("start_img", "")
                    logger.warning("Cleared invalid DB start_img source after sendPhoto failure.")
                except Exception as clear_ex:
                    logger.warning("Failed to clear invalid DB start_img source: %s", clear_ex)

    if not sent:
        try:
            await utils.reply_text(
                message,
                text=_text,
                entities=_entities,
                reply_markup=key,
                quote=False,
                disable_web_page_preview=False,
                link_preview_options={"is_disabled": False, "show_above_text": True},
            )
        except (bot_api.ChatForbidden, RuntimeError) as ex3:
            logger.warning("Skipping /start text fallback: %s", ex3)
            return
        except Exception as ex3:
            logger.exception("Failed to send /start text fallback: %s", ex3)
            return

    if private:
        if await db.is_user(message.from_user.id):
            await db.touch_audience(
                peer_id=message.from_user.id,
                peer_type="user",
                source="start",
                blocked=False,
                is_active=True,
            )
            return
        await utils.send_log(message)
        await db.add_user(message.from_user.id)
        await db.touch_audience(
            peer_id=message.from_user.id,
            peer_type="user",
            source="start",
            blocked=False,
            is_active=True,
        )
    else:
        if await db.is_chat(message.chat.id):
            await db.touch_audience(
                peer_id=message.chat.id,
                peer_type="group",
                source="start_group",
                is_active=True,
            )
            return
        await utils.send_log(message, True)
        await db.add_chat(message.chat.id)
        await db.touch_audience(
            peer_id=message.chat.id,
            peer_type="group",
            source="start_group",
            is_active=True,
        )


@app.on_message(filters.command(["playmode", "settings"]) & filters.group & ~app.bl_users)
@lang.language()
async def settings(_, message: types.Message):
    admin_only = await db.get_play_mode(message.chat.id)
    cmd_delete = await db.get_cmd_delete(message.chat.id)
    autoplay = await db.get_autoplay(message.chat.id)
    _language = await db.get_lang(message.chat.id)
    await utils.reply_text(
        message,
        text=message.lang["start_settings"].format(message.chat.title),
        reply_markup=buttons.settings_markup(
            message.lang, admin_only, cmd_delete, autoplay, _language, message.chat.id
        ),
        quote=True,
    )


@app.on_message(filters.new_chat_members, group=7)
@lang.language()
async def _new_member(_, message: types.Message):
    if message.chat.type != enums.ChatType.SUPERGROUP:
        return await message.chat.leave()

    await asyncio.sleep(3)
    for member in message.new_chat_members:
        if member.id == app.id:
            if await db.is_chat(message.chat.id):
                await db.touch_audience(
                    peer_id=message.chat.id,
                    peer_type="group",
                    source="bot_added",
                    is_active=True,
                )
                return
            await utils.send_log(message, True)
            await db.add_chat(message.chat.id)
            await db.touch_audience(
                peer_id=message.chat.id,
                peer_type="group",
                source="bot_added",
                is_active=True,
            )

