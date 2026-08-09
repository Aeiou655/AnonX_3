# Copyright (c) 2025 AnonX
# Licensed under the MIT License

import asyncio
import re

from pyrogram import enums, types

from AnonX_3 import app, logger, yt


INLINE_RESULT_LIMIT = 8
INLINE_QUERY_MAX_CHARS = 160
INLINE_DEBOUNCE_SEC = 0.25
INLINE_SEARCH_TIMEOUT_SEC = 7.0
INLINE_CACHE_TIME_SEC = 30
_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_INLINE_SEARCH_LIMITER = asyncio.Semaphore(6)
_LATEST_QUERY_BY_USER: dict[int | str, str] = {}


def _normalize_inline_query(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:INLINE_QUERY_MAX_CHARS]


def _vsong_command(
    video_id: str | None,
    bot_username: str | None,
) -> tuple[str, int] | None:
    clean_id = str(video_id or "").strip()
    if not _YOUTUBE_VIDEO_ID_RE.fullmatch(clean_id):
        return None

    username = str(bot_username or "").strip().lstrip("@")
    suffix = f"@{username}" if _BOT_USERNAME_RE.fullmatch(username) else ""
    command = f"/vsong{suffix}"
    url = f"https://www.youtube.com/watch?v={clean_id}"
    return f"{command} {url}", len(command)


def _build_inline_result(track, bot_username: str | None):
    handoff = _vsong_command(getattr(track, "id", None), bot_username)
    if handoff is None:
        return None
    message_text, command_length = handoff
    video_id = str(track.id).strip()
    title = str(getattr(track, "title", None) or "YouTube Video").strip()[:128]
    channel = str(getattr(track, "channel_name", None) or "YouTube").strip()
    duration = str(getattr(track, "duration", None) or "").strip()
    description = " | ".join(part for part in (channel, duration) if part)[:180]
    thumbnail = str(getattr(track, "thumbnail", None) or "").strip()
    if not thumbnail.startswith(("https://", "http://")):
        thumbnail = None

    return types.InlineQueryResultArticle(
        id=f"vsong:{video_id}",
        title=title,
        description=description,
        thumb_url=thumbnail,
        input_message_content=types.InputTextMessageContent(
            message_text=message_text,
            entities=[
                types.MessageEntity(
                    type=enums.MessageEntityType.BOT_COMMAND,
                    offset=0,
                    length=command_length,
                )
            ],
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
        ),
    )


async def _search_inline_tracks(query: str):
    async with _INLINE_SEARCH_LIMITER:
        return await yt.deep_search(
            query,
            0,
            video=True,
            limit=INLINE_RESULT_LIMIT,
        )


async def _answer_inline(inline_query, results, cache_time: int) -> None:
    try:
        await inline_query.answer(
            results=results,
            cache_time=cache_time,
            is_personal=True,
            next_offset="",
        )
    except asyncio.CancelledError:
        raise
    except Exception as ex:
        logger.debug(
            "Inline query answer skipped query_id=%s error_type=%s",
            getattr(inline_query, "id", "unknown"),
            type(ex).__name__,
        )


@app.on_inline_query(group=-1)
async def inline_song_search(client, inline_query: types.InlineQuery) -> None:
    user_id = getattr(getattr(inline_query, "from_user", None), "id", 0) or 0
    if user_id and user_id in app.bl_users:
        return await _answer_inline(inline_query, [], 1)

    query = _normalize_inline_query(getattr(inline_query, "query", None))
    if not query:
        return await _answer_inline(inline_query, [], 1)

    # Telegram sends a query for nearly every keystroke. Let only the most
    # recent query from each user start an external provider race.
    query_id = str(getattr(inline_query, "id", "") or "")
    user_key = user_id or f"query:{query_id}"
    _LATEST_QUERY_BY_USER[user_key] = query_id
    if len(_LATEST_QUERY_BY_USER) > 2048:
        _LATEST_QUERY_BY_USER.pop(next(iter(_LATEST_QUERY_BY_USER)), None)
    await asyncio.sleep(INLINE_DEBOUNCE_SEC)
    if _LATEST_QUERY_BY_USER.get(user_key) != query_id:
        return await _answer_inline(inline_query, [], 1)

    try:
        tracks = await asyncio.wait_for(
            _search_inline_tracks(query),
            timeout=INLINE_SEARCH_TIMEOUT_SEC,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        logger.info("Inline song search timed out user_id=%s", user_id)
        tracks = []
    except Exception as ex:
        logger.warning(
            "Inline song search failed user_id=%s error_type=%s",
            user_id,
            type(ex).__name__,
        )
        tracks = []

    username = getattr(getattr(client, "me", None), "username", None)
    results = []
    seen_ids: set[str] = set()
    for track in tracks or []:
        video_id = str(getattr(track, "id", None) or "").strip()
        if video_id in seen_ids:
            continue
        result = _build_inline_result(track, username)
        if result is None:
            continue
        seen_ids.add(video_id)
        results.append(result)
        if len(results) >= INLINE_RESULT_LIMIT:
            break

    await _answer_inline(inline_query, results, INLINE_CACHE_TIME_SEC)
