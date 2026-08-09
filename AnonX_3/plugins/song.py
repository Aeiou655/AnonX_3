# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import os
import re
from html import escape
from pathlib import Path

from pyrogram import filters, types

from AnonX_3 import app, db, lang, logger, tg, thumb, tiktok, yt
from AnonX_3.helpers import Track, buttons, utils


_VIDEO_FLAGS = {"-v", "--video"}
_VIDEO_CACHE_REVISION = "thumb-v1"
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


def _parse_song_tokens(command, raw_text: str | None) -> tuple[bool, str]:
    """Parse command data defensively across Pyrogram/Kurigram variants."""
    tokens = [str(part) for part in (command or []) if str(part).strip()]
    if len(tokens) <= 1:
        raw_tokens = str(raw_text or "").strip().split()
        if raw_tokens and (not tokens or len(raw_tokens) > 1):
            command_name = raw_tokens[0].lstrip("/").split("@", 1)[0]
            tokens = [command_name, *raw_tokens[1:]]
    command_name = tokens[0].lower().lstrip("/").split("@", 1)[0] if tokens else ""
    args = tokens[1:]
    # Keep the public contract unambiguous: /song is audio and /vsong is
    # video.  Legacy video flags are ignored instead of changing /song mode.
    video = command_name == "vsong"
    query = " ".join(
        arg for arg in args if arg.casefold() not in _VIDEO_FLAGS
    ).strip()
    return video, query


def _media_cache_id(media_id: str, video: bool) -> str:
    """Invalidate legacy video file_ids that may not contain a thumbnail."""
    if video:
        return f"{media_id}:{_VIDEO_CACHE_REVISION}"
    return media_id


def _parse_song_request(message: types.Message) -> tuple[bool, str]:
    return _parse_song_tokens(
        getattr(message, "command", None),
        getattr(message, "text", None) or getattr(message, "caption", None),
    )


def _first_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0).rstrip(".,!?)］】") if match else None


async def _convert_to_mp3(source_path: str, output_path: str) -> bool:
    target = Path(output_path)
    try:
        if target.is_file() and target.stat().st_size >= 16 * 1024:
            return True
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.stem}.part{target.suffix}")
    try:
        partial.unlink(missing_ok=True)
    except OSError:
        return False

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        source_path,
        "-vn",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-b:a",
        "192k",
        str(partial),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await proc.wait() != 0:
        partial.unlink(missing_ok=True)
        return False
    try:
        if not partial.is_file() or partial.stat().st_size < 16 * 1024:
            partial.unlink(missing_ok=True)
            return False
        os.replace(partial, target)
        return True
    except OSError:
        partial.unlink(missing_ok=True)
        return False


async def _send_video_song(
    message: types.Message,
    upload_path: str,
    track,
    caption: str,
) -> types.Message | None:
    try:
        video_cover = await thumb.generate(track)
    except Exception as ex:
        logger.debug("Song video cover generation skipped: %s", ex)
        video_cover = None
    try:
        video_thumb = await thumb.generate_video_thumb(track)
    except Exception as ex:
        logger.debug("Song video thumbnail generation skipped: %s", ex)
        video_thumb = None
    kwargs = {
        "chat_id": message.chat.id,
        "video": upload_path,
        "caption": caption,
        "thumb": video_thumb,
        "supports_streaming": True,
        "duration": track.duration_sec or 0,
        "reply_to_message_id": message.id,
    }
    if video_cover:
        kwargs["video_cover"] = video_cover

    try:
        return await app.send_video(**kwargs)
    except TypeError as ex:
        if "video_cover" not in str(ex):
            raise
        kwargs.pop("video_cover", None)
        if not video_cover:
            return await app.send_video(**kwargs)
        try:
            return await app.send_video(**kwargs, cover=video_cover)
        except TypeError as ex2:
            if "cover" not in str(ex2):
                raise
            return await app.send_video(**kwargs)


def _track_from_telegram_media(media) -> Track:
    return Track(
        id=media.id,
        channel_name="Telegram",
        duration=media.duration or "00:00",
        duration_sec=media.duration_sec or 0,
        title=(media.title or "Telegram Video")[:80],
        thumbnail=media.thumbnail,
        url=media.url,
        file_path=media.local_path or media.file_path,
        view_count="N/A",
        video=bool(media.video),
    )


@app.on_message(filters.command(["song", "vsong"]) & ~app.bl_users, group=-1)
@lang.language()
async def song_hndlr(_, m: types.Message) -> None:
    video, query = _parse_song_request(m)
    source_kind = "youtube"
    tg_media = None

    # An explicit search query must not be replaced by an unrelated URL in the
    # replied-to message. Only fall back to reply entities when no query exists.
    url = _first_url(query) or (utils.get_url(m) if not query else None)
    if url:
        if tg.is_message_link(url):
            source_kind = "telegram_link"
        elif tiktok.valid(url):
            source_kind = "tiktok"
        elif yt.invalid(url):
            return await m.reply_text(m.lang["song_not_found"])
        query = url

    if not query:
        return await m.reply_text(m.lang["song_usage"])

    # The styled Cancel button is a Bot API dictionary. Route it through the
    # Utilities boundary so direct Pyrogram serialization never calls .write()
    # on that dictionary.
    cancel_kb = buttons.cancel_dl(m.lang.get("cancel", "Cancel"))
    sent = await utils.reply_formatted(
        m,
        m.lang["song_searching"],
        template_key="song_searching",
        reply_markup=cancel_kb,
    )
    if sent is None:
        return
    try:
        if source_kind == "telegram_link":
            linked = await tg.fetch_linked_message(query)
            if not linked or not tg.get_media(linked):
                return await sent.edit_text(m.lang["song_not_found"])
            setattr(sent, "lang", m.lang)
            tg_media = await tg.download(linked, sent)
            if not tg_media or not Path(
                tg_media.local_path or tg_media.file_path or ""
            ).is_file():
                return await sent.edit_text(m.lang["song_download_failed"])
            track = _track_from_telegram_media(tg_media)
        elif source_kind == "tiktok":
            track = await tiktok.resolve(query, sent.id, video=video)
        else:
            track = await yt.search(query, sent.id, video=video)
            if not track:
                results = await yt.deep_search(query, sent.id, video=video)
                track = results[0] if results else None
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        logger.exception(
            "Song resolve failed source=%s video=%s query=%r: %s",
            source_kind,
            video,
            query[:120],
            ex,
        )
        return await sent.edit_text(m.lang["song_not_found"])
    if not track:
        return await sent.edit_text(m.lang["song_not_found"])

    cache_id = _media_cache_id(track.id, video)
    cached_fid = await db.get_cached_file(cache_id, video=video)
    if cached_fid:
        requester = "Unknown"
        if m.from_user is not None:
            requester = m.from_user.mention
        elif m.sender_chat is not None:
            requester = escape(m.sender_chat.title or "AnonX Admin")
        safe_title = escape(track.title or "Unknown")
        caption = m.lang["song_caption"].format(safe_title, requester)
        try:
            if video:
                await app.send_video(
                    chat_id=m.chat.id,
                    video=cached_fid,
                    caption=caption,
                    reply_to_message_id=m.id,
                )
            else:
                await app.send_audio(
                    chat_id=m.chat.id,
                    audio=cached_fid,
                    caption=caption,
                    duration=track.duration_sec or 0,
                    title=track.title[:64] if track.title else "Song",
                    performer=track.channel_name or None,
                    reply_to_message_id=m.id,
                )
            await sent.edit_text(m.lang["song_uploading"])
            try:
                await sent.delete()
            except Exception:
                pass
            return
        except Exception as ex:
            logger.info(
                "Cached song file_id rejected id=%s video=%s error=%s",
                track.id,
                video,
                type(ex).__name__,
            )

    # Do not publish a second plain "Downloading song..." state.  The original
    # Cancel-enabled card stays in place and provider byte-progress, when
    # available, updates that same message.
    try:
        if source_kind == "telegram_link":
            source_path = tg_media.local_path or tg_media.file_path
        elif source_kind == "tiktok":
            source_path = await tiktok.download(
                url=query,
                media_id=track.id,
                video=video,
                message_id=sent.id,
            )
        else:
            source_path = await yt.download(
                track.id,
                video=video,
                message_id=sent.id,
                progress_message=sent,
                progress_lang=m.lang,
                progress_media=track,
            )
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        logger.exception(
            "Song download failed source=%s id=%s video=%s: %s",
            source_kind,
            track.id,
            video,
            ex,
        )
        return await sent.edit_text(m.lang["song_download_failed"])
    if not source_path or not Path(source_path).is_file():
        return await sent.edit_text(m.lang["song_download_failed"])
    track.file_path = source_path

    upload_path = source_path
    if not video:
        await sent.edit_text(m.lang["song_converting"])
        mp3_path = f"downloads/{track.id}.mp3"
        if not await _convert_to_mp3(source_path, mp3_path):
            return await sent.edit_text(m.lang["song_convert_failed"])
        upload_path = mp3_path

    await sent.edit_text(m.lang["song_uploading"])
    requester = "Unknown"
    if m.from_user is not None:
        requester = m.from_user.mention
    elif m.sender_chat is not None:
        requester = escape(m.sender_chat.title or "AnonX Admin")
    safe_title = escape(track.title or "Unknown")
    caption = m.lang["song_caption"].format(safe_title, requester)

    sent_msg = None
    try:
        if video:
            sent_msg = await _send_video_song(m, upload_path, track, caption)
        else:
            sent_msg = await app.send_audio(
                chat_id=m.chat.id,
                audio=upload_path,
                caption=caption,
                duration=track.duration_sec or 0,
                title=track.title[:64] if track.title else "Song",
                performer=track.channel_name or None,
                reply_to_message_id=m.id,
            )
    except Exception as ex:
        logger.exception(
            "Song upload failed id=%s video=%s path=%s: %s",
            track.id,
            video,
            upload_path,
            ex,
        )
        return await sent.edit_text(m.lang["song_upload_failed"])

    if sent_msg is not None:
        fid = None
        if video and hasattr(sent_msg, "video") and sent_msg.video:
            fid = sent_msg.video.file_id
        elif not video and hasattr(sent_msg, "audio") and sent_msg.audio:
            fid = sent_msg.audio.file_id
        if fid:
            await db.set_cached_file(cache_id, fid, video=video)

    try:
        await sent.delete()
    except Exception:
        pass
