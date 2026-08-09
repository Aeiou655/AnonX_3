# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import os
import time

from pyrogram import enums, errors, filters, types
from pymongo.errors import ServerSelectionTimeoutError

from AnonX_3 import app, bot_api, config, db, lang, logger, userbot


broadcasting = False
_last_broadcast_at = 0.0
_queued_broadcast_message: types.Message | None = None
_queued_broadcast_task: asyncio.Task | None = None
ERRORS_FILE = "errors.txt"
GROUP_CHAT_TYPES = {
    enums.ChatType.GROUP,
    enums.ChatType.SUPERGROUP,
}

# Duplicate broadcast prevention (in-memory TTL cache)
_recent_broadcasts: dict[tuple[int, int], float] = {}
_BROADCAST_DEDUP_TTL_SEC = 300
_BROADCAST_CONCURRENCY = 8


def _short_db_error(ex: Exception, limit: int = 220) -> str:
    text = str(ex or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def _pin_broadcast_message(sent, chat_id: int) -> str | None:
    """Pin a delivered group message without interrupting the broadcast."""

    if isinstance(sent, (list, tuple)):
        sent = sent[0] if sent else None
    pin = getattr(sent, "pin", None)
    if not callable(pin):
        return f"{chat_id} - pin failed: delivered message unavailable\n"
    try:
        await pin(disable_notification=True)
    except Exception as ex:
        return f"{chat_id} - pin failed: {_short_db_error(ex)}\n"
    return None


async def _run_queued_broadcast_after(delay_seconds: int) -> None:
    global _queued_broadcast_message, _queued_broadcast_task
    try:
        await asyncio.sleep(max(0, int(delay_seconds)))
        queued = _queued_broadcast_message
        if not queued:
            return
        while broadcasting:
            await asyncio.sleep(2)
        await _broadcast(None, queued)
    except Exception as ex:
        logger.warning("Queued broadcast failed: %s", ex)
    finally:
        _queued_broadcast_task = None


async def _sync_dialog_groups_to_db() -> None:
    await db.get_chats()
    synced_ids = set()

    async def _process_group_dialog(dialog):
        chat = getattr(dialog, "chat", None)
        chat_id = getattr(chat, "id", None)
        chat_type = getattr(chat, "type", None)
        if not isinstance(chat_id, int) or chat_type not in GROUP_CHAT_TYPES:
            return
        if chat_id in synced_ids:
            return
        synced_ids.add(chat_id)
        await db.add_chat(chat_id)
        is_admin = None
        try:
            member = await app.get_chat_member(chat_id, app.id)
            status = str(getattr(member, "status", "")).split(".")[-1].lower()
            is_admin = status in {"administrator", "owner"}
        except Exception:
            pass
        await db.touch_audience(
            peer_id=chat_id,
            peer_type="group",
            source="dialog_sync",
            is_admin=is_admin,
            is_active=True,
        )

    try:
        async for dialog in app.get_dialogs():
            await _process_group_dialog(dialog)
    except Exception as ex:
        if "BOT_METHOD_INVALID" not in str(ex):
            logger.warning("Bot dialog sync failed: %s", ex)

    # Fallback: scan assistant userbot dialogs since bot accounts cannot call get_dialogs.
    for ub in userbot.clients:
        try:
            async for dialog in ub.get_dialogs():
                chat = getattr(dialog, "chat", None)
                chat_id = getattr(chat, "id", None)
                chat_type = getattr(chat, "type", None)
                if not isinstance(chat_id, int) or chat_type not in GROUP_CHAT_TYPES:
                    continue
                if chat_id in synced_ids:
                    continue
                # Verify the main bot is actually a member before saving.
                try:
                    member = await app.get_chat_member(chat_id, app.id)
                    if member is None:
                        continue
                except Exception:
                    continue
                finally:
                    await asyncio.sleep(0.05)
                synced_ids.add(chat_id)
                await db.add_chat(chat_id)
                is_admin = None
                try:
                    status = str(getattr(member, "status", "")).split(".")[-1].lower()
                    is_admin = status in {"administrator", "owner"}
                except Exception:
                    pass
                await db.touch_audience(
                    peer_id=chat_id,
                    peer_type="group",
                    source="ub_dialog_sync",
                    is_admin=is_admin,
                    is_active=True,
                )
        except Exception as ex:
            logger.warning(
                "Userbot dialog sync failed for %s: %s",
                getattr(ub, "name", "unknown"),
                ex,
            )


async def _sync_dialog_users_to_db() -> None:
    await db.get_users()
    synced_ids = set()

    try:
        async for dialog in app.get_dialogs():
            chat = getattr(dialog, "chat", None)
            user_id = getattr(chat, "id", None)
            chat_type = getattr(chat, "type", None)
            if isinstance(user_id, int) and chat_type == enums.ChatType.PRIVATE:
                if user_id not in synced_ids:
                    synced_ids.add(user_id)
                    await db.add_user(user_id)
                    await db.touch_audience(
                        peer_id=user_id,
                        peer_type="user",
                        source="dialog_sync",
                        blocked=False,
                        is_active=True,
                    )
    except Exception as ex:
        if "BOT_METHOD_INVALID" not in str(ex):
            logger.warning("Bot user dialog sync failed: %s", ex)


@app.on_message(filters.command(["broadcast"]) & app.sudoers, group=-1)
@lang.language()
async def _broadcast(_, message: types.Message):
    global broadcasting, _last_broadcast_at, _queued_broadcast_message, _queued_broadcast_task

    if not message.reply_to_message:
        return await message.reply_text(message.lang["gcast_usage"])

    if broadcasting:
        return await message.reply_text(message.lang["gcast_active"])

    from_user_id = getattr(message.from_user, "id", 0) if message else 0
    is_owner = int(from_user_id) == int(config.OWNER_ID)
    cooldown_seconds = 0 if is_owner else max(0, int(config.BROADCAST_COOLDOWN_MINUTES)) * 60
    now = time.time()
    if cooldown_seconds > 0 and _last_broadcast_at > 0:
        ready_at = _last_broadcast_at + cooldown_seconds
        if now < ready_at:
            remaining = int(ready_at - now)
            rem_min, rem_sec = divmod(remaining, 60)
            _queued_broadcast_message = message
            if _queued_broadcast_task and not _queued_broadcast_task.done():
                _queued_broadcast_task.cancel()
            _queued_broadcast_task = asyncio.create_task(
                _run_queued_broadcast_after(remaining + 1)
            )
            return await message.reply_text(
                message.lang.get(
                    "gcast_cooldown",
                    "Broadcast cooldown ရှိနေပါတယ်။ {0}m {1}s ပြီးမှ နောက်တစ်ခါ broadcast လုပ်လို့ရပါမယ်။",
                ).format(rem_min, rem_sec)
                + "\n\n"
                + message.lang.get(
                    "gcast_queued_auto",
                    "ဒီ broadcast ကို queue ထဲသိမ်းထားပါတယ်။ cooldown ပြည့်တာနဲ့ auto broadcast လုပ်ပါမယ်။",
                )
            )

    msg = message.reply_to_message
    command = [str(part).casefold() for part in (message.command or [])]
    options = set(command[1:])
    text = message.text or ""
    groups = []
    users = []
    group_set = set()
    user_set = set()
    failed = ""
    count = 0
    ucount = 0
    pin_successes = 0
    pin_failures = 0
    progress_message: types.Message | None = None

    # Duplicate broadcast prevention
    fingerprint = (msg.chat.id, msg.id)
    now = time.time()
    cutoff = now - _BROADCAST_DEDUP_TTL_SEC
    for key in list(_recent_broadcasts.keys()):
        if _recent_broadcasts[key] < cutoff:
            del _recent_broadcasts[key]
    if fingerprint in _recent_broadcasts:
        return await message.reply_text(
            message.lang.get(
                "gcast_duplicate",
                "ဒီ message ကို မကြာသေးခင်ကမှ broadcast လုပ်ခဲ့ပြီးပါပြီ။ နောက်တစ်ခါပြန်လုပ်ချင်ရင် စောင့်ပေးပါ။",
            )
        )
    _recent_broadcasts[fingerprint] = now

    progress_message = await message.reply_text(
        message.lang.get("gcast_start", "Broadcasting...")
    )

    if "-nochat" not in options:
        await _sync_dialog_groups_to_db()
        groups = list(await db.get_chats())
        group_set = set(groups)
    if "-user" in options:
        await _sync_dialog_users_to_db()
        users = list(await db.get_users())
        user_set = set(users)

    chats = groups + users

    try:
        if os.path.exists(ERRORS_FILE):
            os.remove(ERRORS_FILE)
    except OSError:
        pass

    broadcasting = True
    try:
        # Log to logger immediately
        try:
            await msg.forward(app.logger)
            log_msg = await app.send_message(
                chat_id=app.logger,
                text=message.lang["gcast_log"].format(
                    message.from_user.id,
                    message.from_user.mention,
                    text,
                ),
            )
            await log_msg.pin(disable_notification=False)
        except Exception:
            pass

        use_copy = "-copy" in options
        pin_requested = "-pin" in options
        semaphore = asyncio.Semaphore(_BROADCAST_CONCURRENCY)

        async def _deliver_message(chat: int):
            if use_copy:
                return await msg.copy(chat, reply_markup=msg.reply_markup)
            return await msg.forward(chat)

        async def _send_one(chat: int) -> tuple[str, int, str | None, bool | None]:
            if not broadcasting:
                return ("stopped", chat, None, None)
            async with semaphore:
                try:
                    sent = await _deliver_message(chat)
                except errors.FloodWait as fw:
                    await asyncio.sleep(min(fw.value + 5, 300))
                    try:
                        sent = await _deliver_message(chat)
                    except Exception as retry_ex:
                        return (
                            "fail",
                            chat,
                            f"{chat} - retry after floodwait failed: {retry_ex}\n",
                            None,
                        )
                except (
                    errors.UserIsBlocked,
                    errors.PeerIdInvalid,
                    errors.ChatForbidden,
                    errors.ChannelPrivate,
                    bot_api.ChatForbidden,
                ) as ex:
                    if chat in user_set:
                        await db.touch_audience(
                            peer_id=chat,
                            peer_type="user",
                            source="broadcast_blocked",
                            blocked=True,
                            is_active=False,
                        )
                    else:
                        await db.touch_audience(
                            peer_id=chat,
                            peer_type="group",
                            source="broadcast_forbidden",
                            is_active=False,
                        )
                    return ("fail", chat, f"{chat} - {ex}\n", None)
                except Exception as ex:
                    if chat in user_set and "blocked" in str(ex).lower():
                        await db.touch_audience(
                            peer_id=chat,
                            peer_type="user",
                            source="broadcast_blocked",
                            blocked=True,
                            is_active=False,
                        )
                    return ("fail", chat, f"{chat} - {ex}\n", None)

                pin_error = None
                pin_ok = None
                if pin_requested and chat in group_set:
                    pin_error = await _pin_broadcast_message(sent, chat)
                    pin_ok = pin_error is None

                if chat in group_set:
                    await db.touch_audience(
                        peer_id=chat,
                        peer_type="group",
                        source="broadcast_ok",
                        is_active=True,
                    )
                    return ("group_ok", chat, pin_error, pin_ok)
                await db.touch_audience(
                    peer_id=chat,
                    peer_type="user",
                    source="broadcast_ok",
                    blocked=False,
                    is_active=True,
                )
                return ("user_ok", chat, None, None)

        # Process in batches so /stop_broadcast can cancel between batches
        results = []
        batch_size = _BROADCAST_CONCURRENCY * 2
        for i in range(0, len(chats), batch_size):
            if not broadcasting:
                break
            batch = chats[i:i + batch_size]
            tasks = [asyncio.create_task(_send_one(chat)) for chat in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            results.extend(batch_results)

        for res in results:
            if isinstance(res, Exception):
                continue
            status, chat_id, err, pin_ok = res
            if status == "group_ok":
                count += 1
                if pin_ok is True:
                    pin_successes += 1
                elif pin_ok is False:
                    pin_failures += 1
                    if err:
                        failed += err
            elif status == "user_ok":
                ucount += 1
            elif status == "fail" and err:
                failed += err

        text = message.lang["gcast_end"].format(count, ucount)
        if not broadcasting:
            text = message.lang["gcast_stopped"].format(count, ucount)
        if pin_requested:
            pin_summary = message.lang.get(
                "gcast_pin_summary",
                "Pins: <b>{0}</b> successful, <b>{1}</b> failed.",
            ).format(pin_successes, pin_failures)
            text += "\n\n" + pin_summary

        if failed:
            try:
                with open(ERRORS_FILE, "w", encoding="utf-8") as file:
                    file.write(failed)
                await message.reply_document(document=ERRORS_FILE)
            except Exception:
                pass
            finally:
                try:
                    if os.path.exists(ERRORS_FILE):
                        os.remove(ERRORS_FILE)
                except OSError:
                    pass

        if progress_message:
            try:
                await progress_message.edit_text(text)
            except Exception:
                await message.reply_text(text)
        else:
            await message.reply_text(text)
        _last_broadcast_at = time.time()
    finally:
        broadcasting = False
        if _queued_broadcast_message is message:
            _queued_broadcast_message = None


@app.on_message(
    filters.command(["stop_gcast", "stop_broadcast"]) & app.sudoers,
    group=-1,
)
@lang.language()
async def _stop_gcast(_, message: types.Message):
    global broadcasting, _queued_broadcast_message, _queued_broadcast_task

    if not broadcasting:
        if _queued_broadcast_message is not None:
            _queued_broadcast_message = None
            if _queued_broadcast_task and not _queued_broadcast_task.done():
                _queued_broadcast_task.cancel()
            _queued_broadcast_task = None
            return await message.reply_text(
                message.lang.get(
                    "gcast_queue_cleared",
                    "Queued auto-broadcast ကို cancel လုပ်ပြီးပါပြီ။",
                )
            )
        return await message.reply_text(message.lang["gcast_inactive"])

    broadcasting = False
    try:
        await (
            await app.send_message(
                chat_id=app.logger,
                text=message.lang["gcast_stop_log"].format(
                    message.from_user.id,
                    message.from_user.mention,
                ),
            )
        ).pin(disable_notification=False)
    except Exception:
        pass
    await message.reply_text(message.lang["gcast_stop"])



@app.on_message(filters.group, group=999)
async def _track_group_chat(_, m: types.Message):
    """Passive group discovery: save any group chat ID where the bot sees activity."""
    if not m.chat:
        return
    chat_id = m.chat.id
    try:
        if not await db.is_chat(chat_id):
            await db.add_chat(chat_id)
    except ServerSelectionTimeoutError as ex:
        logger.warning(
            "Skipping passive group tracking because MongoDB is temporarily unavailable: %s",
            _short_db_error(ex),
        )


@app.on_message(filters.private, group=999)
async def _track_private_user(_, m: types.Message):
    """Passive user discovery: save any private user who messages the bot, and re-activate previously blocked users."""
    if not m.from_user:
        return
    try:
        await db.add_user(m.from_user.id)
    except ServerSelectionTimeoutError as ex:
        logger.warning("Skipping passive user tracking because MongoDB is temporarily unavailable: %s", _short_db_error(ex))
