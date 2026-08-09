# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import re
import time

from pyrogram import enums, errors, types
from pytgcalls import exceptions as call_exceptions

from AnonX_3 import anon, app, config, db, logger, queue, tasks, tg, tiktok, facebook, yt, userbot
from AnonX_3.core.deferred_status import DeferredStatusMessage
from AnonX_3.core.performance import PlaybackTrace
from AnonX_3.core.request_context import enrich_request
from AnonX_3.helpers import buttons, utils

# Keep strong refs so fire-and-forget delete tasks are not GC'd mid-sleep.
_pending_delete_tasks: set[asyncio.Task] = set()


_assistant_ready: set[tuple[int, int]] = set()
_active_play_request_tasks: dict[int, set[asyncio.Task]] = {}


def _download_progress_template(language: dict | None) -> str:
    """Return a usable progress label even when a locale is incomplete."""
    return (language or {}).get("play_downloading") or "Downloading"


async def _run_noncritical_play_operation(
    phase: str,
    operation,
    *,
    chat_id: int,
    message=None,
):
    """Keep presentation failures outside the playback state transaction.

    Telegram status cards can be deleted while resolver/queue work is in
    flight.  A missing card is not a media, queue, or VC failure and must never
    enter the startup rollback branch.  Cancellation remains authoritative.
    """
    try:
        return await operation(), False
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        stale = bool(message is not None and utils.is_stale_edit_error(ex))
        log = logger.info if stale or utils.is_quiet_edit_error(ex) else logger.warning
        log(
            "Non-critical playback presentation failed phase=%s chat_id=%s "
            "message_id=%s stale=%s error=%s",
            phase,
            chat_id,
            int(getattr(message, "id", 0) or 0) if message is not None else 0,
            int(stale),
            type(ex).__name__,
        )
        return None, stale


async def _edit_queued_status_card(
    chat_id: int,
    sent,
    media,
    *,
    position: int,
    _lang: dict,
    lang_code: str,
):
    """Render one queued card; state admission is intentionally owned elsewhere."""
    play_queued = await db.get_custom_text(
        "play_queued", _lang["play_queued"], lang_code
    )
    play_queued = await utils.normalize_template_entities(
        "play_queued", play_queued, lang_code=lang_code
    )
    play_queued_res = format_play_queued_template(
        play_queued, media, position + 1
    )
    if isinstance(play_queued_res, dict):
        text = play_queued_res["text"]
        _, _, entities = utils.deserialize_entities(
            play_queued_res["entities"]
        )
        text, entities = utils.auto_link_title(
            text, entities, media.title, media.url
        )
        return await utils.edit_text(
            sent,
            text,
            entities=entities,
            reply_markup=buttons.play_queued(chat_id, _lang["close"]),
        )
    return await utils.edit_text(
        sent,
        play_queued_res,
        reply_markup=buttons.play_queued(chat_id, _lang["close"]),
    )


async def _retire_stale_play_status(message, media) -> None:
    """Close late progress writers and forget a status card Telegram removed."""
    await utils.close_download_progress(message, media)
    yt.detach_download_progress(message)


def track_play_request_task(
    message_id: int, task: asyncio.Task | None = None
) -> asyncio.Task | None:
    """Bind request work to the status message used by the Cancel button."""
    if not message_id:
        return task
    bound = task or asyncio.current_task()
    if bound is None:
        return None
    bucket = _active_play_request_tasks.setdefault(int(message_id), set())
    bucket.add(bound)

    def _done(done: asyncio.Task) -> None:
        current = _active_play_request_tasks.get(int(message_id))
        if current is None:
            return
        current.discard(done)
        if not current:
            _active_play_request_tasks.pop(int(message_id), None)

    bound.add_done_callback(_done)
    return bound


def release_play_request_task(
    message_id: int, task: asyncio.Task | None = None
) -> None:
    if not message_id:
        return
    bucket = _active_play_request_tasks.get(int(message_id))
    if not bucket:
        return
    bound = task or asyncio.current_task()
    if bound is not None:
        bucket.discard(bound)
    if not bucket:
        _active_play_request_tasks.pop(int(message_id), None)


async def cancel_play_request(message_id: int) -> bool:
    """Cancel every live branch belonging to one Searching/Downloading card."""
    bucket = _active_play_request_tasks.pop(int(message_id), set())
    caller = asyncio.current_task()
    pending = [
        task
        for task in bucket
        if task is not caller and not task.done()
    ]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return bool(pending)


def _play_request_is_live(message_id: int) -> bool:
    """Whether the status-card request has not reached a terminal state."""
    return not message_id or bool(_active_play_request_tasks.get(int(message_id)))


def _create_play_request_task(
    message_id: int,
    coroutine,
    *,
    name: str,
) -> asyncio.Task:
    """Create request-owned background work so Cancel stops every branch."""
    task = asyncio.create_task(coroutine, name=name)
    if message_id:
        track_play_request_task(message_id, task)
    return task


async def _invalidate_play_request(message) -> None:
    """Stop stale resolver/UI work after a terminal playback outcome."""
    message_id = int(getattr(message, "id", 0) or 0)
    if not message_id:
        return
    try:
        # Publish the terminal ownership barrier before cancelling resolver
        # branches.  A shared yt-dlp owner may keep warming the local cache,
        # but it can never overwrite error_no_call/Cancelled with stale
        # Downloading or Queued progress.
        await utils.close_download_progress(message)
        yt.detach_download_progress(message)
        await cancel_play_request(message_id)
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        logger.debug(
            "Unable to invalidate terminal play request message_id=%s: %s",
            message_id,
            ex,
        )


def _schedule_queued_prefetch(chat_id: int) -> None:
    """Warm the next queued item without blocking or changing queue order."""

    async def _runner() -> None:
        try:
            await anon._prefetch_next(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.debug("Queued prefetch kickoff failed chat_id=%s: %s", chat_id, ex)

    task = asyncio.create_task(
        _runner(),
        name=f"prefetch-queued-next:{chat_id}",
    )
    try:
        tasks.append(task)
    except Exception:
        pass

    def _cleanup(done: asyncio.Task) -> None:
        try:
            if done in tasks:
                tasks.remove(done)
        except Exception:
            pass
        if done.cancelled():
            return
        try:
            done.exception()
        except Exception:
            pass

    task.add_done_callback(_cleanup)


def _prefers_direct_start(media) -> bool:
    if not media or getattr(media, "file_path", None):
        return False
    media_id = str(getattr(media, "id", "") or "")
    source = getattr(media, "source", None)
    if source in {"soundcloud", "soundcloud_remote"}:
        return bool(getattr(media, "url", None))
    if (
        bool(config.YOUTUBE_DIRECT_STREAM)
        and len(media_id) == 11
        and source not in {"tiktok_remote", "telegram_remote", "soundcloud"}
    ):
        return True
    if (
        bool(config.TIKTOK_DIRECT_STREAM)
        and source == "tiktok_remote"
        and bool(getattr(media, "url", None))
    ):
        return True
    if (
        bool(config.FACEBOOK_DIRECT_STREAM)
        and source == "facebook_remote"
        and bool(getattr(media, "url", None))
    ):
        return True
    if (
        bool(getattr(config, "TELEGRAM_DIRECT_STREAM", True))
        and source == "telegram_remote"
        and bool(getattr(media, "telegram_file_id", None))
    ):
        return True
    return False


async def attach_cancel_button(message, cancel_text: str | None = None) -> None:
    """Force Cancel inline keyboard onto a SEARCHING/DOWNLOADING status message."""
    if not message:
        return
    label = (cancel_text or "Cancel").strip() or "Cancel"
    # Bot API first — supports style=danger (red button).
    # Pyrogram MTProto fallback for edge cases.
    bot_api_kb = buttons.cancel_dl(label)
    try:
        await utils.edit_download_progress_markup(
            message,
            reply_markup=bot_api_kb,
            ignore_stale=True,
        )
        return
    except Exception as ex:
        logger.debug("attach_cancel bot_api failed: %s", ex)
    # Pyrogram fallback
    pyro_kb = buttons.cancel_dl_pyrogram(label)
    try:
        await utils.edit_download_progress_markup(
            message,
            reply_markup=pyro_kb,
            ignore_stale=True,
        )
    except Exception as ex:
        logger.debug("attach_cancel pyrogram failed: %s", ex)


def _assistant_label(client) -> str:
    assistant_id = getattr(client, "id", "unknown")
    username = getattr(client, "username", None)
    if username:
        return f"{assistant_id}(@{username})"
    return str(assistant_id)


def _normalize_invite_error(ex: Exception) -> str:
    for attr in ("ID", "MESSAGE", "NAME"):
        value = getattr(ex, attr, None)
        if isinstance(value, str) and value:
            return value

    text = str(ex or "")
    for pattern in (r"\[\d+\s+([A-Z0-9_]+)\]", r"\[([A-Z0-9_]+)\]"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return type(ex).__name__


def playlist_to_queue(chat_id: int, tracks: list) -> str:
    text = "<blockquote expandable>"
    for track in tracks:
        pos = queue.add(chat_id, track)
        text += f"<b>{pos + 1}.</b> {track.title}\n"
    text = text[:1948] + "</blockquote>"
    return text


async def _auto_delete_play_command(log_msg: types.Message | None) -> None:
    """Delete the user's /play command after the track is queued or starts."""
    if not log_msg:
        return
    if not bool(getattr(config, "AUTO_DELETE_PLAY_COMMAND", True)):
        return
    try:
        await log_msg.delete()
        return
    except Exception:
        pass
    try:
        chat_id = getattr(getattr(log_msg, "chat", None), "id", None)
        msg_id = getattr(log_msg, "id", None)
        if chat_id is not None and msg_id is not None:
            await app.delete_messages(chat_id, msg_id)
    except Exception as ex:
        logger.debug(
            "Auto-delete play command failed chat_id=%s msg_id=%s: %s",
            getattr(getattr(log_msg, "chat", None), "id", None),
            getattr(log_msg, "id", None),
            ex,
        )


async def _auto_delete_message(
    msg: types.Message | None = None,
    delay_sec: float = 12.0,
    *,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> None:
    """Best-effort delete of a bot status message (e.g. play_queued card)."""
    cid = chat_id
    mid = message_id
    if msg is not None:
        if cid is None:
            cid = getattr(getattr(msg, "chat", None), "id", None)
        if mid is None:
            mid = getattr(msg, "id", None)
    if cid is None or mid is None:
        logger.warning("Auto-delete skipped: missing chat_id/message_id")
        return
    try:
        await asyncio.sleep(max(1.0, float(delay_sec)))
    except asyncio.CancelledError:
        return
    except Exception:
        return

    deleted = False
    # 1) Message object delete (same client that sent/edited)
    if msg is not None:
        try:
            await msg.delete()
            deleted = True
        except Exception as ex:
            logger.debug("msg.delete failed chat=%s msg=%s: %s", cid, mid, ex)
    # 2) Bot app client
    if not deleted:
        try:
            await app.delete_messages(cid, mid)
            deleted = True
        except Exception as ex:
            logger.debug("app.delete_messages failed chat=%s msg=%s: %s", cid, mid, ex)
    # 3) Bot API path (some edits go through Bot API)
    if not deleted:
        try:
            from AnonX_3 import bot_api

            await bot_api.delete_message(cid, mid)
            deleted = True
        except Exception as ex:
            logger.debug("bot_api.delete_message failed chat=%s msg=%s: %s", cid, mid, ex)

    if deleted:
        logger.info("Auto-deleted play_queued message chat_id=%s msg_id=%s", cid, mid)
    else:
        logger.warning(
            "Auto-delete play_queued FAILED chat_id=%s msg_id=%s "
            "(bot may lack delete rights, or message already gone)",
            cid,
            mid,
        )


def _schedule_auto_delete(
    msg: types.Message | None,
    delay_sec: float,
    *,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> None:
    """Schedule delete with a strong task reference (avoids GC of create_task)."""
    cid = chat_id or getattr(getattr(msg, "chat", None), "id", None)
    mid = message_id or getattr(msg, "id", None)
    if cid is None or mid is None:
        logger.warning("schedule auto-delete skipped: no chat/msg id")
        return
    task = asyncio.create_task(
        _auto_delete_message(
            msg,
            delay_sec=delay_sec,
            chat_id=int(cid),
            message_id=int(mid),
        ),
        name=f"auto-del-queued:{cid}:{mid}",
    )
    _pending_delete_tasks.add(task)
    try:
        tasks.append(task)
    except Exception:
        pass

    def _cleanup(t: asyncio.Task) -> None:
        _pending_delete_tasks.discard(t)
        try:
            if t in tasks:
                tasks.remove(t)
        except Exception:
            pass
        if t.cancelled():
            return
        ex = t.exception() if not t.cancelled() else None
        if ex:
            logger.warning("auto-delete task error: %s", ex)

    task.add_done_callback(_cleanup)


def _is_valid_url(url: str) -> bool:
    """Check if url is a valid http/https URL (not ytsearch:, etc)."""
    if not url or not isinstance(url, str):
        return False
    return url.startswith(("http://", "https://"))


def _media_format_fields(media, position: int | None = None) -> dict:
    raw_url = getattr(media, "url", None) or ""
    url = raw_url if _is_valid_url(raw_url) else ""
    title = getattr(media, "title", None) or "Unknown"
    duration = getattr(media, "duration", None) or "00:00"
    user = getattr(media, "user", None) or "Unknown"
    pos = int(position or 0)
    return {
        "url": url,
        "link": url,
        "title": title,
        "duration": duration,
        "user": user,
        "requester": user,
        "mention": user,
        "position": pos,
        "pos": pos,
    }


def _template_text_blob(template) -> str:
    if isinstance(template, dict):
        return str(template.get("text") or "")
    return str(template or "")


def _looks_like_label_play_card(template) -> bool:
    """Custom cards like TITLE / DURATION / REQUESTED BY (often wrong {0}{1}{2})."""
    t = _template_text_blob(template).upper()
    if not t:
        return False
    has_title = "TITLE" in t or "ခေါင်းစဉ်" in t
    has_dur = "DURATION" in t or "ကြာချိန်" in t
    has_req = "REQUESTED" in t or "REQUEST" in t or "တောင်းဆို" in t
    return has_title and has_dur and has_req


def format_play_media_template(template, media):
    """play_media: positional url,title,duration,user + named fields."""
    fields = _media_format_fields(media)
    # Label cards that use {0}{1}{2} as Title/Duration/User (not href={0}>{1})
    blob = _template_text_blob(template)
    if _looks_like_label_play_card(template) and "href={" not in blob.lower():
        return utils.format_template(
            template,
            fields["title"],
            fields["duration"],
            fields["user"],
            fields["url"],
            **fields,
        )
    return utils.format_template(
        template,
        fields["url"],
        fields["title"],
        fields["duration"],
        fields["user"],
        **fields,
    )


def format_play_queued_template(template, media, position: int):
    """
    play_queued default: {0}=pos, {1}=url, {2}=title, {3}=duration, {4}=user
    Label cards (START STREAMING / TITLE:…) often expect title,duration,user at 0,1,2.
    """
    fields = _media_format_fields(media, position=position)
    blob = _template_text_blob(template)
    # Custom "START STREAMING" style without queue position line
    if _looks_like_label_play_card(template) and "href={" not in blob.lower():
        # Prefer named; positional title,duration,user for {0}{1}{2}
        return utils.format_template(
            template,
            fields["title"],
            fields["duration"],
            fields["user"],
            fields["url"],
            fields["position"],
            **fields,
        )
    return utils.format_template(
        template,
        fields["position"],
        fields["url"],
        fields["title"],
        fields["duration"],
        fields["user"],
        **fields,
    )


def _request_id_for(media) -> str:
    """Return a stable command identity, including for legacy media objects."""
    request_id = str(getattr(media, "request_id", "") or "")
    if request_id:
        return request_id
    request_id = f"legacy-{id(media):x}"
    try:
        setattr(media, "request_id", request_id)
    except Exception:
        pass
    return request_id


async def _rollback_admitted_media(
    chat_id: int,
    media,
    *,
    cleanup_initial_call: bool,
    reason: str,
) -> bool:
    """Undo one provisional admission without touching another request's item."""
    request_id = _request_id_for(media)
    removed = queue.remove_request(chat_id, request_id)
    logger.info(
        "Playback admission rollback chat_id=%s request_id=%s removed=%s reason=%s",
        chat_id,
        request_id,
        removed,
        reason,
    )
    if not cleanup_initial_call:
        return removed
    try:
        # The initial-play lock is held by the caller, so no later command can
        # be cleared by this recovery.  stop() also releases any partial
        # PyTgCalls/startup-gate resources if cancellation raced the join.
        if not await db.get_call(chat_id):
            await anon.stop(chat_id)
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        logger.warning(
            "Initial playback rollback cleanup failed chat_id=%s reason=%s error=%s",
            chat_id,
            reason,
            ex,
        )
    return removed


async def _append_playlist_notice(
    chat_id: int,
    tracks: list,
    _lang: dict,
    lang_code: str,
) -> None:
    """Append playlist tails only after their head has been committed."""
    if not tracks:
        return
    added = playlist_to_queue(chat_id, tracks)
    playlist_tpl = await db.get_custom_text(
        "playlist_queued", _lang["playlist_queued"], lang_code
    )
    playlist_res = utils.format_template(playlist_tpl, len(tracks))
    added_parsed = utils.parse_html_entities(added)
    combined = utils.append_template_text(playlist_res, added_parsed)
    await utils.send_message(
        chat_id=chat_id,
        text=combined["text"],
        entities=combined["entities"],
    )


async def _admit_and_stream_media(
    chat_id: int,
    sent: types.Message,
    media,
    *,
    tracks: list,
    force: bool,
    _lang: dict,
    log_msg: types.Message | None,
    trace: PlaybackTrace | None,
    lang_code: str,
    initial_start: bool,
    voice_call_verified: bool = False,
) -> None:
    """Commit either a confirmed queue item or one serialized initial start."""
    request_id = _request_id_for(media)
    admitted = False
    playback_dispatched = False

    try:
        if initial_start:
            # A queue without an active bot call is never a valid steady state.
            # Clear only this legacy/orphaned state before retrying the first
            # start, then require a real Telegram group call before admission.
            if queue.get_current(chat_id) is not None:
                logger.warning(
                    "Discarding orphaned queue before initial playback chat_id=%s",
                    chat_id,
                )
                queue.clear(chat_id)
            if not voice_call_verified and not await anon.has_active_group_call(chat_id):
                await _invalidate_play_request(sent)
                await utils.edit_text(sent, _lang["error_no_call"], ignore_stale=True)
                return

        if force:
            await anon._delete_now_playing(chat_id)
            anon.prefetch_manager.cancel(chat_id)
            queue.force_add(chat_id, media)
            admitted = True
        else:
            position = queue.add(chat_id, media)
            admitted = True
            if position != 0:
                # The initial-play lock and orphan repair above make this
                # impossible for a healthy first request.  Never turn that
                # inconsistent state into a visible queued card.
                if initial_start:
                    await _rollback_admitted_media(
                        chat_id,
                        media,
                        cleanup_initial_call=False,
                        reason="initial-position-not-zero",
                    )
                    await _invalidate_play_request(sent)
                    await utils.edit_text(
                        sent, _lang["error_no_call"], ignore_stale=True
                    )
                    return

                edited, stale_status = await _run_noncritical_play_operation(
                    "queued-card",
                    lambda: _edit_queued_status_card(
                        chat_id,
                        sent,
                        media,
                        position=position,
                        _lang=_lang,
                        lang_code=lang_code,
                    ),
                    chat_id=chat_id,
                    message=sent,
                )
                queued_msg = sent
                if edited is not None:
                    queued_msg = edited
                # Remember card id — deleted dynamically when this track
                # actually starts (play_next / force play), not on a timer.
                qid = 0 if stale_status else (
                    getattr(queued_msg, "id", None) or getattr(sent, "id", None)
                )
                if qid:
                    media.message_id = int(qid)
                    try:
                        setattr(media, "status_message_id", int(qid))
                    except Exception:
                        pass
                elif stale_status:
                    media.message_id = 0
                    try:
                        setattr(media, "status_message_id", 0)
                    except Exception:
                        pass
                    await _run_noncritical_play_operation(
                        "stale-queued-card-retire",
                        lambda: _retire_stale_play_status(sent, media),
                        chat_id=chat_id,
                        message=sent,
                    )
                # The active stream is already running, so this download must
                # be a silent, deduplicated next-track warmup.
                _schedule_queued_prefetch(chat_id)
                await _run_noncritical_play_operation(
                    "queued-playlist-notice",
                    lambda: _append_playlist_notice(
                        chat_id, tracks, _lang, lang_code
                    ),
                    chat_id=chat_id,
                )
                await _run_noncritical_play_operation(
                    "queued-command-cleanup",
                    lambda: _auto_delete_play_command(log_msg),
                    chat_id=chat_id,
                )
                # Optional timed fallback only if explicitly enabled (default OFF).
                if bool(getattr(config, "AUTO_DELETE_PLAY_QUEUED", False)):
                    delay = float(
                        getattr(config, "AUTO_DELETE_PLAY_QUEUED_SEC", 12) or 12
                    )
                    _schedule_auto_delete(
                        queued_msg,
                        delay,
                        chat_id=chat_id,
                        message_id=int(qid) if qid else None,
                    )
                return

        # A healthy direct candidate does not need a blocking Downloading edit:
        # playback can be dispatched immediately while its local fallback warms.
        if not media.file_path and not _prefers_direct_start(media):
            async def _show_download_status():
                downloading_tpl = await db.get_custom_text(
                    "play_downloading", _download_progress_template(_lang), lang_code
                )
                cancel_label = _lang.get("cancel", "Cancel")
                await utils.edit_download_progress(
                    sent,
                    utils.render_initial_download_progress(downloading_tpl),
                    reply_markup=buttons.cancel_dl(cancel_label),
                    media=media,
                    ignore_stale=False,
                )
                if getattr(sent, "reply_markup", None) is None:
                    _create_play_request_task(
                        int(getattr(sent, "id", 0) or 0),
                        attach_cancel_button(sent, cancel_label),
                        name=f"cancel-button:{chat_id}",
                    )

            _, stale_status = await _run_noncritical_play_operation(
                "download-status",
                _show_download_status,
                chat_id=chat_id,
                message=sent,
            )
            if stale_status:
                await _run_noncritical_play_operation(
                    "stale-download-card-retire",
                    lambda: _retire_stale_play_status(sent, media),
                    chat_id=chat_id,
                    message=sent,
                )

        if trace:
            trace.mark("playback_dispatch")
        playback_dispatched = True
        await anon.play_media(
            chat_id=chat_id,
            message=sent,
            media=media,
            trace=trace,
            initial_start=initial_start,
        )

        # play_media handles a late NoActiveGroupCall internally.  It returns
        # normally after rendering the error, so the command layer must verify
        # that the first start really committed before adding playlist tails.
        if initial_start and not await db.get_call(chat_id):
            await _invalidate_play_request(sent)
            await _rollback_admitted_media(
                chat_id,
                media,
                cleanup_initial_call=True,
                reason="initial-play-not-confirmed",
            )
            return

        await _run_noncritical_play_operation(
            "post-start-command-cleanup",
            lambda: _auto_delete_play_command(log_msg),
            chat_id=chat_id,
        )
        await _run_noncritical_play_operation(
            "post-start-playlist-notice",
            lambda: _append_playlist_notice(chat_id, tracks, _lang, lang_code),
            chat_id=chat_id,
        )
    except asyncio.CancelledError:
        try:
            setattr(media, "cancelled", True)
        except Exception:
            pass
        # Once db marks the first stream active, it is no longer a cancellable
        # provisional request.  A late Cancel must not erase that committed
        # playback or a later command's queue entry.
        unconfirmed_initial = initial_start
        if initial_start:
            try:
                unconfirmed_initial = not await db.get_call(chat_id)
            except Exception:
                unconfirmed_initial = True
        # The status-card scope must always be terminal after cancellation.
        # A committed stream is preserved below, but its stale resolver/UI
        # branches must not revive a deleted Downloading/Queued card.
        await _invalidate_play_request(sent)
        if admitted and (not initial_start or unconfirmed_initial):
            await asyncio.shield(
                _rollback_admitted_media(
                    chat_id,
                    media,
                    cleanup_initial_call=initial_start and playback_dispatched,
                    reason="cancelled",
                )
            )
        logger.info(
            "Playback request cancelled chat_id=%s media_id=%s request_id=%s",
            chat_id,
            getattr(media, "id", None),
            request_id,
        )
        raise
    except call_exceptions.NoVideoSourceFound:
        logger.warning(
            "Playback source contains no video chat_id=%s media_id=%s source=%s",
            chat_id,
            getattr(media, "id", None),
            getattr(media, "source", None),
        )
        failure = _lang.get("error_no_video", _lang["error_no_file"])
        await _invalidate_play_request(sent)
        try:
            await utils.edit_text(sent, failure, reply_markup=buttons.support_button())
        except Exception:
            pass
        await _rollback_admitted_media(
            chat_id,
            media,
            cleanup_initial_call=initial_start and playback_dispatched,
            reason="no-video-source",
        )
    except Exception:
        logger.exception(
            "Unexpected playback startup failure for chat_id=%s media_id=%s source=%s",
            chat_id,
            getattr(media, "id", None),
            getattr(media, "source", None),
        )
        await _invalidate_play_request(sent)
        try:
            await utils.edit_text(
                sent,
                _lang.get("error_no_file", _lang["play_error"]),
                reply_markup=buttons.support_button(),
            )
        except Exception:
            pass

        current = queue.get_current(chat_id)
        current_matches = bool(
            current
            and str(getattr(current, "request_id", "") or "") == request_id
        )
        if current_matches and not initial_start and queue.get_next(chat_id, check=True):
            logger.warning(
                "Skipping failed startup media chat_id=%s media_id=%s and advancing to next queued item",
                chat_id,
                getattr(media, "id", None),
            )
            try:
                await anon.play_next(chat_id)
            except Exception:
                logger.exception(
                    "Failed to advance queue after startup failure chat_id=%s media_id=%s",
                    chat_id,
                    getattr(media, "id", None),
                )
            return

        await _rollback_admitted_media(
            chat_id,
            media,
            cleanup_initial_call=initial_start and playback_dispatched,
            reason="startup-exception",
        )


async def stream_media(
    chat_id: int,
    sent: types.Message,
    media,
    tracks: list = None,
    force: bool = False,
    _lang: dict = None,
    user: str = None,
    log_msg: types.Message = None,
    trace: PlaybackTrace | None = None,
) -> None:
    admission_hint = getattr(media, "_play_admission_hint", None)
    hint_fresh = bool(
        isinstance(admission_hint, dict)
        and time.monotonic() - float(admission_hint.get("created_at", 0.0) or 0.0)
        <= 2.5
    )
    hinted_lang = admission_hint.get("lang_code") if hint_fresh else None
    lang_code = str(hinted_lang or await db.get_lang(chat_id))
    # Play length uses DURATION_LIMIT only (env minutes → config seconds).
    max_dur = int(getattr(config, "DURATION_LIMIT", 0) or 0)
    if max_dur > 0 and int(getattr(media, "duration_sec", 0) or 0) > max_dur:
        return await utils.edit_text(
            sent, _lang["play_duration_limit"].format(max(1, max_dur // 60))
        )

    if await db.is_logger() and log_msg and chat_id != app.logger:
        # Logging must never sit on the playback critical path.
        asyncio.create_task(
            utils.play_log(log_msg, sent.link, media.title, media.duration),
            name=f"play-log:{chat_id}",
        )

    if user:
        media.user = user

    kwargs = {
        "tracks": tracks,
        "force": force,
        "_lang": _lang,
        "log_msg": log_msg,
        "trace": trace,
        "lang_code": lang_code,
        "voice_call_verified": bool(
            hint_fresh and admission_hint.get("voice_call_active") is True
        ),
    }
    initial_lease = getattr(media, "_initial_playback_lease", None)
    lease_valid = bool(
        initial_lease is not None
        and int(getattr(initial_lease, "chat_id", 0) or 0) == int(chat_id)
        and not bool(getattr(initial_lease, "released", True))
    )
    # Stale-session guard: db.get_call() can report True even after the real
    # Telegram VC has ended.  Verify against the actual Telegram group call
    # before reusing a stale-session path — otherwise the bot queues a new
    # request against a VC that no longer exists and the session gets stuck.
    # get_call() is process-local. Read it at handoff time so a second command
    # cannot reuse the earlier hint after another request has committed.
    db_has_call = chat_id in getattr(db, "active_calls", {})
    if db_has_call:
        if hint_fresh and "voice_call_active" in admission_hint:
            vc_alive = bool(admission_hint.get("voice_call_active"))
        else:
            try:
                vc_alive = await anon.has_active_group_call(chat_id)
            except Exception:
                vc_alive = False
        if not vc_alive:
            logger.warning(
                "Stale session detected chat_id=%s — db thinks active but "
                "Telegram reports no VC.  Forcing full cleanup before new /play.",
                chat_id,
            )
            await anon.stop(chat_id)
            db_has_call = False

    if db_has_call:
        if lease_valid:
            initial_lease.release()
            lease_valid = False
        return await _admit_and_stream_media(
            chat_id, sent, media, initial_start=False, **kwargs
        )

    if lease_valid:
        try:
            return await _admit_and_stream_media(
                chat_id,
                sent,
                media,
                initial_start=True,
                **kwargs,
            )
        finally:
            initial_lease.release()
            setattr(media, "_initial_playback_lease", None)

    # Re-check under the shared per-chat lock: a concurrent first request may
    # have committed playback while this request was waiting.
    async with anon.initial_playback_lock(chat_id):
        initial_start = chat_id not in getattr(db, "active_calls", {})
        return await _admit_and_stream_media(
            chat_id, sent, media, initial_start=initial_start, **kwargs
        )


def checkUB(play):
    async def wrapper(_, m: types.Message):
        if not m.from_user:
            return await m.reply_text(m.lang["play_user_invalid"])

        chat_id = m.chat.id
        if m.chat.type != enums.ChatType.SUPERGROUP:
            await m.reply_text(m.lang["play_chat_invalid"])
            try:
                return await app.leave_chat(chat_id)
            except errors.FloodWait as fw:
                logger.warning("FloodWait on leave_chat(%s): %ss, skipping leave", chat_id, fw.value)
                return

        # Allow bare /play when the command message, a reply, a forward, or a
        # story reply carries playable media.
        has_media_source = tg.has_playable_source(m)
        if not has_media_source and not m.reply_to_message and (
            len(m.command) < 2 or (len(m.command) == 2 and m.command[1] == "-f")
        ):
            return await m.reply_text(m.lang["play_usage"])

        if len(queue.get_queue(chat_id)) >= config.QUEUE_LIMIT:
            return await m.reply_text(m.lang["play_queue_full"].format(config.QUEUE_LIMIT))

        force = m.command[0].endswith("force") or (
            len(m.command) > 1 and m.command[1] == "-f"
        )
        video = m.command[0][0] == "v" and config.VIDEO_PLAY
        try:
            active_load = len(getattr(db, "active_calls", []) or [])
        except Exception:
            active_load = -1
        trace = PlaybackTrace(
            str(m.command[0] if m.command else "play"),
            video=bool(video),
            chat_load=active_load,
        )
        url = utils.get_url(m)
        is_tiktok_url = bool(url and tiktok.valid(url))
        is_facebook_url = bool(url and facebook.valid(url))
        is_tg_message_link = bool(url and tg.is_message_link(url))
        is_tg_story_link = bool(url and tg.is_story_link(url))
        is_tg_file_id = bool(url and tg.is_telegram_file_id(url))
        if is_tg_file_id:
            # A raw Telegram file ID cannot be resolved without chat context.
            # Reject early instead of routing it to YouTube/yt-dlp extraction.
            tpl = await db.get_custom_text_for_chat(
                chat_id, "play_not_found", m.lang["play_not_found"]
            )
            return await utils.reply_formatted(
                m, tpl, config.SUPPORT_CHAT, template_key="play_not_found"
            )
        if (
            url
            and not is_tiktok_url
            and not is_facebook_url
            and not is_tg_message_link
            and not is_tg_story_link
            and yt.invalid(url)
        ):
            tpl = await db.get_custom_text_for_chat(
                chat_id, "play_not_found", m.lang["play_not_found"]
            )
            return await utils.reply_formatted(
                m, tpl, config.SUPPORT_CHAT, template_key="play_not_found"
            )
        m3u8 = bool(
            url
            and not is_tiktok_url
            and not is_facebook_url
            and not is_tg_message_link
            and not is_tg_story_link
            and not is_tg_file_id
            and not yt.valid(url)
        )

        request_task = asyncio.current_task()
        request_message_id = 0
        authorization_task = None
        warm_task = None
        warm_external_task = None
        admission_task = None
        initial_lease = None
        initial_preconnect = None
        warm_query = None
        try:
            from AnonX_3.core.metrics import mark_request

            mark_request()
        except Exception:
            pass

        async def _authorize_play_request() -> bool:
            play_mode = await db.get_play_mode(chat_id)
            if not (play_mode or force):
                return True
            adminlist, is_authorized = await asyncio.gather(
                db.get_admins(chat_id),
                db.is_auth(chat_id, m.from_user.id),
            )
            return bool(
                m.from_user.id in adminlist
                or is_authorized
                or m.from_user.id in app._sudo_ids
            )

        authorization_task = _create_play_request_task(
            0,
            _authorize_play_request(),
            name=f"play-authorize:{chat_id}:{m.id}",
        )

        # Provider search and the Telegram status-card send are independent.
        # Start the read-only YouTube lane first so a 600-800ms Telegram RPC
        # cannot sit in front of search, direct resolution, and VC startup.
        if (
            not has_media_source
            and not m.reply_to_message
            and not m3u8
            and not is_tiktok_url
            and not is_facebook_url
        ):
            warm_query = url or " ".join(
                part for part in m.command[1:] if part != "-f"
            ).strip()

        async def _warm_search_and_direct():
            """Resolve cache/search and launch direct resolution immediately."""
            if not warm_query or "playlist" in warm_query:
                return None
            if tg.is_message_link(warm_query) or tg.is_story_link(warm_query):
                return None
            if not _play_request_is_live(request_message_id):
                return None
            if authorization_task is not None and not await asyncio.shield(
                authorization_task
            ):
                return None
            # Use the command ID until the asynchronous status card is bound.
            # Search/cache identity does not require a bot-authored message.
            status_id = request_message_id or int(getattr(m, "id", 0) or 0)
            cached = yt.resolve_cached_source(
                warm_query,
                status_id,
                video=video,
            )
            if cached is not None:
                return cached
            result = await yt.search(warm_query, m.id, video=video)
            if (
                result is not None
                and bool(getattr(config, "YOUTUBE_DIRECT_STREAM", True))
                and len(str(getattr(result, "id", "") or "")) == 11
            ):
                yt.warm_direct_stream_source(
                    str(getattr(result, "id", "") or ""),
                    video=bool(getattr(result, "video", video)),
                    quality_tier=None,
                )
            return result

        if warm_query:
            warm_task = _create_play_request_task(
                0,
                _warm_search_and_direct(),
                name=f"play-warm-search-early:{m.id}",
            )

        async def _prefetch_admission_state():
            """Overlap the one Telegram VC-presence RPC with search and auth."""
            authorized = bool(
                authorization_task is None
                or await asyncio.shield(authorization_task)
            )
            hint = {"created_at": time.monotonic()}
            if not authorized:
                return hint
            language_task = asyncio.create_task(db.get_lang(chat_id))
            voice_task = asyncio.create_task(anon.has_active_group_call(chat_id))
            language_result, voice_result = await asyncio.gather(
                language_task,
                voice_task,
                return_exceptions=True,
            )
            if not isinstance(language_result, BaseException):
                hint["lang_code"] = language_result
            if not isinstance(voice_result, BaseException):
                hint["voice_call_active"] = bool(voice_result)
            return hint

        admission_task = _create_play_request_task(
            0,
            _prefetch_admission_state(),
            name=f"play-admission-warm:{chat_id}:{m.id}",
        )

        # The proxy exposes cheap chat metadata immediately, but every edit
        # waits for and targets the bot-owned status card. Playback can proceed
        # without risking an edit of the user's command message.
        pre_sent = DeferredStatusMessage(m)

        async def _send_early_acknowledgement():
            nonlocal request_message_id
            searching_tpl = await db.get_custom_text_for_chat(
                chat_id, "play_searching", m.lang["play_searching"]
            )
            # Cancel must be on THIS message — play_hndlr reuses pre_sent and
            # will not send a second SEARCHING card.
            cancel_label = m.lang.get("cancel", "Cancel")
            sent = await utils.reply_formatted(
                m,
                searching_tpl,
                template_key="play_searching",
                reply_markup=buttons.cancel_dl(cancel_label),
            )
            if sent is None:
                raise RuntimeError("play_status_ack_missing")
            pre_sent.bind(sent)
            request_message_id = int(getattr(sent, "id", 0) or 0)
            if request_message_id:
                track_play_request_task(request_message_id, request_task)
                if authorization_task is not None:
                    track_play_request_task(request_message_id, authorization_task)
                if warm_task is not None:
                    track_play_request_task(request_message_id, warm_task)
                if admission_task is not None:
                    track_play_request_task(request_message_id, admission_task)
            # The normal send already includes the keyboard.  Repair a custom
            # template that dropped it without delaying search/startup.
            if getattr(sent, "reply_markup", None) is None:
                _create_play_request_task(
                    request_message_id,
                    attach_cancel_button(sent, cancel_label),
                    name=f"cancel-button-ack:{chat_id}",
                )
            trace.mark("ack")
            return sent

        async def _ack_runner():
            try:
                return await _send_early_acknowledgement()
            except BaseException as ex:
                pre_sent.fail(ex)
                raise

        ack_task = asyncio.create_task(
            _ack_runner(),
            name=f"play-status-ack:{chat_id}:{m.id}",
        )

        def _ack_done(done: asyncio.Task) -> None:
            if done.cancelled():
                return
            try:
                done.result()
            except Exception as ex:
                logger.warning(
                    "Early /play acknowledgement failed chat_id=%s: %s",
                    chat_id,
                    ex,
                )
                # Preserve the existing contract: if no status card can be
                # created, cancel any still-provisional playback transaction.
                if request_task is not None and not request_task.done():
                    request_task.cancel()

        ack_task.add_done_callback(_ack_done)

        async def _fail_ready(text: str):
            pending = [
                task
                for task in (
                    authorization_task,
                    warm_task,
                    warm_external_task,
                    admission_task,
                )
                if task is not None
                and task is not asyncio.current_task()
                and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if pre_sent is not None:
                try:
                    await utils.edit_text(pre_sent, text)
                    return None
                except Exception:
                    pass
            return await m.reply_text(text)

        # Authorization and acknowledgement run concurrently, but no provider
        # search or VC-presence request is allowed to cross the auth barrier.
        if authorization_task is not None and not await authorization_task:
            return await _fail_ready(m.lang["play_admin"])

        # Warm YouTube resolution while the assistant membership/readiness
        # check runs. The search layer is single-flight, so the handler later
        # reuses this exact work instead of issuing another provider request.
        # DEEP FIX: For direct YouTube URLs, run search + direct stream in TRUE parallel.
        if (
            has_media_source
            or is_tiktok_url
            or is_facebook_url
            or is_tg_message_link
            or is_tg_story_link
        ):
            async def _warm_external_source():
                if not _play_request_is_live(request_message_id):
                    return None
                status_id = request_message_id or int(getattr(m, "id", 0) or 0)
                resolved = None
                if has_media_source:
                    playable_source = tg.resolve_playable(m)
                    if playable_source:
                        kind, source_obj = playable_source
                        if kind == "story":
                            resolved = await tg.resolve_story(
                                source_obj, status_id, video=video
                            )
                        else:
                            resolved = await tg.resolve(
                                source_obj, status_id, video=video
                            )
                elif is_tg_story_link:
                    story = await tg.fetch_story_link(url, group_chat_id=chat_id)
                    if story and getattr(story, "video", None):
                        resolved = await tg.resolve_story(
                            story, status_id, video=video
                        )
                elif is_tg_message_link:
                    linked = await tg.fetch_linked_message(url)
                    if linked and tg.get_media(linked):
                        resolved = await tg.resolve(
                            linked, status_id, video=video
                        )
                elif is_tiktok_url:
                    resolved = await tiktok.resolve(url, status_id, video=video)
                    if resolved:
                        resolved.source = "tiktok_remote"
                        resolved.local_path = (
                            f"downloads/{resolved.id}."
                            f"{'mp4' if video else 'm4a'}"
                        )
                elif is_facebook_url:
                    resolved = await facebook.resolve(url, status_id, video=video)
                    if resolved:
                        resolved.source = "facebook_remote"
                        resolved.local_path = (
                            f"downloads/{resolved.id}."
                            f"{'mp4' if video else 'm4a'}"
                        )

                if not _play_request_is_live(request_message_id):
                    return None
                if resolved and (force or chat_id not in db.active_calls):
                    if pre_sent is not None:
                        downloading_tpl = await db.get_custom_text_for_chat(
                            chat_id, "play_downloading", _download_progress_template(m.lang)
                        )
                        setattr(resolved, "download_progress_message", pre_sent)
                        setattr(resolved, "download_progress_lang", m.lang)
                        setattr(
                            resolved,
                            "download_progress_template",
                            downloading_tpl,
                        )
                        setattr(
                            resolved,
                            "download_progress_cancel_label",
                            m.lang.get("cancel", "Cancel"),
                        )
                    source_name = getattr(resolved, "source", None)
                    if source_name == "tiktok_remote":
                        await tiktok.start_current_cache(chat_id, resolved)
                    elif source_name == "facebook_remote":
                        await facebook.start_current_cache(chat_id, resolved)
                    elif source_name == "telegram_remote":
                        await tg.start_current_cache(chat_id, resolved)
                return resolved

            warm_external_task = _create_play_request_task(
                request_message_id,
                _warm_external_source(),
                name=f"play-warm-external:{m.id}",
            )

            async def _on_external_found(task: asyncio.Task) -> None:
                if (
                    task.cancelled()
                    or pre_sent is None
                    or not _play_request_is_live(request_message_id)
                ):
                    return
                try:
                    resolved = task.result()
                except Exception:
                    return
                if (
                    not _play_request_is_live(request_message_id)
                    or resolved is None
                    or getattr(resolved, "cancelled", False)
                    or (
                    not force
                    and (
                        chat_id in db.active_calls
                        or queue.get_current(chat_id) is not None
                    )
                    )
                    or getattr(resolved, "download_progress_started", False)
                ):
                    return
                try:
                    downloading_tpl = getattr(
                        resolved, "download_progress_template", None
                    ) or await db.get_custom_text_for_chat(
                        chat_id, "play_downloading", _download_progress_template(m.lang)
                    )
                    if not _play_request_is_live(request_message_id):
                        return
                    await utils.edit_download_progress(
                        pre_sent,
                        utils.render_initial_download_progress(downloading_tpl),
                        reply_markup=buttons.cancel_dl(
                            m.lang.get("cancel", "Cancel")
                        ),
                        media=resolved,
                    )
                except Exception:
                    pass

            def _schedule_external_found(task: asyncio.Task) -> None:
                if task.cancelled() or not _play_request_is_live(request_message_id):
                    return
                _create_play_request_task(
                    request_message_id,
                    _on_external_found(task),
                    name=f"play-warm-external-ui:{m.id}",
                )

            warm_external_task.add_done_callback(_schedule_external_found)

        if (
            not has_media_source
            and not m.reply_to_message
            and not m3u8
            and not is_tiktok_url
            and not is_facebook_url
        ):
            warm_query = url or " ".join(
                part for part in m.command[1:] if part != "-f"
            ).strip()
            if (
                warm_query
                and "playlist" not in warm_query
                and not tg.is_message_link(warm_query)
                and not tg.is_story_link(warm_query)
            ):
                if warm_task is None:
                    warm_task = _create_play_request_task(
                        request_message_id,
                        _warm_search_and_direct(),
                        name=f"play-warm-search:{m.id}",
                    )

                async def _on_search_found(task: asyncio.Task) -> None:
                    """Update UI immediately when search returns — don't wait for assistant."""
                    if task.cancelled() or not _play_request_is_live(request_message_id):
                        return
                    try:
                        result = task.result()
                    except Exception:
                        return
                    if (
                        result is None
                        or pre_sent is None
                        or getattr(result, "cancelled", False)
                        or not _play_request_is_live(request_message_id)
                    ):
                        return

                    # The warm Track can become the physical cache owner
                    # before play_hndlr builds its final Track instance. Keep
                    # the user query and status-card scope on that owner so a
                    # successful local file is catalogued under the exact
                    # request text and a terminal one-shot outcome cannot be
                    # retried by the later handoff.
                    enrich_request(
                        result,
                        chat_id=chat_id,
                        user_id=int(getattr(getattr(m, "from_user", None), "id", 0) or 0),
                        query=warm_query,
                        request_source=str(
                            getattr(m, "_play_request_source", "") or "command"
                        ),
                        priority=100,
                    )
                    setattr(result, "_play_request_scope", request_message_id)

                    if getattr(result, "file_path", None):
                        # Keep the exact verified cache object for play_hndlr;
                        # do not render Downloading or start a cache worker.
                        setattr(m, "_warm_cache_media", result)
                        return

                    # An active stream means this request is queue-bound.  The
                    # main handler will turn SEARCHING into play_queued after
                    # atomic queue admission; never race it with DOWNLOADING.
                    if not force and (
                        chat_id in db.active_calls
                        or queue.get_current(chat_id) is not None
                    ):
                        return
                    if (
                        bool(getattr(config, "YOUTUBE_DIRECT_STREAM", True))
                        and len(str(getattr(result, "id", "") or "")) == 11
                    ):
                        # _warm_search_and_direct starts the direct singleflight
                        # before warm_task completes, so reaching this UI callback
                        # means mweb/POT resolution is already in flight or cached.
                        logger.info(
                            "YouTube direct-first warm search: direct resolver prewarmed "
                            "chat_id=%s media_id=%s video=%s",
                            chat_id,
                            getattr(result, "id", None),
                            int(bool(getattr(result, "video", False))),
                        )
                        return

                    lang_code = await db.get_lang(chat_id)
                    downloading_tpl = await db.get_custom_text(
                        "play_downloading", _download_progress_template(m.lang), lang_code
                    )
                    if not _play_request_is_live(request_message_id):
                        return
                    cancel_label = m.lang.get("cancel", "Cancel")
                    setattr(result, "download_progress_message", pre_sent)
                    setattr(result, "download_progress_lang", m.lang)
                    setattr(result, "download_progress_template", downloading_tpl)
                    setattr(result, "download_progress_cancel_label", cancel_label)

                    try:
                        # Publish the base Downloading state before the worker
                        # can emit progress. Starting the worker first allowed
                        # this edit to race and overwrite a newer progress bar.
                        await utils.edit_download_progress(
                            pre_sent,
                            utils.render_initial_download_progress(downloading_tpl),
                            reply_markup=buttons.cancel_dl(cancel_label),
                            media=result,
                        )
                        if getattr(pre_sent, "reply_markup", None) is None:
                            _create_play_request_task(
                                request_message_id,
                                attach_cancel_button(pre_sent, cancel_label),
                                name=f"cancel-button-found:{chat_id}",
                            )
                    except Exception:
                        pass

                    # Search completion is the event that advances the UI.
                    # The status card is now committed, so every subsequent
                    # worker edit is a monotonic progress update.
                    if (
                        (force or chat_id not in db.active_calls)
                        and not bool(getattr(result, "video", False))
                        and _play_request_is_live(request_message_id)
                    ):
                        _create_play_request_task(
                            request_message_id,
                            anon.prefetch_manager.start_current_cache(
                                chat_id,
                                result,
                                quality_tier=None,
                                force=True,
                                immediate=True,
                                local_only=True,
                            ),
                            name=f"warm-local-after-search:{m.id}",
                        )

                def _schedule_search_found(task: asyncio.Task) -> None:
                    if task.cancelled() or not _play_request_is_live(request_message_id):
                        return
                    _create_play_request_task(
                        request_message_id,
                        _on_search_found(task),
                        name=f"play-warm-search-ui:{m.id}",
                    )

                warm_task.add_done_callback(_schedule_search_found)

        if chat_id not in db.active_calls:
            total_assistants = max(len(userbot.clients), 1)
            invite_link = None
            invite_notice = None
            last_reason = None

            for attempt in range(1, total_assistants + 1):
                client = await db.get_client(chat_id)
                reason = None
                logger.info(
                    "Assistant invite attempt %s/%s for chat_id=%s assistant=%s",
                    attempt,
                    total_assistants,
                    chat_id,
                    _assistant_label(client),
                )
                try:
                    member = None
                    ready_key = (chat_id, int(client.id))
                    ready_cached = ready_key in _assistant_ready
                    if not ready_cached:
                        member = await app.get_chat_member(chat_id, client.id)
                    if member is not None and member.status in [
                        enums.ChatMemberStatus.BANNED,
                        enums.ChatMemberStatus.RESTRICTED,
                    ]:
                        try:
                            await app.unban_chat_member(
                                chat_id=chat_id, user_id=client.id
                            )
                        except Exception:
                            logger.warning(
                                "Assistant %s banned/restricted in chat_id=%s; unban failed",
                                _assistant_label(client),
                                chat_id,
                            )
                            return await _fail_ready(
                                m.lang["play_banned"].format(
                                    app.name,
                                    client.id,
                                    client.mention,
                                    f"@{client.username}" if client.username else None,
                                )
                            )
                except errors.ChatAdminRequired:
                    logger.warning(
                        "Bot lacks admin rights to check assistant in chat_id=%s",
                        chat_id,
                    )
                    return await _fail_ready(m.lang["admin_required"])
                except (
                    errors.UserNotParticipant,
                    errors.exceptions.bad_request_400.UserNotParticipant,
                    errors.PeerIdInvalid,
                    errors.exceptions.bad_request_400.PeerIdInvalid,
                    TimeoutError,
                ):
                    if invite_notice is None:
                        try:
                            invite_notice = await m.reply_text(
                                m.lang["play_invite"].format(app.name)
                            )
                        except Exception:
                            pass

                    if invite_link is None:
                        if m.chat.username:
                            invite_link = m.chat.username
                        else:
                            try:
                                invite_link = (await app.get_chat(chat_id)).invite_link
                                if not invite_link:
                                    invite_link = await app.export_chat_invite_link(chat_id)
                            except errors.ChatAdminRequired:
                                logger.warning(
                                    "Bot lacks admin rights to create invite link for chat_id=%s",
                                    chat_id,
                                )
                                return await _fail_ready(m.lang["admin_required"])
                            except Exception as ex:
                                reason = _normalize_invite_error(ex)
                                logger.warning(
                                    "Failed to create invite link for chat_id=%s: %s",
                                    chat_id,
                                    reason,
                                )
                                return await _fail_ready(
                                    m.lang["play_invite_error"].format(reason)
                                )

                    if m.chat.username:
                        try:
                            await client.resolve_peer(invite_link)
                        except Exception:
                            pass

                    try:
                        await client.join_chat(invite_link)
                    except errors.UserAlreadyParticipant:
                        pass
                    except errors.InviteRequestSent:
                        try:
                            await app.approve_chat_join_request(chat_id, client.id)
                        except errors.HideRequesterMissing:
                            pass
                        except Exception as ex:
                            reason = _normalize_invite_error(ex)
                    except errors.FloodWait as fw:
                        reason = _normalize_invite_error(fw)
                    except Exception as ex:
                        reason = _normalize_invite_error(ex)

                    if reason:
                        last_reason = reason
                    else:
                        try:
                            await client.resolve_peer(chat_id)
                        except Exception:
                            pass
                        if invite_notice:
                            try:
                                await invite_notice.delete()
                            except Exception:
                                pass
                        logger.info(
                            "Assistant invite succeeded for chat_id=%s attempt=%s/%s assistant=%s",
                            chat_id,
                            attempt,
                            total_assistants,
                            _assistant_label(client),
                        )
                        break
                except errors.FloodWait as fw:
                    reason = _normalize_invite_error(fw)
                    last_reason = reason
                except Exception as ex:
                    reason = _normalize_invite_error(ex)
                    last_reason = reason

                if not reason:
                    _assistant_ready.add((chat_id, int(client.id)))
                    logger.info(
                        "Assistant ready for chat_id=%s attempt=%s/%s assistant=%s",
                        chat_id,
                        attempt,
                        total_assistants,
                        _assistant_label(client),
                    )
                    # Start PyTgCalls InputGroupCall + local NTgCalls payload warm
                    # here, while the search/direct resolver is still running.
                    # play_media() calls the same helper later; cache/task dedupe
                    # makes that second call a local no-op instead of racing
                    # create_call() with early-connect.
                    try:
                        call_client = await db.get_assistant(chat_id)
                        vc_warm_task = anon._schedule_vc_metadata_warm(
                            call_client, chat_id
                        )
                        if vc_warm_task is not None:
                            def _vc_command_warm_done(done: asyncio.Task) -> None:
                                if done.cancelled():
                                    return
                                try:
                                    done.result()
                                except Exception:
                                    return
                                logger.info(
                                    "vc_command_overlap_warm_ready chat_id=%s "
                                    "assistant=%s",
                                    chat_id, _assistant_label(client),
                                )
                            vc_warm_task.add_done_callback(_vc_command_warm_done)
                    except Exception as ex:
                        logger.debug(
                            "vc_command_overlap_warm_skipped chat_id=%s error=%s",
                            chat_id, type(ex).__name__,
                        )
                    break

                logger.warning(
                    "Assistant invite attempt failed for chat_id=%s attempt=%s/%s assistant=%s reason=%s",
                    chat_id,
                    attempt,
                    total_assistants,
                    _assistant_label(client),
                    reason,
                )
                if attempt < total_assistants:
                    rotated = await db.rotate_assistant(chat_id)
                    logger.info(
                        "Assistant rotated for chat_id=%s to slot=%s after reason=%s",
                        chat_id,
                        rotated,
                        reason,
                    )
                    continue

                last_reason = last_reason or reason or "InviteFailed"
                logger.warning(
                    "Assistant invite failover exhausted for chat_id=%s after %s attempts; last_reason=%s",
                    chat_id,
                    total_assistants,
                    last_reason,
                )
                failure_text = m.lang["play_invite_error"].format(last_reason)
                if invite_notice:
                    try:
                        await invite_notice.delete()
                    except Exception:
                        pass
                return await _fail_ready(failure_text)

        # Authorized initial YouTube requests can overlap Telegram VC signaling
        # with the tail of search/resolution. The same per-chat lock used by
        # normal queue admission is held until the provisional connection is
        # adopted or rolled back, so a concurrent first request cannot overtake
        # this transaction.
        admission_hint = None
        if admission_task is not None:
            try:
                admission_hint = await admission_task
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logger.debug(
                    "Playback admission warm-up skipped chat_id=%s error=%s",
                    chat_id,
                    type(ex).__name__,
                )
            else:
                if isinstance(admission_hint, dict):
                    setattr(m, "_play_admission_hint", admission_hint)

        preconnect_enabled = bool(
            warm_query
            and "playlist" not in warm_query
            and bool(getattr(config, "YOUTUBE_DIRECT_STREAM", True))
            and isinstance(admission_hint, dict)
            and admission_hint.get("voice_call_active") is True
            and chat_id not in getattr(db, "active_calls", {})
            and queue.get_current(chat_id) is None
            and not bool(getattr(anon, "_raw_direct_disabled_reason", ""))
            and (
                (
                    not video
                    and bool(getattr(config, "DIRECT_PREVALIDATED_RAW_AUDIO", True))
                    and bool(getattr(config, "DIRECT_EXTERNAL_PREBUFFER_AUDIO", True))
                )
                or (
                    video
                    and bool(getattr(config, "DIRECT_PREVALIDATED_RAW_VIDEO", True))
                    and bool(getattr(config, "DIRECT_VIDEO_VC_RESOLVER_OVERLAP", True))
                )
            )
        )
        if preconnect_enabled:
            try:
                initial_lease = await anon.acquire_initial_playback_lease(chat_id)
                if (
                    chat_id in getattr(db, "active_calls", {})
                    or queue.get_current(chat_id) is not None
                    or not _play_request_is_live(request_message_id)
                ):
                    initial_lease.release()
                    initial_lease = None
                else:
                    initial_preconnect = await anon.begin_initial_direct_preconnect(
                        chat_id=chat_id,
                        video=bool(video),
                        trace=trace,
                        request_id=int(getattr(m, "id", 0) or 0),
                    )
                    setattr(m, "_play_initial_lease", initial_lease)
                    setattr(m, "_play_initial_preconnect", initial_preconnect)
            except asyncio.CancelledError:
                if initial_lease is not None:
                    initial_lease.release()
                raise
            except Exception as ex:
                if initial_lease is not None:
                    initial_lease.release()
                    initial_lease = None
                logger.info(
                    "direct_command_preconnect_skipped chat_id=%s reason=%s",
                    chat_id,
                    type(ex).__name__,
                )
        try:
            if warm_external_task is not None:
                try:
                    warmed_external = await warm_external_task
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    # Warm resolution is an optimization. Let the normal play
                    # handler retry the provider instead of turning a transient
                    # warm-up failure into a generic request failure.
                    logger.warning(
                        "External warm-up failed; continuing with normal resolver "
                        "chat_id=%s source=%s: %s",
                        chat_id,
                        (
                            "telegram"
                            if has_media_source or is_tg_message_link or is_tg_story_link
                            else "tiktok"
                            if is_tiktok_url
                            else "facebook"
                        ),
                        ex,
                    )
                    warmed_external = None
                if warmed_external is not None:
                    setattr(m, "_warm_external_media", warmed_external)
            if warm_task is not None:
                try:
                    warmed_search = await asyncio.wait_for(
                        asyncio.shield(warm_task), timeout=4.0
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "Warm YouTube search handoff timed out chat_id=%s",
                        chat_id,
                    )
                    warmed_search = None
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    logger.warning(
                        "Warm YouTube search failed; continuing with normal resolver "
                        "chat_id=%s: %s",
                        chat_id,
                        ex,
                    )
                    warmed_search = None
                if warmed_search is not None:
                    if getattr(warmed_search, "file_path", None):
                        setattr(m, "_warm_cache_media", warmed_search)
                    else:
                        setattr(m, "_warm_search_media", warmed_search)
            if trace:
                trace.mark("search")
            if admission_task is not None:
                try:
                    admission_hint = await admission_task
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    logger.debug(
                        "Playback admission warm-up skipped chat_id=%s error=%s",
                        chat_id,
                        type(ex).__name__,
                    )
                else:
                    if isinstance(admission_hint, dict):
                        setattr(m, "_play_admission_hint", admission_hint)
            trace.mark("guard_ready")
            return await play(
                _,
                m,
                force,
                m3u8,
                video,
                url,
                trace=trace,
                pre_sent=pre_sent,
            )
        except asyncio.CancelledError:
            logger.info(
                "Play handler cancelled chat_id=%s message_id=%s",
                chat_id,
                request_message_id,
            )
            return None
        except Exception as ex:
            logger.exception(
                "Play handler failed chat_id=%s source=%s",
                chat_id,
                (
                    "facebook"
                    if is_facebook_url
                    else "telegram"
                    if is_tg_message_link or is_tg_story_link or has_media_source
                    else "tiktok"
                    if is_tiktok_url
                    else "youtube"
                ),
            )
            failure = m.lang.get("error_no_file") or m.lang.get(
                "play_not_found", m.lang.get("play_error", "An error occurred.")
            )
            try:
                if "{0}" in str(failure):
                    failure = str(failure).format(config.SUPPORT_CHAT)
                if pre_sent is not None:
                    await utils.edit_text(pre_sent, failure, reply_markup=buttons.support_button())
                else:
                    await m.reply_text(failure)
            except Exception:
                logger.debug(
                    "Unable to render terminal play failure chat_id=%s error=%s",
                    chat_id,
                    type(ex).__name__,
                )
        finally:
            transport = getattr(m, "_play_initial_preconnect", None)
            if isinstance(transport, dict) and not transport.get("adopted"):
                try:
                    await asyncio.shield(
                        anon.cancel_initial_direct_preconnect(transport)
                    )
                except Exception as ex:
                    logger.debug(
                        "Initial preconnect cleanup skipped chat_id=%s error=%s",
                        chat_id,
                        type(ex).__name__,
                    )
            lease = getattr(m, "_play_initial_lease", initial_lease)
            if lease is not None:
                lease.release()
            if request_message_id:
                release_play_request_task(request_message_id, request_task)

    return wrapper
