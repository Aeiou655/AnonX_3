# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio

from pyrogram import filters, types

from AnonX_3 import app, config, db, lang, logger, tg, tiktok, facebook, yt
from AnonX_3.core.request_context import enrich_request
from AnonX_3.helpers import buttons, utils
from AnonX_3.helpers._play import (
    _invalidate_play_request,
    attach_cancel_button,
    checkUB,
    stream_media,
)


async def _edit_play_not_found(message, chat_id: int, language: dict):
    """Close request-owned progress work before publishing a terminal miss."""
    await _invalidate_play_request(message)
    template = await db.get_custom_text_for_chat(
        chat_id, "play_not_found", language["play_not_found"]
    )
    return await utils.edit_formatted(
        message,
        template,
        config.SUPPORT_CHAT,
        template_key="play_not_found",
    )


async def _edit_play_retryable(message, language: dict, key: str):
    """Publish a retryable resolver failure without mislabeling it as a miss."""
    await _invalidate_play_request(message)
    return await message.edit_text(
        language.get(key, language["play_not_found"])
    )


@app.on_message(
    filters.command(["play", "playforce", "vplay", "vplayforce"])
    & filters.group
    & ~app.bl_users
)
@lang.language()
@checkUB
async def play_hndlr(
    _,
    m: types.Message,
    force: bool = False,
    m3u8: bool = False,
    video: bool = False,
    url: str = None,
    trace=None,
    pre_sent=None,
) -> None:
    sent = pre_sent
    cancel_label = m.lang.get("cancel", "Cancel")
    cancel_kb = buttons.cancel_dl(cancel_label)
    if sent is None:
        searching_tpl = await db.get_custom_text_for_chat(
            m.chat.id, "play_searching", m.lang["play_searching"]
        )
        try:
            sent = await utils.reply_formatted(
                m,
                searching_tpl,
                template_key="play_searching",
                reply_markup=cancel_kb,
            )
            if trace:
                trace.mark("ack")
        except Exception as ex:
            logger.warning(
                "Skipping /play response due to message send failure chat_id=%s: %s",
                m.chat.id,
                ex,
            )
            return
    if sent is None:
        return
    # checkUB already sends the keyboard with its early acknowledgement.
    # Avoid a second Telegram edit RPC on every /play; only repair custom
    # templates/clients that actually dropped the markup.
    if getattr(sent, "reply_markup", None) is None:
        await attach_cancel_button(sent, cancel_label)
    file = None
    query = None
    mention = m.from_user.mention
    playable = tg.resolve_playable(m)
    tracks = []
    warmed_external = getattr(m, "_warm_external_media", None)
    warmed_cache = getattr(m, "_warm_cache_media", None)
    warmed_search = getattr(m, "_warm_search_media", None)

    if warmed_cache is not None:
        file = warmed_cache
        query = (url or "").strip() or " ".join(
            part for part in m.command[1:] if part != "-f"
        ).strip() or None
    elif warmed_search is not None:
        file = warmed_search
        query = (url or "").strip() or " ".join(
            part for part in m.command[1:] if part != "-f"
        ).strip() or None
    elif warmed_external is not None:
        file = warmed_external
        query = (url or "").strip() or None
    elif playable:
        kind, source = playable
        setattr(sent, "lang", m.lang)
        if kind == "story":
            file = await tg.resolve_story(source, sent.id, video=video)
        else:
            file = await tg.resolve(source, sent.id, video=video)
        # Telegram reply/file: resolve metadata for direct stream (no download yet).
        if not file:
            return await _edit_play_not_found(sent, m.chat.id, m.lang)
    else:
        query = (
            " ".join(part for part in m.command[1:] if part != "-f").strip()
            if len(m.command) >= 2
            else None
        )
        source_kind = "youtube"
        if url and tg.is_story_link(url):
            source_kind = "telegram_story"
        elif url and tg.is_message_link(url):
            source_kind = "telegram_link"
        elif url and tiktok.valid(url):
            source_kind = "tiktok"
        elif url and facebook.valid(url):
            source_kind = "facebook"
        if url and source_kind == "youtube" and "playlist" in url:
            try:
                await utils.edit_text(
                    sent,
                    m.lang["playlist_fetch"],
                    reply_markup=cancel_kb,
                )
            except Exception:
                await sent.edit_text(m.lang["playlist_fetch"])
        if source_kind == "telegram_story":
            story = await tg.fetch_story_link(url, group_chat_id=m.chat.id)
            if not story or not getattr(story, "video", None):
                return await _edit_play_not_found(sent, m.chat.id, m.lang)
            setattr(sent, "lang", m.lang)
            file = await tg.resolve_story(story, sent.id, video=video)
        elif source_kind == "telegram_link":
            linked = await tg.fetch_linked_message(url)
            if not linked or not tg.get_media(linked):
                return await _edit_play_not_found(sent, m.chat.id, m.lang)
            setattr(sent, "lang", m.lang)
            file = await tg.resolve(linked, sent.id, video=video)
        elif source_kind == "tiktok":
            target = (url or query or "").strip()
            file = await tiktok.resolve(target, sent.id, video=video)
            if not file:
                return await _edit_play_not_found(sent, m.chat.id, m.lang)
            file.source = "tiktok_remote"
            file.local_path = f"downloads/{file.id}.{'mp4' if video else 'm4a'}"
        elif source_kind == "facebook":
            target = (url or query or "").strip()
            file = await facebook.resolve(target, sent.id, video=video)
            if not file:
                return await _edit_play_not_found(sent, m.chat.id, m.lang)
            file.source = "facebook_remote"
            file.local_path = f"downloads/{file.id}.{'mp4' if video else 'm4a'}"
        else:
            # Track resolve task so Cancel inline button can abort SEARCHING...
            resolve_task = asyncio.create_task(
                yt.resolve_source(
                    query=query,
                    url=url,
                    m3u8=bool(m3u8),
                    message_id=sent.id,
                    video=video,
                    user=mention,
                    playlist_limit=config.PLAYLIST_LIMIT,
                )
            )
            yt._active_tasks[sent.id] = resolve_task
            try:
                resolve_timeout = max(
                    8.0,
                    float(getattr(config, "PLAY_RESOLVE_TIMEOUT_SEC", 18)),
                )
                file, tracks, err = await asyncio.wait_for(
                    resolve_task,
                    timeout=25.0 if url and "playlist" in url else resolve_timeout,
                )
            except asyncio.CancelledError:
                logger.info(
                    "Play search cancelled chat_id=%s message_id=%s",
                    m.chat.id,
                    sent.id,
                )
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "Source resolution timed out chat_id=%s timeout=%ss",
                    m.chat.id,
                    resolve_timeout,
                )
                return await _edit_play_retryable(
                    sent, m.lang, "play_source_timeout"
                )
            finally:
                if yt._active_tasks.get(sent.id) is resolve_task:
                    yt._active_tasks.pop(sent.id, None)
            if err == "playlist_error":
                await _invalidate_play_request(sent)
                return await sent.edit_text(m.lang["playlist_error"])
            if err == "play_not_found":
                return await _edit_play_not_found(sent, m.chat.id, m.lang)
            if err == "play_source_unavailable":
                return await _edit_play_retryable(
                    sent, m.lang, "play_source_unavailable"
                )

    if not file:
        return await _edit_play_not_found(sent, m.chat.id, m.lang)

    admission_hint = getattr(m, "_play_admission_hint", None)
    if isinstance(admission_hint, dict):
        setattr(file, "_play_admission_hint", admission_hint)
    initial_lease = getattr(m, "_play_initial_lease", None)
    if initial_lease is not None:
        setattr(file, "_initial_playback_lease", initial_lease)
    initial_preconnect = getattr(m, "_play_initial_preconnect", None)
    if isinstance(initial_preconnect, dict):
        setattr(file, "_initial_direct_preconnect", initial_preconnect)

    file_source = str(getattr(file, "source", "") or "").lower()
    file_id = str(getattr(file, "id", "") or "").strip()
    external_download_sources = {
        "telegram_remote",
        "tiktok_remote",
        "facebook_remote",
    }
    # YouTube search and resolve deliberately use separate Track instances.
    # Whichever instance wins the parallel local-cache race must therefore
    # carry the status message.  Previously only external providers received
    # this context, so a YouTube download could run normally while the visible
    # card stayed forever at its initial 0.0%.
    is_youtube_download = (
        file_source not in external_download_sources
        and len(file_id) == 11
        and all(ch.isalnum() or ch in "_-" for ch in file_id)
        and not bool(getattr(file, "file_path", None))
    )
    if (
        (file_source in external_download_sources or is_youtube_download)
        and (force or m.chat.id not in db.active_calls)
        and getattr(file, "download_progress_message", None) is None
    ):
        downloading_tpl = await db.get_custom_text_for_chat(
            m.chat.id, "play_downloading", m.lang["play_downloading"]
        )
        setattr(file, "download_progress_message", sent)
        setattr(file, "download_progress_lang", m.lang)
        setattr(file, "download_progress_template", downloading_tpl)
        setattr(
            file,
            "download_progress_cancel_label",
            m.lang.get("cancel", "Cancel"),
        )

    request_source = str(getattr(m, "_play_request_source", "") or "command")
    user_id = int(getattr(m.from_user, "id", 0) or 0)
    # This status-card ID is the acquisition lifetime for a warm Track and
    # the final resolved Track.  It lets the cache manager carry a terminal
    # one-shot outcome across those otherwise distinct objects.
    setattr(file, "_play_request_scope", int(getattr(sent, "id", 0) or 0))
    enrich_request(
        file,
        chat_id=m.chat.id,
        user_id=user_id,
        query=query or url,
        request_source=request_source,
        priority=100 if request_source == "command" else 25,
    )
    for track in tracks:
        enrich_request(
            track,
            chat_id=m.chat.id,
            user_id=user_id,
            query=getattr(track, "title", None) or query or url,
            request_source="playlist" if len(tracks) > 1 else request_source,
            priority=90 if request_source != "aidj" else 25,
        )

    if trace:
        trace.mark("source_ready")

    await stream_media(
        chat_id=m.chat.id,
        sent=sent,
        media=file,
        tracks=tracks,
        force=force,
        _lang=m.lang,
        user=mention,
        log_msg=m,
        trace=trace,
    )
