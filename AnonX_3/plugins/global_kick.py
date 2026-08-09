# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Sudo-only global silent-kick watchlist controlled from LOGGER_ID."""

import asyncio
import html
import logging
from contextlib import suppress
from time import monotonic

from pyrogram import StopPropagation, filters, types

from AnonX_3 import app, db

_log = logging.getLogger(__name__)
_CACHE_TTL_SECONDS = 5.0
_SWEEP_CONCURRENCY = 12
_kick_ids: set[int] = set()
_kick_ids_loaded_at = 0.0
_kick_ids_lock = asyncio.Lock()
_sweep_tasks: set[asyncio.Task] = set()


async def _load_kick_ids(*, force: bool = False) -> set[int]:
    global _kick_ids, _kick_ids_loaded_at
    if not force and monotonic() - _kick_ids_loaded_at < _CACHE_TTL_SECONDS:
        return _kick_ids
    async with _kick_ids_lock:
        if force or monotonic() - _kick_ids_loaded_at >= _CACHE_TTL_SECONDS:
            _kick_ids = await db.get_global_kick_ids()
            _kick_ids_loaded_at = monotonic()
    return _kick_ids


async def _protected_ids() -> set[int]:
    protected = {int(app.id), int(app.owner), *map(int, app._sudo_ids)}
    protected.update(map(int, await db.get_owners()))
    protected.update(map(int, await db.get_sudoers()))
    return protected


async def _resolve_add_target(m: types.Message) -> tuple[int, str, str] | None:
    if m.reply_to_message and m.reply_to_message.from_user:
        user = m.reply_to_message.from_user
        return user.id, user.username or "", user.first_name or ""
    if len(m.command) < 2:
        return None
    raw = m.command[1].strip()
    if raw.lstrip("+").isdigit():
        user_id = int(raw)
        if user_id <= 0:
            return None
        with suppress(Exception):
            user = await app.get_users(user_id)
            return user.id, user.username or "", user.first_name or ""
        return user_id, "", ""
    try:
        user = await app.get_users(raw)
    except Exception:
        return None
    return user.id, user.username or "", user.first_name or ""


async def _resolve_remove_id(m: types.Message) -> int | None:
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id
    if len(m.command) < 2:
        return None
    raw = m.command[1].strip()
    if raw.lstrip("+").isdigit():
        user_id = int(raw)
        return user_id if user_id > 0 else None
    stored = await db.find_global_kick_by_username(raw)
    if stored:
        return int(stored["_id"])
    with suppress(Exception):
        return int((await app.get_users(raw)).id)
    return None


async def _silent_delete(m: types.Message) -> None:
    with suppress(Exception):
        await m.delete()


async def _kick_from_chat(chat_id: int, user_id: int) -> None:
    try:
        await app.ban_chat_member(chat_id, user_id)
        await app.unban_chat_member(chat_id, user_id)
    except Exception as ex:
        _log.debug(
            "Global silent kick skipped chat_id=%s user_id=%s error=%s",
            chat_id,
            user_id,
            type(ex).__name__,
        )


async def _sweep_user(user_id: int) -> None:
    semaphore = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def limited(chat_id: int) -> None:
        async with semaphore:
            await _kick_from_chat(chat_id, user_id)

    await asyncio.gather(
        *(limited(chat_id) for chat_id in await db.get_chats()),
        return_exceptions=True,
    )


def _schedule_sweep(user_id: int) -> None:
    task = asyncio.create_task(_sweep_user(user_id))
    _sweep_tasks.add(task)
    task.add_done_callback(_sweep_tasks.discard)


async def _send_chunked(chat_id: int, heading: str, lines: list[str]) -> None:
    if not lines:
        await app.send_message(chat_id, f"{heading}\n\n<em>Empty</em>")
        return
    chunk = heading
    for line in lines:
        candidate = f"{chunk}\n{line}"
        if len(candidate) > 3900:
            await app.send_message(chat_id, chunk)
            chunk = f"{heading}\n{line}"
        else:
            chunk = candidate
    await app.send_message(chat_id, chunk)


@app.on_message(
    filters.command(["kick"])
    & filters.group
    & filters.chat(app.logger)
    & app.sudoers,
    group=-1,
)
async def global_kick_add(_, m: types.Message):
    await _silent_delete(m)
    try:
        target = await _resolve_add_target(m)
        if not target:
            return
        user_id, username, first_name = target
        if user_id in await _protected_ids():
            return
        await db.add_global_kick(
            user_id,
            username=username,
            first_name=first_name,
            added_by=m.from_user.id,
        )
        await _load_kick_ids(force=True)
        _schedule_sweep(user_id)
    finally:
        raise StopPropagation


@app.on_message(
    filters.command(["unkick"])
    & filters.group
    & filters.chat(app.logger)
    & app.sudoers,
    group=-1,
)
async def global_kick_remove(_, m: types.Message):
    await _silent_delete(m)
    try:
        user_id = await _resolve_remove_id(m)
        if user_id is None:
            return
        await db.del_global_kick(user_id)
        await _load_kick_ids(force=True)
    finally:
        raise StopPropagation


@app.on_message(
    filters.command(["kicklist"])
    & filters.group
    & filters.chat(app.logger)
    & app.sudoers,
    group=-1,
)
async def global_kick_list(_, m: types.Message):
    entries = await db.get_global_kicks()
    await _load_kick_ids(force=True)
    lines = []
    for index, entry in enumerate(entries, 1):
        user_id = int(entry["_id"])
        name = entry.get("first_name") or "Unknown"
        username = entry.get("username") or ""
        with suppress(Exception):
            user = await app.get_users(user_id)
            name = user.first_name or name
            username = user.username or ""
        username_text = f" @{html.escape(username)}" if username else ""
        lines.append(
            f"{index}. {html.escape(name)}{username_text} — <code>{user_id}</code>"
        )
    await _send_chunked(m.chat.id, "<b>Global Silent Kick List</b>", lines)


@app.on_message(
    filters.command(["sudolists"])
    & filters.group
    & filters.chat(app.logger)
    & app.sudoers,
    group=-1,
)
async def global_sudo_list(_, m: types.Message):
    owner_ids = list(dict.fromkeys([app.owner, *await db.get_owners()]))
    sudo_ids = [
        user_id
        for user_id in dict.fromkeys(await db.get_sudoers())
        if user_id not in owner_ids
    ]
    lines = []
    for role, user_ids in (("Owner", owner_ids), ("Sudo", sudo_ids)):
        for user_id in user_ids:
            label = f"<code>{int(user_id)}</code>"
            with suppress(Exception):
                user = await app.get_users(user_id)
                username = f" @{html.escape(user.username)}" if user.username else ""
                label = (
                    f"{html.escape(user.first_name or 'Unknown')}{username} "
                    f"— <code>{user.id}</code>"
                )
            lines.append(f"• <b>{role}</b>: {label}")
    await _send_chunked(m.chat.id, "<b>Current Sudo Lists</b>", lines)


@app.on_message(filters.new_chat_members & filters.group, group=-3)
async def enforce_global_kick_on_join(_, m: types.Message):
    kick_ids = await _load_kick_ids(force=True)
    protected = await _protected_ids()
    for user in m.new_chat_members:
        if user.id in kick_ids and user.id not in protected:
            await _kick_from_chat(m.chat.id, user.id)


@app.on_message(filters.group, group=-4)
async def enforce_global_kick_on_message(_, m: types.Message):
    user = m.from_user
    if not user:
        return
    kick_ids = await _load_kick_ids()
    if user.id in kick_ids and user.id not in await _protected_ids():
        await _kick_from_chat(m.chat.id, user.id)
