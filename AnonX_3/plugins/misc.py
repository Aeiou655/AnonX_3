# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import os
import time
import asyncio
from pathlib import Path

from pyrogram import enums, errors, filters, types
from pymongo.errors import ServerSelectionTimeoutError

from AnonX_3 import (
    anon, app, bot_api, config, db, lang, logger, queue,
    runtime_storage_percent, userbot,
)
from AnonX_3.core.supervisor import supervisor
from AnonX_3.helpers import buttons, utils


STORAGE_CLEANUP_THRESHOLD_PERCENT = 75.0
STORAGE_CLEANUP_CHECK_SEC = 300


def _clear_active_message_id(chat_id: int, message_id: int) -> None:
    media = queue.get_current(chat_id)
    if media and media.message_id == message_id:
        media.message_id = 0


@app.on_message(filters.video_chat_ended, group=20)
async def _watcher_vc(_, m: types.Message):
    """Clean up only after Telegram reports that the group call has ended.

    A ``video_chat_started`` update can arrive while the first playback request
    is still joining.  Treating that update as a stop signal used to race the
    initial start and could leave an orphaned queue head behind.
    """
    chat_id = m.chat.id
    if chat_id not in db.active_calls and queue.get_current(chat_id) is None:
        return
    try:
        await anon.stop(chat_id)
    except ServerSelectionTimeoutError as ex:
        logger.warning(
            "Voice-chat watcher deferred cleanup because MongoDB is unavailable: %s",
            str(ex).split("Topology Description:", 1)[0].strip(),
        )


async def auto_leave():
    while True:
        await asyncio.sleep(3600)
        for ub in userbot.clients:
            try:
                chats = [dialog.chat.id async for dialog in ub.get_dialogs()
                            if dialog.chat.type in [
                                enums.ChatType.GROUP, enums.ChatType.SUPERGROUP,
                            ]][-20:]
                for chat in chats:
                    if chat == app.logger:
                        continue
                    if chat in db.active_calls:
                        continue
                    await ub.leave_chat(chat)
                    await asyncio.sleep(7)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue


async def track_time():
    while True:
        await asyncio.sleep(1)
        for chat_id in list(db.active_calls):
            if not await db.playing(chat_id):
                continue
            media = queue.get_current(chat_id)
            if not media:
                continue
            media.time += 1


async def update_timer(length=10):
    while True:
        await asyncio.sleep(7)
        for chat_id in list(db.active_calls):
            if not await db.playing(chat_id):
                continue
            try:
                media = queue.get_current(chat_id)
                duration, message_id = media.duration_sec, media.message_id
                if not duration or not message_id or not media.time:
                    continue
                played = media.time
                remaining = duration - played
                pos = min(int((played / duration) * length), length - 1)
                timer = "—" * pos + "◉" + "—" * (length - pos - 1)

                if remaining <= 30:
                    next = queue.get_next(chat_id, check=True)
                    if next and not next.file_path:
                        client = await db.get_assistant(chat_id)
                        profile = anon.stream_profile.select(chat_id, client)
                        await anon.prefetch_manager.start_next(
                            chat_id,
                            quality_tier=profile.download_tier,
                        )

                if remaining < 10:
                    remove = True
                else:
                    if config.THUMB_GEN:
                        timer = f"{time.strftime('%M:%S', time.gmtime(played))} | {timer} | -{time.strftime('%M:%S', time.gmtime(remaining))}"
                    else:
                        timer = None
                    remove = False

                if not timer and not remove:
                    continue

                updated = await utils.edit_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=buttons.controls(
                        chat_id=chat_id, timer=timer, remove=remove
                    ),
                    ignore_stale=True,
                )
                if updated is None:
                    _clear_active_message_id(chat_id, message_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass


async def vc_watcher(sleep=15):
    while True:
        await asyncio.sleep(sleep)
        for chat_id in list(db.active_calls):
            client = await db.get_assistant(chat_id)
            media = queue.get_current(chat_id)
            participants = await client.get_participants(chat_id)
            if len(participants) < 2 and media.time > 30:
                _lang = await lang.get_lang(chat_id)
                try:
                    sent = await utils.edit_reply_markup(
                        chat_id=chat_id,
                        message_id=media.message_id,
                        reply_markup=buttons.controls(
                            chat_id=chat_id, status=_lang["stopped"], remove=True
                        ),
                        ignore_stale=True,
                    )
                    if sent is None:
                        _clear_active_message_id(chat_id, media.message_id)
                        continue
                    await anon.stop(chat_id)
                    await sent.reply_text(_lang["auto_left"])
                except (errors.MessageIdInvalid, errors.Forbidden, errors.ChatWriteForbidden, bot_api.ChatForbidden):
                    pass


def _active_runtime_files() -> set[str]:
    protected = set()
    for chat_id in list(db.active_calls):
        media = queue.get_current(chat_id)
        if not media:
            continue

        if media.file_path:
            try:
                path = Path(media.file_path)
                if path.is_file():
                    protected.add(str(path.resolve()))
            except OSError:
                pass

        media_id = getattr(media, "id", None)
        if media_id:
            for cache_name in (f"{media_id}.png", f"temp_{media_id}.jpg"):
                try:
                    path = Path("cache") / cache_name
                    if path.is_file():
                        protected.add(str(path.resolve()))
                except OSError:
                    pass
    return protected


def _cleanup_runtime_dirs() -> None:
    protected = _active_runtime_files()
    removed = 0

    for dirname in ("downloads", "cache"):
        root = Path(dirname)
        if not root.exists():
            continue

        entries = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for entry in entries:
            if entry.is_symlink():
                try:
                    entry.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                except Exception as ex:
                    logger.warning("Failed to remove runtime link %s: %s", entry, ex)
                continue

            if entry.is_dir():
                try:
                    entry.rmdir()
                except (FileNotFoundError, OSError):
                    pass
                continue

            if not entry.is_file():
                continue

            try:
                resolved = str(entry.resolve())
            except OSError:
                continue

            if resolved in protected:
                continue

            try:
                os.remove(entry)
                removed += 1
            except FileNotFoundError:
                continue
            except Exception as ex:
                logger.warning("Failed to remove stale runtime file %s: %s", entry, ex)

    if removed:
        logger.info("Runtime cleanup removed %s inactive item(s).", removed)


async def daily_runtime_cleanup():
    while True:
        await asyncio.sleep(86400)
        try:
            _cleanup_runtime_dirs()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning("Daily runtime cleanup failed: %s", ex)


async def storage_pressure_cleanup():
    while True:
        try:
            used_percent = runtime_storage_percent()
            if used_percent >= STORAGE_CLEANUP_THRESHOLD_PERCENT:
                logger.warning(
                    "Storage usage %.1f%% reached cleanup threshold %.1f%%.",
                    used_percent,
                    STORAGE_CLEANUP_THRESHOLD_PERCENT,
                )
                _cleanup_runtime_dirs()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning("Storage pressure cleanup failed: %s", ex)
        await asyncio.sleep(STORAGE_CLEANUP_CHECK_SEC)


async def audience_sync():
    # Plugins are imported before app.boot() so command handlers are available
    # from the first update. Defer network work until both Telegram clients are
    # ready; otherwise this task races startup and logs CLIENT_NOT_STARTED.
    while not getattr(app, "is_connected", False) or not userbot.clients:
        await asyncio.sleep(0.25)

    # Telegram bot accounts cannot call get_dialogs. Skip that known-invalid
    # request and continue with assistant-userbot dialogs.
    dialogs_unsupported = bool(
        getattr(getattr(app, "me", None), "is_bot", False)
    )
    while True:
        try:
            await db.get_users()
            await db.get_chats()
            synced_ids = set()

            async def _process_bot_dialog(dialog):
                chat = getattr(dialog, "chat", None)
                if not chat or not isinstance(getattr(chat, "id", None), int):
                    return
                chat_id = chat.id
                if chat.type == enums.ChatType.PRIVATE:
                    if chat_id not in synced_ids:
                        synced_ids.add(chat_id)
                        await db.add_user(chat_id)
                        await db.touch_audience(
                            peer_id=chat_id,
                            peer_type="user",
                            source="periodic_sync",
                            blocked=False,
                            is_active=True,
                        )
                    return
                if chat.type not in {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}:
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
                    source="periodic_sync",
                    is_admin=is_admin,
                    is_active=True,
                )

            if not dialogs_unsupported:
                try:
                    async for dialog in app.get_dialogs():
                        await _process_bot_dialog(dialog)
                except Exception as ex:
                    if "BOT_METHOD_INVALID" in str(ex):
                        dialogs_unsupported = True
                        logger.info(
                            "Audience sync dialogs scan disabled: bot accounts cannot call get_dialogs."
                        )
                    else:
                        raise

            # Fallback: scan assistant userbot dialogs.
            for ub in userbot.clients:
                try:
                    async for dialog in ub.get_dialogs():
                        chat = getattr(dialog, "chat", None)
                        if not chat or not isinstance(getattr(chat, "id", None), int):
                            continue
                        chat_id = chat.id
                        if chat.type not in {enums.ChatType.GROUP, enums.ChatType.SUPERGROUP}:
                            continue
                        if chat_id in synced_ids:
                            continue
                        # Verify main bot is actually a member before saving.
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
                            source="periodic_sync",
                            is_admin=is_admin,
                            is_active=True,
                        )
                except Exception as ex:
                    logger.warning("Audience sync userbot dialog scan failed: %s", ex)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning("Audience sync failed: %s", ex)
        await asyncio.sleep(600)


async def activevc_sampler():
    sleep_for = max(60, int(config.ACTIVEVC_SAMPLE_INTERVAL_SEC))
    while True:
        try:
            await db.add_activevc_sample()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning("ActiveVC sampler failed: %s", ex)
        await asyncio.sleep(sleep_for)


if config.AUTO_END:
    supervisor.spawn("vc_watcher", vc_watcher)
if config.AUTO_LEAVE:
    supervisor.spawn("auto_leave", auto_leave)
supervisor.spawn("track_time", track_time)
supervisor.spawn("update_timer", update_timer)
supervisor.spawn("daily_runtime_cleanup", daily_runtime_cleanup)
supervisor.spawn("storage_pressure_cleanup", storage_pressure_cleanup)
supervisor.spawn("audience_sync", audience_sync)
supervisor.spawn("activevc_sampler", activevc_sampler)

