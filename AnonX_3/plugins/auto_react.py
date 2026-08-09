# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

import logging
import random
import re
import time

from pyrogram import errors, filters, types

from AnonX_3 import app, db, lang
from AnonX_3.helpers import admin_check

_log = logging.getLogger(__name__)
_REACTIONS = ("👍", "❤️", "🔥", "🥰", "👏", "🎉", "🤩", "🙏", "👌", "⚡", "🤝", "😁")
_cooldowns: dict[int, float] = {}
_last_reactions: dict[int, str] = {}
_IGNORED_ERRORS = (
    "REACTION_INVALID",
    "REACTIONS_TOO_MANY",
    "CHAT_REACTIONS_NONE",
    "MESSAGE_ID_INVALID",
    "MESSAGE_NOT_MODIFIED",
    "BOT_METHOD_INVALID",
    "CHAT_WRITE_FORBIDDEN",
    "PEER_ID_INVALID",
)


@app.on_message(filters.command(["autoreact"]) & filters.group & ~app.bl_users)
@lang.language()
@admin_check
async def auto_react_hndlr(_, m: types.Message):
    is_sudo = m.from_user.id in app._sudo_ids
    enabled = (
        await db.get_auto_react_global()
        if is_sudo
        else await db.get_auto_react(m.chat.id)
    )
    raw_arg = " ".join(m.command[1:]).strip().lower()
    arg = re.split(r"\s+", raw_arg, maxsplit=1)[0] if raw_arg else ""
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

    if len(m.command) == 1:
        new_state = not enabled
    elif arg in states:
        new_state = states[arg]
    else:
        return await m.reply_text(
            m.lang.get(
                "auto_react_usage",
                "<b>Usage:</b>\n\n<code>/{0}</code> or <code>/{0} [on|off]</code>",
            ).format(m.command[0])
        )

    if is_sudo:
        await db.set_auto_react_all(new_state)
        _cooldowns.clear()
    else:
        await db.set_auto_react(m.chat.id, new_state)
        _cooldowns.pop(m.chat.id, None)
    key = "auto_react_on" if new_state else "auto_react_off"
    if is_sudo:
        fallback = (
            "Auto reaction is now ON for all groups."
            if new_state
            else "Auto reaction is now OFF for all groups."
        )
    else:
        fallback = (
            "Auto reaction is now ON for this group."
            if new_state
            else "Auto reaction is now OFF for this group."
        )
    await m.reply_text(m.lang.get(key, fallback))


@app.on_message(filters.group & ~filters.service & ~app.bl_users, group=32)
async def auto_react_watcher(_, m: types.Message):
    if not m.from_user or m.from_user.is_bot:
        return

    content = (m.text or m.caption or "").lstrip()
    if content.startswith("/"):
        return

    chat_id = m.chat.id
    if not await db.get_auto_react(chat_id):
        return

    if _cooldowns.get(chat_id, 0) > time.monotonic():
        return

    try:
        choices = [item for item in _REACTIONS if item != _last_reactions.get(chat_id)]
        reaction = random.choice(choices)
        await app.send_reaction(chat_id, m.id, reaction, big=False)
        _last_reactions[chat_id] = reaction
    except errors.FloodWait as ex:
        _cooldowns[chat_id] = time.monotonic() + max(int(ex.value), 1)
    except Exception as ex:
        if any(item in str(ex).upper() for item in _IGNORED_ERRORS):
            return
        _log.warning("Auto reaction failed in chat %s: %s", chat_id, ex)
