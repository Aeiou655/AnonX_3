# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import re
from html import escape

from pyrogram import enums, filters, types

from AnonX_3 import app, db, lang
from AnonX_3.helpers import is_admin, utils

# Default /id template (/settext id_user):
# User ID:{0}
# Username:{1}
# Name:{2}
# Group:{3}
_DEFAULT_ID_USER = (
    "User ID:{0}\n"
    "Username:{1}\n"
    "Name:{2}\n"
    "Group:{3}"
)

# Strip accidental trailing braces after placeholders, e.g. {0}} → {0}
_PLACEHOLDER_EXTRA_BRACE = re.compile(
    r"(\{(?:\d+|[a-zA-Z_][a-zA-Z0-9_]*)(?:![^}:]+)?(?::[^{}]+)?\})\}+"
)


def _clean_id_template(template: str | dict) -> str | dict:
    """Remove extra '}' after placeholders so output does not show a stray brace."""
    if isinstance(template, dict):
        text = template.get("text", "")
        if isinstance(text, str):
            cleaned = _PLACEHOLDER_EXTRA_BRACE.sub(r"\1", text)
            # drop bare trailing }} leftovers used as escapes for empty content
            cleaned = cleaned.replace("}}", "}")
            # but keep intentional single braces out of placeholders — only collapse doubles
            # Re-run: if someone typed {0}} we already fixed; remaining lone } after digits is rare
            return {**template, "text": cleaned}
        return template
    if isinstance(template, str):
        cleaned = _PLACEHOLDER_EXTRA_BRACE.sub(r"\1", template)
        return cleaned
    return template


def _username(user_or_chat) -> str:
    username = getattr(user_or_chat, "username", None)
    return f"@{username}" if username else "None"


def _display_name(user: types.User) -> str:
    try:
        return user.mention
    except Exception:
        name = escape(user.first_name or "")
        if user.last_name:
            name = f"{name} {escape(user.last_name)}".strip()
        return name or "Unknown"


def _group_label(m: types.Message) -> str:
    chat = m.chat
    if not chat:
        return "Unknown"
    if chat.type in {enums.ChatType.PRIVATE, enums.ChatType.BOT}:
        return "Private"
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
    if title:
        return escape(str(title))
    return str(chat.id)


async def _reply_template(m: types.Message, key: str, fallback: str, *args):
    lang_code = await db.get_lang(m.chat.id)
    tpl = await db.get_custom_text(key, fallback, lang_code)
    tpl = _clean_id_template(tpl)
    tpl = await utils.normalize_template_entities(key, tpl, lang_code=lang_code)
    tpl = _clean_id_template(tpl)
    res = utils.format_template(tpl, *args)
    if isinstance(res, dict):
        text = res.get("text", "")
        # Final safety: remove a lone stray "}" that appears only as format artifact
        # immediately after pure digit IDs is already correct; only strip }} pairs left.
        if isinstance(text, str) and "}}" in text:
            text = text.replace("}}", "}")
            res = {**res, "text": text}
        return await utils.reply_text(m, res["text"], entities=res.get("entities"))
    if isinstance(res, str) and "}}" in res:
        res = res.replace("}}", "}")
    return await utils.reply_text(m, res)


async def _allowed(m: types.Message) -> bool:
    """Allow sudoers, group admins, and group owners only."""
    user = m.from_user
    if not user:
        return False
    if user.id in app._sudo_ids:
        return True
    if m.chat.type in {enums.ChatType.PRIVATE, enums.ChatType.BOT}:
        return False
    try:
        if user.id in await db.get_admins(m.chat.id):
            return True
    except Exception:
        pass
    try:
        return await is_admin(m.chat.id, user.id)
    except Exception:
        return False


@app.on_message(filters.command(["id"]) & ~app.bl_users)
@lang.language()
async def _id_command(_, m: types.Message):
    """Reply to a message with /id → User ID / Username / Name / Group."""
    if not await _allowed(m):
        return await utils.reply_text(
            m, m.lang.get("user_no_perms", "You do not have permission.")
        )

    group = _group_label(m)
    replied = m.reply_to_message
    fallback = m.lang.get("id_user", _DEFAULT_ID_USER)

    if not replied:
        if m.from_user:
            return await _reply_template(
                m,
                "id_user",
                fallback,
                m.from_user.id,
                _username(m.from_user),
                _display_name(m.from_user),
                group,
            )
        return await utils.reply_text(
            m,
            m.lang.get("id_usage", "Reply to a user's message with /id"),
        )

    if replied.from_user:
        return await _reply_template(
            m,
            "id_user",
            fallback,
            replied.from_user.id,
            _username(replied.from_user),
            _display_name(replied.from_user),
            group,
        )

    if replied.sender_chat:
        chat = replied.sender_chat
        title = escape(
            getattr(chat, "title", None)
            or getattr(chat, "first_name", None)
            or "Unknown"
        )
        return await _reply_template(
            m,
            "id_user",
            fallback,
            chat.id,
            _username(chat),
            title,
            group,
        )

    if replied.forward_from:
        user = replied.forward_from
        return await _reply_template(
            m,
            "id_user",
            fallback,
            user.id,
            _username(user),
            _display_name(user),
            group,
        )

    if replied.forward_from_chat:
        chat = replied.forward_from_chat
        title = escape(
            getattr(chat, "title", None)
            or getattr(chat, "first_name", None)
            or "Unknown"
        )
        return await _reply_template(
            m,
            "id_user",
            fallback,
            chat.id,
            _username(chat),
            title,
            group,
        )

    return await utils.reply_text(
        m,
        m.lang.get("id_no_target", "No user/chat found on that message."),
    )
