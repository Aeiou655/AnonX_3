# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of SUIIII MUSIC 🇲🇲


import asyncio
import html
import re

from pyrogram import enums, filters, types
from pyrogram.errors import MessageDeleteForbidden

from AnonX_3 import app, db, lang, logger
from AnonX_3.helpers import buttons, utils
from AnonX_3.helpers._admins import is_admin

FILTER_WARNING_DEFAULT = (
    "🚫 Filter <code>{0}</code> ပါဝင်သဖြင့် {1} ရဲ့ message ကို ဖျက်လိုက်ပါပြီ။\n"
    "⚠️ Strike: <b>{2}/3</b>\n"
    "{3}"
)
FILTER_STRIKE_LIMIT = 3

_FILTER_DATA = {}  # {chat_id: {keyword: True}}
_FILTER_LOCK = asyncio.Lock()
_FILTER_STRIKES = {}  # {(chat_id, user_id): count}
_FILTER_STRIKE_LOCK = asyncio.Lock()
_FILTER_DELETE_TASKS = {}  # {(chat_id, message_id): asyncio.Task}


def _clean_keyword(k: str) -> str:
    return re.sub(r"\s+", " ", (k or "").strip().lower())


async def _load_filters(chat_id: int) -> dict[str, bool]:
    async with _FILTER_LOCK:
        if chat_id in _FILTER_DATA:
            return _FILTER_DATA[chat_id]
    doc = await db.cache.find_one({"_id": f"filter_{chat_id}"}) or {}
    raw_data = doc.get("values", {}) if isinstance(doc.get("values"), dict) else {}
    # Older versions stored {keyword: reply_text}. Keep every existing keyword,
    # but normalize values because replies now use one /settext template.
    data = {
        cleaned: True
        for keyword in raw_data
        if (cleaned := _clean_keyword(str(keyword)))
    }
    async with _FILTER_LOCK:
        _FILTER_DATA[chat_id] = data
    return data


async def _save_filters(chat_id: int, data: dict[str, bool]) -> None:
    async with _FILTER_LOCK:
        _FILTER_DATA[chat_id] = data
    await db.cache.update_one(
        {"_id": f"filter_{chat_id}"},
        {"$set": {"values": data}},
        upsert=True,
    )


async def _check_permission(chat_id: int, user_id: int) -> bool:
    """Admin, owner, or sudo — matches project convention."""
    if user_id in app._sudo_ids:
        return True
    try:
        return await is_admin(chat_id, user_id)
    except Exception:
        return False


async def _increment_strike(chat_id: int, user_id: int) -> int:
    key = (chat_id, user_id)
    async with _FILTER_STRIKE_LOCK:
        if key not in _FILTER_STRIKES:
            try:
                doc = (
                    await db.cache.find_one({"_id": f"filter_strikes_{chat_id}"})
                    or {}
                )
            except Exception as ex:
                logger.warning(
                    "Filter strike load failed chat_id=%s user_id=%s: %s",
                    chat_id,
                    user_id,
                    ex,
                )
                doc = {}
            values = doc.get("values", {}) if isinstance(doc.get("values"), dict) else {}
            try:
                _FILTER_STRIKES[key] = max(0, int(values.get(str(user_id), 0)))
            except (TypeError, ValueError):
                _FILTER_STRIKES[key] = 0

        count = min(_FILTER_STRIKES[key] + 1, FILTER_STRIKE_LIMIT)
        _FILTER_STRIKES[key] = count
        try:
            await db.cache.update_one(
                {"_id": f"filter_strikes_{chat_id}"},
                {"$set": {f"values.{user_id}": count}},
                upsert=True,
            )
        except Exception as ex:
            logger.warning(
                "Filter strike persistence failed chat_id=%s user_id=%s: %s",
                chat_id,
                user_id,
                ex,
            )
        return count


async def _reset_strikes(chat_id: int, user_id: int) -> None:
    key = (chat_id, user_id)
    async with _FILTER_STRIKE_LOCK:
        _FILTER_STRIKES[key] = 0
        try:
            await db.cache.update_one(
                {"_id": f"filter_strikes_{chat_id}"},
                {"$set": {f"values.{user_id}": 0}},
                upsert=True,
            )
        except Exception as ex:
            logger.warning(
                "Filter strike reset persistence failed chat_id=%s user_id=%s: %s",
                chat_id,
                user_id,
                ex,
            )


def _mute_permissions() -> types.ChatPermissions:
    return types.ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_send_polls=False,
        can_invite_users=False,
    )


async def _unmute_permissions(chat_id: int) -> types.ChatPermissions:
    try:
        chat = await app.get_chat(chat_id)
        permissions = getattr(chat, "permissions", None)
        if isinstance(permissions, types.ChatPermissions):
            return permissions
    except Exception:
        pass
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_send_polls=True,
        can_invite_users=True,
    )


async def _set_muted(chat_id: int, user_id: int, muted: bool) -> None:
    if muted and await _check_permission(chat_id, user_id):
        raise PermissionError("Administrators cannot be muted.")
    permissions = (
        _mute_permissions() if muted else await _unmute_permissions(chat_id)
    )
    await app.restrict_chat_member(chat_id, user_id, permissions=permissions)
    if muted:
        await _reset_strikes(chat_id, user_id)


async def _auto_delete(msg, delay: float = 5.0):
    """Delete a message after a delay."""
    key = (msg.chat.id, msg.id)
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except asyncio.CancelledError:
        return
    except Exception:
        pass
    finally:
        current = asyncio.current_task()
        if _FILTER_DELETE_TASKS.get(key) is current:
            _FILTER_DELETE_TASKS.pop(key, None)


def _schedule_delete(msg, delay: float = 5.0) -> None:
    key = (msg.chat.id, msg.id)
    previous = _FILTER_DELETE_TASKS.pop(key, None)
    if previous:
        previous.cancel()
    _FILTER_DELETE_TASKS[key] = asyncio.create_task(_auto_delete(msg, delay))


def _cancel_delete(msg) -> None:
    task = _FILTER_DELETE_TASKS.pop((msg.chat.id, msg.id), None)
    if task:
        task.cancel()


def _format_filter_list(data: dict[str, bool], _lang: dict) -> str:
    if not data:
        return _lang.get("filter_empty", "<b>No filters set.</b>")
    lines = ["<b>📋 Filter Keywords:</b>"]
    for keyword in sorted(data):
        lines.append(f"• <code>{html.escape(keyword)}</code>")
    return "\n".join(lines)


@app.on_message(
    filters.command(["filter", "filtter"]) & filters.group & ~app.bl_users
)
@lang.language()
async def filter_cmd(_, m: types.Message):
    """Manage delete-only keyword filters (admin/sudo only)."""
    chat_id = m.chat.id
    user_id = m.from_user.id if m.from_user else 0

    if not await _check_permission(chat_id, user_id):
        await m.reply_text(m.lang.get("admin_only", "Admin only."))
        return

    args = m.command[1:] if len(m.command) > 1 else []
    data = await _load_filters(chat_id)

    if not args:
        text = (
            "<b>🔍 Filter:</b>\n\n"
            "<code>/filter keyword</code> — keyword ထည့်ရန်\n"
            "<code>/filter remove keyword</code> — keyword ဖျက်ရန်\n"
            "<code>/filter list</code> — keyword စာရင်း\n"
            "<code>/filter clear</code> — အားလုံးဖျက်ရန်\n\n"
            "Warning စာသားပြင်ရန် message ကို reply လုပ်ပြီး "
            "<code>/settext filter</code> သုံးပါ။"
        )
        if data:
            text += f"\n\n{_format_filter_list(data, m.lang)}"
        await m.reply_text(text)
        return

    sub = args[0].lower()

    if sub == "list":
        await m.reply_text(_format_filter_list(data, m.lang))

    elif sub == "clear":
        await _save_filters(chat_id, {})
        await m.reply_text(m.lang.get("filter_cleared", "<b>✅ All filters cleared.</b>"))

    elif sub == "remove":
        if len(args) < 2:
            await m.reply_text(m.lang.get("filter_usage_remove", "Usage: <code>/filter remove keyword</code>"))
            return
        kw = _clean_keyword(" ".join(args[1:]))
        if kw in data:
            del data[kw]
            await _save_filters(chat_id, data)
            await m.reply_text(
                m.lang.get(
                    "filter_removed",
                    "<b>✅ Filter removed:</b> <code>{0}</code>",
                ).format(html.escape(kw))
            )
        else:
            await m.reply_text(
                m.lang.get(
                    "filter_not_found",
                    "<b>❌ Filter not found:</b> <code>{0}</code>",
                ).format(html.escape(kw))
            )

    else:
        # Direct syntax: /filter keyword. Keep /filter add keyword as a
        # compatibility alias, but no per-keyword reply text is stored.
        keyword_args = args[1:] if sub == "add" else args
        kw = _clean_keyword(" ".join(keyword_args))
        if not kw:
            return await m.reply_text("သုံးနည်း: <code>/filter keyword</code>")
        if kw in data:
            return await m.reply_text(
                f"<b>ℹ️ Filter ရှိပြီးသား:</b> <code>{html.escape(kw)}</code>"
            )
        data[kw] = True
        await _save_filters(chat_id, data)
        await m.reply_text(
            f"<b>✅ Filter ထည့်ပြီး:</b> <code>{html.escape(kw)}</code>"
        )


@app.on_message(
    filters.text
    & filters.group
    & ~app.bl_users
    & ~filters.command(["filter", "filtter"]),
    group=25,
)
async def filter_matcher(_, m: types.Message):
    """Match incoming messages against filters."""
    if not m.text or not m.from_user or m.from_user.is_bot:
        return
    # Don't filter admin/sudo messages
    if m.from_user.id in app._sudo_ids or m.from_user.id in app.owners:
        return
    try:
        if await _check_permission(m.chat.id, m.from_user.id):
            return
    except Exception:
        pass

    chat_id = m.chat.id
    data = await _load_filters(chat_id)
    if not data:
        return

    text = _clean_keyword(m.text)
    for kw in data:
        if kw in text:
            strike = await _increment_strike(chat_id, m.from_user.id)
            try:
                await m.delete()
            except MessageDeleteForbidden:
                pass
            except Exception as ex:
                logger.debug("Filter delete failed chat_id=%s: %s", chat_id, ex)

            try:
                muted = False
                moderation_status = "Message ကိုသာ ဖျက်ထားပါတယ်။"
                if strike >= FILTER_STRIKE_LIMIT:
                    try:
                        await _set_muted(chat_id, m.from_user.id, True)
                        muted = True
                        moderation_status = "🔇 ၃ ကြိမ်ပြည့်သဖြင့် auto mute လုပ်ထားပါတယ်။"
                    except Exception as ex:
                        moderation_status = "⚠️ Auto mute မအောင်မြင်ပါ။ Admin က Mute နှိပ်နိုင်ပါတယ်။"
                        logger.warning(
                            "Filter auto-mute failed chat_id=%s user_id=%s: %s",
                            chat_id,
                            m.from_user.id,
                            ex,
                        )

                warning = await db.get_custom_text_for_chat(
                    chat_id,
                    "filter_warning",
                    FILTER_WARNING_DEFAULT,
                )
                sent = await utils.send_formatted(
                    chat_id,
                    warning,
                    html.escape(kw),
                    m.from_user.mention,
                    strike,
                    moderation_status,
                    template_key="filter_warning",
                    reply_markup=buttons.filter_moderation(
                        m.from_user.id,
                        chat_id,
                        muted=muted,
                    ),
                )
                if sent and not muted:
                    _schedule_delete(sent, delay=5.0)
            except Exception as ex:
                logger.warning("Filter reply failed chat_id=%s: %s", chat_id, ex)
            break


@app.on_callback_query(
    filters.regex(r"^filtermod (mute|unmute) \d+ -?\d+$") & ~app.bl_users
)
async def filter_moderation_callback(_, query: types.CallbackQuery):
    parts = query.data.split()
    action = parts[1]
    target_id = int(parts[2])
    chat_id = int(parts[3])
    actor_id = query.from_user.id if query.from_user else 0
    message_chat_id = getattr(getattr(query.message, "chat", None), "id", 0)

    if message_chat_id != chat_id:
        return await query.answer("ဒီ button က ဒီ group အတွက်မဟုတ်ပါ။", show_alert=False)
    if not actor_id or not await _check_permission(chat_id, actor_id):
        return await query.answer("Admin only.", show_alert=False)

    muted = action == "mute"
    try:
        await _set_muted(chat_id, target_id, muted)
    except PermissionError:
        return await query.answer("Admin / Sudo user ကို mute လုပ်လို့မရပါ။", show_alert=False)
    except Exception as ex:
        logger.warning(
            "Filter moderation callback failed action=%s chat_id=%s target_id=%s: %s",
            action,
            chat_id,
            target_id,
            ex,
        )
        return await query.answer(f"Failed: {str(ex)[:120]}", show_alert=False)

    if muted:
        _cancel_delete(query.message)
        reply_markup = buttons.filter_moderation(
            target_id,
            chat_id,
            muted=True,
        )
        answer = "🔇 User ကို mute လုပ်ပြီးပါပြီ။"
    else:
        reply_markup = buttons.filter_moderation(
            target_id,
            chat_id,
            muted=False,
        )
        answer = "🔊 User ကို unmute လုပ်ပြီးပါပြီ။"
        _schedule_delete(query.message, delay=5.0)

    try:
        await utils.edit_reply_markup(
            query.message,
            reply_markup=reply_markup,
            ignore_stale=True,
        )
    except Exception:
        pass
    await query.answer(answer)
