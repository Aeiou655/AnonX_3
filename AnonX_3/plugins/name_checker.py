# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲

from html import escape
import time

from pyrogram import enums, filters, types
from pymongo.errors import ServerSelectionTimeoutError

from AnonX_3 import app, db, lang, logger
from AnonX_3.helpers import utils

DEFAULT_NAME_CHECKER_TEMPLATE = (
    "🔎 Name changed\n"
    "{2} ({3}) -> {5} ({6})\n"
    "ID: <code>{4}</code>\n"
    "Group: {0}"
)


def _clean(value: str | None) -> str:
    return (value or "").strip()


# ── Premium Emoji Support ───────────────────────────────────────
# Telegram Premium users can put custom (animated) emojis in their
# display name.  Pyrogram exposes those as PUA placeholder
# characters inside first_name / last_name, but does **not** provide
# the custom_emoji_id document reference needed to render them.
#
# When a custom_emoji_id can be resolved the placeholder is wrapped
# in an <emoji id="…"> HTML tag that the existing format_template /
# parse_html_entities pipeline already handles.  When it cannot be
# resolved (which is the default today) the character is replaced
# with a visible fallback so nothing displays as blank / tofu.

PUA_EMOJI_FALLBACK = "[emoji]"
_ALERT_PERMISSION_BLOCKED_UNTIL: dict[int, float] = {}
_ALERT_PERMISSION_COOLDOWN_SEC = 3600.0

# Per-user cache:  user_id -> { PUA_codepoint -> custom_emoji_id_str }
_emoji_id_cache: dict[int, dict[int, str]] = {}


def _short_db_error(ex: Exception, limit: int = 220) -> str:
    text = str(ex or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _resolve_emoji_id(user_id: int, pua_char: str) -> str | None:
    """Best-effort resolve a PUA placeholder to a custom_emoji_id.

    Currently always returns *None* because no public Bot API or
    MTProto method exposes the name-entity mapping.  The function
    is a designed extension point for future strategies:
      - Bot API ``getChatMember`` (if enriched in a later version)
      - Raw MTProto ``users.getFullUser``
      - Userbot-based profile extraction
      - Correlation from message-text entities
    """
    cache = _emoji_id_cache.get(user_id)
    if cache:
        emoji_id = cache.get(ord(pua_char))
        if emoji_id is not None:
            return emoji_id
    return None


def _name_to_html(name: str, user_id: int | None = None) -> str:
    """Convert a display name to safe HTML for use in alert templates.

    * Normal characters are HTML-escaped (unchanged behaviour).
    * Resolvable PUA placeholder chars → ``<emoji id="X">…</emoji>``.
    * Unresolvable PUA placeholder chars → ``PUA_EMOJI_FALLBACK``.
    """
    if not name:
        return ""
    if not utils.has_premium_emoji(name):
        return escape(name, quote=False)

    parts: list[str] = []
    for c in name:
        if utils.is_premium_emoji_char(c):
            emoji_id = _resolve_emoji_id(user_id, c) if user_id else None
            if emoji_id:
                parts.append(
                    f'<emoji id="{emoji_id}">{escape(c, quote=False)}</emoji>'
                )
            else:
                parts.append(PUA_EMOJI_FALLBACK)
        else:
            parts.append(escape(c, quote=False))
    return "".join(parts)


def _full_name(user: types.User) -> str:
    return " ".join(
        part for part in (_clean(user.first_name), _clean(user.last_name)) if part
    ).strip()


def _profile_from_user(user: types.User) -> dict:
    return {
        "user_id": user.id,
        "first_name": _clean(user.first_name),
        "last_name": _clean(user.last_name),
        "full_name": _full_name(user),
        "username": _clean(user.username),
    }


def _display_name(profile: dict | None) -> str:
    if not profile:
        return "None"
    return profile.get("full_name") or profile.get("first_name") or "None"


def _display_username(profile: dict | None) -> str:
    if not profile:
        return "None"
    username = _clean(profile.get("username"))
    return f"@{username}" if username else "None"


def _html(value: str) -> str:
    return escape(str(value), quote=False)


def _changed(previous: dict, current: dict) -> bool:
    return (
        _display_name(previous) != _display_name(current)
        or _display_username(previous) != _display_username(current)
    )


def _format_args(message: types.Message, previous: dict, current: dict) -> tuple:
    chat_title = getattr(message.chat, "title", None) or "Unknown group"
    chat_id = getattr(message.chat, "id", "Unknown")
    user_id = current["user_id"]
    return (
        _html(chat_title),
        chat_id,
        _name_to_html(_display_name(previous), user_id),
        _html(_display_username(previous)),
        user_id,
        _name_to_html(_display_name(current), user_id),
        _html(_display_username(current)),
        user_id,
    )


async def _safe_send(chat_id: int, args: tuple, *, label: str) -> None:
    now = time.monotonic()
    if now < _ALERT_PERMISSION_BLOCKED_UNTIL.get(int(chat_id), 0.0):
        return
    try:
        template = await db.get_custom_text_for_chat(
            chat_id,
            "name_checker",
            DEFAULT_NAME_CHECKER_TEMPLATE,
        )
        await utils.send_formatted(
            chat_id,
            template,
            *args,
            template_key="name_checker",
            disable_web_page_preview=True,
            _quiet_permission_error=True,
        )
    except Exception as ex:
        if utils.is_chat_forbidden_error(ex):
            _ALERT_PERMISSION_BLOCKED_UNTIL[int(chat_id)] = (
                time.monotonic() + _ALERT_PERMISSION_COOLDOWN_SEC
            )
            logger.warning(
                "Name checker alert paused for %s chat=%s; bot cannot send text there",
                _ALERT_PERMISSION_COOLDOWN_SEC,
                chat_id,
            )
            return
        logger.warning("Name checker failed to send %s alert to %s: %s", label, chat_id, ex)


def _parse_toggle_arg(arg: str) -> bool | None:
    states = {
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
    return states.get(arg)


async def _can_manage_name_checker(message: types.Message) -> bool:
    user = message.from_user
    if not user:
        return False
    if user.id in app._sudo_ids:
        return True
    admins = await db.get_admins(message.chat.id)
    if user.id in admins:
        return True
    try:
        member = await app.get_chat_member(message.chat.id, user.id)
    except Exception:
        return False
    return member.status in {
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    }


async def _name_checker_usage(message: types.Message) -> str:
    enabled = await db.is_name_checker_for_chat(message.chat.id)
    current = "on" if enabled else "off"
    return message.lang.get(
        "check_usage",
        "<b>Usage:</b>\n\n/{0} [on|off] to toggle name checker for this group.\nReply to a user's message with /{0} to check immediately.\n\n<b>Current:</b> <code>{1}</code>",
    ).format(message.command[0], current)


@app.on_message(filters.group & ~app.bl_users, group=998)
async def _name_checker(_, message: types.Message):
    user = message.from_user
    if not user or getattr(user, "is_bot", False):
        return
    try:
        if not await db.is_name_checker_for_chat(message.chat.id):
            return

        current = _profile_from_user(user)
        previous = await db.update_name_profile(current)
    except ServerSelectionTimeoutError as ex:
        logger.warning(
            "Name checker skipped because MongoDB is temporarily unavailable: %s",
            _short_db_error(ex),
        )
        return
    if not previous or not _changed(previous, current):
        return

    args = _format_args(message, previous, current)
    await _safe_send(message.chat.id, args, label="group")
    if app.logger != message.chat.id:
        await _safe_send(app.logger, args, label="logger")


@app.on_message(filters.command(["check"]) & filters.group & ~app.bl_users)
@lang.language()
async def _manual_name_check(_, message: types.Message):
    raw_arg = " ".join(message.command[1:]).strip().lower()
    state = _parse_toggle_arg(raw_arg) if raw_arg else None
    if raw_arg:
        if state is None:
            return await message.reply_text(await _name_checker_usage(message))
        if not await _can_manage_name_checker(message):
            return await message.reply_text(message.lang["user_no_perms"])
        await db.set_name_checker(message.chat.id, state)
        key = "check_on" if state else "check_off"
        fallback = "Name checker is now ON for this group." if state else "Name checker is now OFF for this group."
        return await message.reply_text(message.lang.get(key, fallback))

    if not await _can_manage_name_checker(message):
        return await message.reply_text(message.lang["user_no_perms"])

    replied = message.reply_to_message
    if not replied or not replied.from_user:
        return await message.reply_text(await _name_checker_usage(message))

    user = replied.from_user
    if getattr(user, "is_bot", False):
        return await message.reply_text(
            message.lang.get("check_user_only", "Name checker only supports user accounts.")
        )

    current = _profile_from_user(user)
    previous = await db.update_name_profile(current)
    if previous and _changed(previous, current):
        args = _format_args(message, previous, current)
        await _safe_send(message.chat.id, args, label="manual group")
        if app.logger != message.chat.id:
            await _safe_send(app.logger, args, label="manual logger")
        return

    await message.reply_text(message.lang.get("check_no_change", "Nothing changed !"))
