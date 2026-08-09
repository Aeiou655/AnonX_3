# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

import asyncio
import logging
import re

from pyrogram import enums, errors, filters, types

from AnonX_3 import app, db, lang
from AnonX_3.helpers import admin_check, utils

_log = logging.getLogger(__name__)

_LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/)",
    re.IGNORECASE,
)
_LINK_NOTICE_TTL = 12


def _unmute_permissions(chat: types.Chat) -> types.ChatPermissions:
    perms = getattr(chat, "permissions", None)
    if isinstance(perms, types.ChatPermissions):
        return perms
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_send_polls=True,
        can_invite_users=True,
    )


async def _is_group_admin(chat_id: int, user_id: int) -> bool:
    if user_id in app._sudo_ids:
        return True

    admins = await db.get_admins(chat_id)
    if user_id in admins:
        return True

    try:
        member = await app.get_chat_member(chat_id, user_id)
    except Exception:
        return False

    return member.status in {
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    }


async def _resolve_target(m: types.Message) -> types.User | None:
    target = await utils.extract_user(m)
    if target:
        return target
    return None


async def _validate_target(m: types.Message) -> types.User | None:
    target = await _resolve_target(m)
    if not target:
        await m.reply_text(m.lang["mod_usage"].format(m.command[0]))
        return None

    if target.id == m.from_user.id:
        await m.reply_text(m.lang["mod_cannot_self"])
        return None

    if target.id == app.id:
        await m.reply_text(m.lang["mod_cannot_bot"])
        return None

    if await _is_group_admin(m.chat.id, target.id):
        await m.reply_text(m.lang["mod_cannot_admin"])
        return None

    return target


async def _moderation_error(m: types.Message, action: str, ex: Exception) -> None:
    if any(
        key in str(ex).upper()
        for key in (
            "CHAT_ADMIN_REQUIRED",
            "RIGHT_FORBIDDEN",
            "USER_ADMIN_INVALID",
            "MESSAGE_DELETE_FORBIDDEN",
        )
    ):
        await m.reply_text(m.lang["mod_bot_no_rights"])
        return

    await m.reply_text(m.lang["mod_failed"].format(action, str(ex)))


async def _cleanup_notice(message: types.Message, delay: int = _LINK_NOTICE_TTL) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


async def _send_delete_notice(m: types.Message, template_key: str, fallback: str) -> None:
    template = await db.get_custom_text_for_chat(
        m.chat.id,
        template_key,
        fallback,
    )
    try:
        notice = await utils.send_formatted(
            m.chat.id,
            template,
            getattr(app, "name", "Bot"),
            getattr(m.from_user, "mention", "User"),
            template_key=template_key,
        )
    except Exception:
        _log.warning("%s: send_formatted failed", template_key, exc_info=True)
        return

    asyncio.create_task(_cleanup_notice(notice))


async def _send_link_delete_notice(m: types.Message) -> None:
    await _send_delete_notice(m, "link_deleted_notice", m.lang["link_deleted_notice"])


async def _send_forward_delete_notice(m: types.Message) -> None:
    await _send_delete_notice(
        m,
        "forward_deleted_notice",
        m.lang.get(
            "forward_deleted_notice",
            "<b>{0}</b> removed a forwarded message.\n"
            "<b>Reason:</b> Forwarded posts are not allowed here.",
        ),
    )


@app.on_message(
    filters.command(["kick"])
    & filters.group
    & ~filters.chat(app.logger)
    & ~app.bl_users
)
@lang.language()
@admin_check
async def kick_hndlr(_, m: types.Message):
    target = await _validate_target(m)
    if not target:
        return

    try:
        await app.ban_chat_member(m.chat.id, target.id)
        await app.unban_chat_member(m.chat.id, target.id)
    except Exception as ex:
        await _moderation_error(m, "kick", ex)
        return

    await m.reply_text(m.lang["kick_done"].format(target.mention))


@app.on_message(filters.command(["ban"]) & filters.group & ~app.bl_users)
@lang.language()
@admin_check
async def ban_hndlr(_, m: types.Message):
    target = await _validate_target(m)
    if not target:
        return

    try:
        await app.ban_chat_member(m.chat.id, target.id)
    except Exception as ex:
        await _moderation_error(m, "ban", ex)
        return

    await m.reply_text(
        m.lang["ban_done"].format(target.mention),
        reply_markup=types.InlineKeyboardMarkup(
            [[types.InlineKeyboardButton(
                m.lang["mod_unban_button"],
                callback_data=f"unban {target.id} {m.chat.id}"
            )]]
        ),
    )


@app.on_message(filters.command(["unban"]) & filters.group & ~app.bl_users)
@lang.language()
@admin_check
async def unban_hndlr(_, m: types.Message):
    target = await _validate_target(m)
    if not target:
        return

    try:
        await app.unban_chat_member(m.chat.id, target.id)
    except Exception as ex:
        await _moderation_error(m, "unban", ex)
        return

    await m.reply_text(
        m.lang["unban_done"].format(target.mention),
        reply_markup=types.InlineKeyboardMarkup(
            [[types.InlineKeyboardButton(
                m.lang["mod_ban_button"],
                callback_data=f"ban {target.id} {m.chat.id}"
            )]]
        ),
    )


@app.on_message(filters.command(["mute"]) & filters.group & ~app.bl_users)
@lang.language()
@admin_check
async def mute_hndlr(_, m: types.Message):
    target = await _validate_target(m)
    if not target:
        return

    permissions = types.ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_send_polls=False,
        can_invite_users=False,
    )

    try:
        await app.delete_user_history(m.chat.id, target.id)
    except Exception:
        pass

    try:
        await app.restrict_chat_member(m.chat.id, target.id, permissions=permissions)
    except Exception as ex:
        await _moderation_error(m, "mute", ex)
        return

    await m.reply_text(
        m.lang["mute_done"].format(target.mention),
        reply_markup=types.InlineKeyboardMarkup(
            [[types.InlineKeyboardButton(
                m.lang["mod_unmute_button"],
                callback_data=f"unmute {target.id} {m.chat.id}"
            )]]
        ),
    )


@app.on_message(filters.command(["unmute"]) & filters.group & ~app.bl_users)
@lang.language()
@admin_check
async def unmute_hndlr(_, m: types.Message):
    target = await _validate_target(m)
    if not target:
        return

    try:
        await app.restrict_chat_member(
            m.chat.id,
            target.id,
            permissions=_unmute_permissions(m.chat),
        )
    except Exception as ex:
        await _moderation_error(m, "unmute", ex)
        return

    await m.reply_text(m.lang["unmute_done"].format(target.mention))


def _message_has_link(m: types.Message) -> bool:
    entities = list(getattr(m, "entities", None) or [])
    entities.extend(getattr(m, "caption_entities", None) or [])

    for entity in entities:
        if entity.type in {
            enums.MessageEntityType.URL,
            enums.MessageEntityType.TEXT_LINK,
        }:
            return True

    text = getattr(m, "text", None) or getattr(m, "caption", None) or ""
    return bool(_LINK_RE.search(text))


def _message_is_forwarded(m: types.Message) -> bool:
    return any(
        getattr(m, attr, None)
        for attr in (
            "forward_date",
            "forward_from",
            "forward_from_chat",
            "forward_sender_name",
            "forward_from_message_id",
            "forward_origin",
        )
    )


@app.on_message(filters.group & ~filters.service & ~app.bl_users, group=27)
@lang.language()
async def auto_forward_delete(_, m: types.Message):
    if not m.from_user or m.from_user.is_bot:
        return

    if await _is_group_admin(m.chat.id, m.from_user.id):
        return

    if not _message_is_forwarded(m):
        return

    try:
        await m.delete()
    except (
        errors.Forbidden,
        errors.ChatAdminRequired,
        errors.MessageDeleteForbidden,
        errors.MessageIdInvalid,
    ):
        return

    await _send_forward_delete_notice(m)


@app.on_message(filters.group & ~filters.service & ~app.bl_users, group=28)
@lang.language()
async def auto_link_delete(_, m: types.Message):
    if not m.from_user or m.from_user.is_bot:
        return

    if await _is_group_admin(m.chat.id, m.from_user.id):
        return

    if not _message_has_link(m):
        return

    try:
        await m.delete()
    except (
        errors.Forbidden,
        errors.ChatAdminRequired,
        errors.MessageDeleteForbidden,
        errors.MessageIdInvalid,
    ):
        return

    await _send_link_delete_notice(m)
