# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import os
import re
import time
from contextlib import suppress

from pyrogram import errors, types

from AnonX_3 import config, logger
from AnonX_3.core.downloader.singleflight import SingleFlight
from AnonX_3.core.downloader.validation import is_playable_media
from AnonX_3.helpers import Media, buttons, utils


_telegram_download_flight = SingleFlight("telegram-download")
_BOT_API_DIRECT_MAX_BYTES = 18 * 1024 * 1024


class Telegram:
    def __init__(self):
        self.active = []
        self.events = {}
        self.last_edit = {}
        self.active_tasks = {}
        self.current_cache: dict[int, tuple[Media, asyncio.Task]] = {}
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_complete = False
        self.sleep = 5
        try:
            timeout = float(getattr(config, "TELEGRAM_DOWNLOAD_TIMEOUT_SEC", 420))
        except Exception:
            timeout = 420.0
        self.download_timeout = max(30.0, timeout)
        try:
            retries = int(getattr(config, "TELEGRAM_DOWNLOAD_RETRIES", 1))
        except Exception:
            retries = 1
        self.download_retries = max(0, retries)

    @staticmethod
    def _duration_label(seconds: int) -> str:
        total = max(0, int(seconds or 0))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _parse_message_link(self, url: str) -> tuple[str | int, int] | None:
        link = (url or "").strip()
        if not link:
            return None
        # Story links use /s/<id> — exclude them from message-link parsing.
        if re.search(r"/s/\d+", link, flags=re.IGNORECASE):
            return None

        # https://t.me/<username>/<message_id>
        m_public = re.match(
            r"^https?://t\.me/(?P<chat>[A-Za-z0-9_]{5,})/(?P<msg>\d+)(?:\?.*)?$",
            link,
            flags=re.IGNORECASE,
        )
        if m_public:
            return m_public.group("chat"), int(m_public.group("msg"))

        # https://t.me/c/<internal_id>/<message_id> -> chat_id = -100<internal_id>
        m_private = re.match(
            r"^https?://t\.me/c/(?P<internal>\d+)/(?P<msg>\d+)(?:\?.*)?$",
            link,
            flags=re.IGNORECASE,
        )
        if m_private:
            return int(f"-100{m_private.group('internal')}"), int(m_private.group("msg"))
        return None

    def _parse_story_link(self, url: str) -> tuple[str | int, int] | None:
        link = (url or "").strip()
        if not link:
            return None
        # https://t.me/<username>/s/<story_id>
        m_public = re.match(
            r"^https?://t\.me/(?P<chat>[A-Za-z0-9_]{5,})/s/(?P<story>\d+)(?:\?.*)?$",
            link,
            flags=re.IGNORECASE,
        )
        if m_public:
            return m_public.group("chat"), int(m_public.group("story"))
        # https://t.me/c/<internal_id>/s/<story_id>
        m_private = re.match(
            r"^https?://t\.me/c/(?P<internal>\d+)/s/(?P<story>\d+)(?:\?.*)?$",
            link,
            flags=re.IGNORECASE,
        )
        if m_private:
            return int(f"-100{m_private.group('internal')}"), int(m_private.group("story"))
        return None

    def is_message_link(self, url: str) -> bool:
        return self._parse_message_link(url) is not None

    def is_story_link(self, url: str) -> bool:
        return self._parse_story_link(url) is not None

    @staticmethod
    def is_telegram_file_id(value: str) -> bool:
        """Return True when *value* looks like a Telegram file ID or file unique ID.

        Telegram file IDs / file unique IDs are base64-encoded binary blobs
        that start with well-known type prefixes (audio, video, document,
        voice, animation, video-note, photo, sticker).  Bot API file IDs
        returned by ``sendDocument`` and similar calls can also contain a
        leading ``A`` variant prefix.
        """
        if not value or not isinstance(value, str):
            return False
        text = value.strip()
        if len(text) < 20:
            return False
        return bool(
            re.match(
                r"^A?[BQCD][AQU][A-Za-z0-9+/=_-]{18,}$",
                text,
            )
        )

    async def fetch_linked_message(self, url: str) -> types.Message | None:
        parsed = self._parse_message_link(url)
        if not parsed:
            return None
        chat_ref, msg_id = parsed
        try:
            from AnonX_3 import app

            linked = await app.get_messages(chat_ref, msg_id)
            if linked and self._media_obj(linked):
                return linked
        except Exception:
            pass
        for client in await self._assistant_clients():
            try:
                linked = await client.get_messages(chat_ref, msg_id)
                if linked and self._media_obj(linked):
                    return linked
            except Exception:
                continue
        return None

    @staticmethod
    def _media_obj(target) -> object | None:
        """Playable media blob from a Message or Story-like object."""
        if not target:
            return None
        return (
            getattr(target, "audio", None)
            or getattr(target, "voice", None)
            or getattr(target, "video", None)
            or getattr(target, "video_note", None)
            or getattr(target, "animation", None)
            or getattr(target, "document", None)
        )

    def get_media(self, msg: types.Message | None) -> bool:
        """True when a message carries playable audio/video media (incl. forwards)."""
        if not msg:
            return False
        if self._media_obj(msg):
            return True
        story = getattr(msg, "story", None)
        if story and getattr(story, "video", None):
            return True
        return False

    def has_playable_source(self, m: types.Message) -> bool:
        """True when /play can use media/story on the command, reply, or story reply."""
        return self.resolve_playable(m) is not None

    def resolve_playable(self, m: types.Message) -> tuple[str, object] | None:
        """
        Resolve the best playable source from a /play command message.

        Returns:
            ("message", Message) for telegram audio/video/document/voice/video_note/animation
            ("story", Story) for story media / reply-to-story / story embed
        """
        if not m:
            return None

        # 1) Reply to a normal (or forwarded) media message.
        replied = getattr(m, "reply_to_message", None)
        if replied:
            if self._media_obj(replied):
                return ("message", replied)
            story = getattr(replied, "story", None)
            if story:
                return ("story", story)

        # 2) Reply directly to a story (reply_to_story).
        reply_story = getattr(m, "reply_to_story", None)
        if reply_story:
            return ("story", reply_story)

        # 3) Media on the command message itself (forward + caption /play).
        if self._media_obj(m):
            return ("message", m)

        # 4) Embedded story media on the command message.
        story = getattr(m, "story", None)
        if story:
            return ("story", story)

        return None

    def _story_peer(self, story) -> str | int | None:
        chat = getattr(story, "chat", None)
        if chat is not None and getattr(chat, "id", None) is not None:
            return chat.id
        if chat is not None and getattr(chat, "username", None):
            return chat.username
        user = getattr(story, "from_user", None)
        if user is not None and getattr(user, "id", None) is not None:
            return user.id
        if user is not None and getattr(user, "username", None):
            return user.username
        sender = getattr(story, "sender_chat", None)
        if sender is not None and getattr(sender, "id", None) is not None:
            return sender.id
        if sender is not None and getattr(sender, "username", None):
            return sender.username
        return None

    async def _assistant_clients(self, group_chat_id: int | None = None):
        from AnonX_3 import db, userbot

        clients = list(getattr(userbot, "clients", None) or [])
        if not clients:
            return []
        # Never return a PyTgCalls / non-Pyrogram client as a
        # Telegram API helper — only Pyrogram Client instances
        # support get_messages(), get_stories(), etc.
        clients = [
            c for c in clients
            if hasattr(c, "get_messages") and callable(getattr(c, "get_messages", None))
        ]
        if not clients:
            return []
        ordered = []
        if group_chat_id is not None:
            try:
                preferred = await db.get_assistant(group_chat_id)
                if preferred is not None:
                    ordered.append(preferred)
            except Exception:
                pass
        for client in clients:
            if client not in ordered:
                ordered.append(client)
        return ordered

    async def hydrate_story(self, story, group_chat_id: int | None = None):
        """Ensure story has downloadable video via userbot (bots cannot fetch full stories)."""
        if story is None:
            return None
        if getattr(story, "video", None):
            return story

        peer = self._story_peer(story)
        story_id = getattr(story, "id", None)
        if peer is None or not story_id:
            return story

        for client in await self._assistant_clients(group_chat_id):
            try:
                if not hasattr(client, "get_stories"):
                    continue
                result = await client.get_stories(peer, story_ids=story_id)
                items = result if isinstance(result, list) else [result]
                for item in items:
                    if item is None:
                        continue
                    if getattr(item, "video", None):
                        return item
                    # Keep last non-empty item even if photo-only.
                    story = item
            except Exception as ex:
                logger.warning(
                    "Story hydrate failed peer=%s story_id=%s client=%s: %s",
                    peer,
                    story_id,
                    getattr(client, "me", None) and getattr(client.me, "id", None),
                    ex,
                )
        return story

    async def fetch_story_link(
        self, url: str, group_chat_id: int | None = None
    ):
        parsed = self._parse_story_link(url)
        if not parsed:
            return None
        peer, story_id = parsed

        class _StoryStub:
            def __init__(self, sid, chat_ref):
                self.id = sid
                self.chat = type(
                    "ChatRef",
                    (),
                    {
                        "id": chat_ref,
                        "username": chat_ref if isinstance(chat_ref, str) else None,
                    },
                )()
                self.from_user = None
                self.sender_chat = None
                self.video = None
                self.photo = None

        return await self.hydrate_story(_StoryStub(story_id, peer), group_chat_id)

    async def resolve_direct_stream(
        self,
        *,
        file_id: str | None = None,
        media: Media | None = None,
    ) -> tuple[str | None, str | None]:
        """Get a direct Telegram streaming URL via Bot API getFile.
        
        Returns (stream_url, local_path). The stream URL is from Telegram's CDN
        and can be played directly without downloading to disk first.
        """
        from AnonX_3 import bot_api

        fid = file_id or (getattr(media, "telegram_file_id", None) if media else None)
        if not fid:
            return None, None

        local_path = getattr(media, "local_path", None) if media else None
        if not local_path:
            suffix = "mp4" if bool(getattr(media, "video", False)) else "m4a"
            local_path = f"downloads/tg_{self._safe_path_token(str(fid))}.{suffix}"

        file_size = int(getattr(media, "telegram_file_size", 0) or 0)
        if file_size > _BOT_API_DIRECT_MAX_BYTES:
            # api.telegram.org/getFile cannot download large files. The local
            # cache has already started in parallel and uses assistant MTProto,
            # which supports Telegram's multi-gigabyte media.
            logger.info(
                "Telegram media bypassing Bot API getFile; using assistant MTProto "
                "media_id=%s size=%s",
                getattr(media, "id", None),
                file_size,
            )
            return None, local_path

        try:
            result = await bot_api._request("getFile", {"file_id": str(fid)})
        except bot_api.FileTooLarge:
            logger.info(
                "Telegram Bot API file limit reached; switching to assistant MTProto "
                "media_id=%s",
                getattr(media, "id", None),
            )
            return None, local_path
        except Exception as ex:
            logger.warning("Telegram getFile failed for file_id=%s: %s", fid, ex)
            return None, local_path

        if not isinstance(result, dict):
            return None, local_path

        file_path = result.get("file_path")
        if not file_path:
            return None, local_path

        token = getattr(config, "BOT_TOKEN", "") or ""
        if not token:
            return None, local_path

        stream_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        return stream_url, local_path

    async def ensure_local_file(self, media: Media) -> str | None:
        local_path = getattr(media, "local_path", None) or getattr(media, "file_path", None)
        file_id = getattr(media, "telegram_file_id", None) or getattr(media, "id", None)
        video = bool(getattr(media, "video", False))
        if local_path and await asyncio.to_thread(
            is_playable_media,
            local_path,
            video=video,
        ):
            return local_path
        if not file_id or not local_path:
            return None

        async def _owner() -> str | None:
            if await asyncio.to_thread(
                is_playable_media,
                local_path,
                video=video,
            ):
                return local_path
            source_message = getattr(media, "telegram_message", None)
            source_story = getattr(media, "telegram_story", None)
            progress_message = getattr(media, "download_progress_message", None)
            progress_template = getattr(
                media,
                "download_progress_template",
                "Downloading...",
            )
            progress_lang = getattr(media, "download_progress_lang", None) or {}
            cancel_label = getattr(
                media,
                "download_progress_cancel_label",
                progress_lang.get("cancel", "Cancel"),
            )
            progress_started = time.monotonic()
            progress_last_edit = 0.0

            async def _live_progress(current, total):
                nonlocal progress_last_edit
                if progress_message is None or utils.is_download_progress_closed(
                    progress_message
                ):
                    return
                now = time.monotonic()
                current = max(0, int(current or 0))
                total = max(0, int(total or 0))
                # Update at real progress milestones while keeping Telegram
                # edits safely below flood limits.
                if current < total and now - progress_last_edit < 2.0:
                    return
                elapsed = max(0.001, now - progress_started)
                speed = current / elapsed
                eta = int((total - current) / speed) if total and speed else 0
                rendered = utils.render_download_progress(
                    progress_template,
                    current=current,
                    total=total,
                    speed=speed,
                    eta_seconds=eta,
                )
                progress_last_edit = now
                try:
                    await utils.edit_download_progress(
                        progress_message,
                        rendered,
                        reply_markup=buttons.cancel_dl(cancel_label),
                        media=media,
                    )
                except Exception:
                    # Progress UI is best-effort and must never abort the
                    # underlying multi-gigabyte MTProto transfer.
                    pass

            try:
                if source_message is not None:
                    await self._download_bytes_to_path(
                        source_message, local_path, _live_progress
                    )
                elif source_story is not None:
                    if hasattr(source_story, "download"):
                        await source_story.download(
                            file_name=local_path,
                            progress=_live_progress,
                        )
                    else:
                        client = getattr(source_story, "_client", None)
                        if client is None:
                            clients = await self._assistant_clients()
                            client = clients[0] if clients else None
                        if client is None:
                            raise RuntimeError("No assistant can download Telegram story")
                        await client.download_media(
                            source_story,
                            file_name=local_path,
                            progress=_live_progress,
                        )
                else:
                    from AnonX_3 import app

                    await app.download_media(
                        file_id,
                        file_name=local_path,
                        progress=_live_progress,
                    )
                if await asyncio.to_thread(
                    is_playable_media,
                    local_path,
                    video=video,
                ):
                    setattr(media, "local_path", local_path)
                    return local_path
                logger.warning(
                    "Telegram local artifact missing required streams media_id=%s video=%s",
                    getattr(media, "id", None),
                    int(video),
                )
            except Exception as ex:
                logger.warning(
                    "Telegram parallel local download failed media_id=%s: %s",
                    getattr(media, "id", None),
                    ex,
                )
            return None

        return await _telegram_download_flight.do(
            f"telegram:{self._safe_path_token(str(file_id))}",
            _owner,
        )

    async def start_current_cache(self, chat_id: int, media: Media) -> None:
        if self._shutting_down:
            return
        if not config.TELEGRAM_DIRECT_CACHE_BG:
            return
        if not media or getattr(media, "source", None) != "telegram_remote":
            return
        existing = self.current_cache.get(chat_id)
        if existing and existing[0] is media and not existing[1].done():
            return

        async def _runner(target: Media) -> None:
            try:
                await self.ensure_local_file(target)
            except asyncio.CancelledError:
                # Explicit cancellation boundary: don't propagate further
                pass
            except Exception:
                pass

        task = asyncio.create_task(_runner(media))
        self.current_cache[chat_id] = (media, task)

        def _cleanup(_):
            current = self.current_cache.get(chat_id)
            if current and current[1] is task:
                self.current_cache.pop(chat_id, None)

        task.add_done_callback(_cleanup)

    async def await_current_cache_or_download(
        self,
        chat_id: int,
        media: Media,
        ping: float | None = None,
    ) -> str | None:
        current = self.current_cache.get(chat_id)
        if current and current[0] is media:
            task = current[1]
            if not task.done():
                try:
                    await asyncio.wait_for(
                        task,
                        timeout=config.resolve_cache_timeout(
                            config.TELEGRAM_DIRECT_CACHE_TIMEOUT_SEC,
                            ping=ping,
                        ),
                    )
                except Exception:
                    pass
            self.current_cache.pop(chat_id, None)

        return await self.ensure_local_file(media)

    async def cancel(self, query: types.CallbackQuery) -> bool:
        """Cancel Telegram work only; the callback handler owns all UI cleanup."""
        event = self.events.get(query.message.id)
        task = self.active_tasks.pop(query.message.id, None)
        if event:
            event.set()

        if task and not task.done():
            task.cancel()
        return bool(event or task)

    async def shutdown(self) -> None:
        """Cancel and await every cache/download task owned by this provider."""

        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutting_down = True
            current = asyncio.current_task()
            owned = [
                *(entry[1] for entry in self.current_cache.values()),
                *self.active_tasks.values(),
            ]
            self.current_cache.clear()
            self.active_tasks.clear()
            for event in self.events.values():
                event.set()
            self.events.clear()

            tasks = list(dict.fromkeys(task for task in owned if task is not current))
            for task in tasks:
                if not task.done():
                    task.cancel()

            failures: list[Exception] = []
            try:
                results = await asyncio.gather(
                    *tasks,
                    _telegram_download_flight.shutdown(),
                    return_exceptions=True,
                )
                failures.extend(
                    result for result in results if isinstance(result, Exception)
                )
            finally:
                self.active.clear()
                self.last_edit.clear()
                self._shutdown_complete = True

            if failures:
                raise ExceptionGroup("Telegram provider shutdown failed", failures)

    @staticmethod
    def _safe_path_token(value: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "tg"))
        return (text[:80] or "tg").strip("_") or "tg"

    def _describe_media(
        self, container, media, *, default_title: str = "Telegram File"
    ) -> tuple[str, str, int, int, bool, str]:
        """Return file_id, ext, size, duration, is_video, title."""
        raw_uid = getattr(media, "file_unique_id", None) or str(
            getattr(media, "file_id", "tg")
        )
        file_id = self._safe_path_token(raw_uid)
        raw_name = getattr(media, "file_name", None) or ""
        file_ext = raw_name.split(".")[-1].lower() if "." in raw_name else ""
        file_ext = re.sub(r"[^a-z0-9]", "", file_ext)[:12]
        file_size = int(getattr(media, "file_size", 0) or 0)
        duration = int(getattr(media, "duration", 0) or 0)
        mime = (getattr(media, "mime_type", None) or "").lower()
        ext_video = file_ext in {
            "mp4",
            "mkv",
            "webm",
            "mov",
            "avi",
            "m4v",
            "mpeg",
            "mpg",
        }
        is_video = bool(
            getattr(container, "video", None)
            or getattr(container, "video_note", None)
            or getattr(container, "animation", None)
            or mime.startswith("video/")
            or ext_video
        )
        if not file_ext:
            if is_video:
                file_ext = "mp4"
            elif getattr(container, "voice", None):
                file_ext = "ogg"
            elif mime.startswith("audio/") or getattr(container, "audio", None):
                file_ext = "mp3"
            else:
                file_ext = "bin"
        file_title = str(
            getattr(media, "title", None) or raw_name or default_title
        )
        return file_id, file_ext, file_size, duration, is_video, file_title

    async def _download_bytes_to_path(self, msg: types.Message, path: str, progress) -> str:
        """Try bot download, then assistants (large files / FILE_REFERENCE recovery)."""
        os.makedirs(os.path.dirname(path) or "downloads", exist_ok=True)
        # Remove empty/partial leftovers
        if os.path.exists(path) and os.path.getsize(path) <= 0:
            with suppress(Exception):
                os.remove(path)

        errors_log: list[str] = []
        file_size = 0
        media = self._media_obj(msg)
        if media is not None:
            file_size = int(getattr(media, "file_size", 0) or 0)

        # Bots are capped ~20MB; prefer assistant for larger files.
        prefer_assistant = file_size > 18 * 1024 * 1024
        strategies: list = []

        async def _via_message_download():
            return await msg.download(file_name=path, progress=progress)

        async def _via_app_file_id():
            from AnonX_3 import app

            media_obj = self._media_obj(msg)
            file_id = getattr(media_obj, "file_id", None) if media_obj else None
            if not file_id:
                raise RuntimeError("No file_id on media")
            return await app.download_media(file_id, file_name=path, progress=progress)

        async def _via_assistants():
            chat_id = getattr(getattr(msg, "chat", None), "id", None)
            msg_id = getattr(msg, "id", None)
            if chat_id is None or msg_id is None:
                raise RuntimeError("Missing chat/message id for assistant download")
            last_ex: Exception | None = None
            for client in await self._assistant_clients(chat_id):
                try:
                    fetched = await client.get_messages(chat_id, msg_id)
                    if not fetched:
                        continue
                    return await fetched.download(file_name=path, progress=progress)
                except Exception as ex:
                    last_ex = ex
                    logger.warning(
                        "Assistant telegram download failed client=%s: %s",
                        getattr(getattr(client, "me", None), "id", None),
                        ex,
                    )
            raise RuntimeError(
                f"Assistant download failed: {type(last_ex).__name__ if last_ex else 'none'}"
            )

        if prefer_assistant:
            strategies = [_via_assistants, _via_message_download, _via_app_file_id]
        else:
            strategies = [_via_message_download, _via_app_file_id, _via_assistants]

        last_error: Exception | None = None
        for strategy in strategies:
            try:
                result = await strategy()
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    return path
                # Some clients return the path string instead of writing expected name
                if isinstance(result, str) and os.path.isfile(result) and os.path.getsize(result) > 0:
                    if os.path.abspath(result) != os.path.abspath(path):
                        with suppress(Exception):
                            os.replace(result, path)
                        if os.path.isfile(path) and os.path.getsize(path) > 0:
                            return path
                        return result
                raise RuntimeError("Download produced empty file")
            except Exception as ex:
                last_error = ex
                errors_log.append(f"{strategy.__name__}:{type(ex).__name__}")
                with suppress(Exception):
                    if os.path.exists(path) and os.path.getsize(path) <= 0:
                        os.remove(path)
                continue

        raise RuntimeError(
            "Telegram download failed ("
            + ", ".join(errors_log)
            + f") last={type(last_error).__name__ if last_error else 'n/a'}"
        )

    async def _download_with_progress(
        self,
        *,
        download_coro_factory,
        file_id: str,
        file_path: str,
        file_size: int,
        duration: int,
        is_video: bool,
        file_title: str,
        source_url: str | None,
        sent: types.Message,
    ) -> Media | None:
        msg_id = sent.id
        event = asyncio.Event()
        self.events[msg_id] = event
        self.last_edit[msg_id] = 0
        start_time = time.time()
        os.makedirs(os.path.dirname(file_path) or "downloads", exist_ok=True)

        if duration > config.DURATION_LIMIT:
            try:
                await sent.edit_text(
                    sent.lang["play_duration_limit"].format(config.DURATION_LIMIT // 60)
                )
            except Exception:
                pass
            return None

        max_download_size = config.DOWNLOAD_LIMIT_MB * 1024 * 1024
        if file_size and file_size > max_download_size:
            try:
                await sent.edit_text(sent.lang["dl_limit"])
            except Exception:
                pass
            return None

        async def progress(current, total):
            if event.is_set():
                return
            now = time.time()
            if now - self.last_edit[msg_id] < self.sleep:
                return
            self.last_edit[msg_id] = now
            total = total or file_size or current or 1
            percent = current * 100 / total
            speed = current / (now - start_time or 1e-6)
            eta = utils.format_eta(int((total - current) / speed)) if speed else "…"
            text = sent.lang["dl_progress"].format(
                utils.format_size(current),
                utils.format_size(total),
                percent,
                utils.format_size(speed),
                eta,
            )
            await utils.edit_text(
                sent, text, reply_markup=buttons.cancel_dl(sent.lang["cancel"])
            )

        try:
            if not (os.path.isfile(file_path) and os.path.getsize(file_path) > 0):
                if file_id in self.active:
                    await sent.edit_text(sent.lang["dl_active"])
                    return None

                self.active.append(file_id)
                attempts = max(1, self.download_retries + 1)
                last_error = None
                for attempt in range(1, attempts + 1):
                    task = asyncio.create_task(download_coro_factory(file_path, progress))
                    self.active_tasks[msg_id] = task
                    try:
                        await asyncio.wait_for(task, timeout=self.download_timeout)
                        if os.path.isfile(file_path) and os.path.getsize(file_path) > 0:
                            last_error = None
                            break
                        last_error = RuntimeError("Empty download result")
                    except asyncio.TimeoutError as ex:
                        last_error = ex
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                        if attempt < attempts:
                            with suppress(Exception):
                                await sent.edit_text(
                                    f"Download timed out. Retrying ({attempt}/{attempts})..."
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception as ex:
                        last_error = ex
                        logger.warning(
                            "Telegram download attempt %s/%s failed: %s",
                            attempt,
                            attempts,
                            ex,
                        )
                        if attempt >= attempts:
                            raise
                    finally:
                        self.active_tasks.pop(msg_id, None)

                if last_error:
                    raise last_error
                if file_id in self.active:
                    self.active.remove(file_id)
                # Leave status message as-is until Now Playing; do not show
                # "Download ready. Joining voice chat...".

            if not (os.path.isfile(file_path) and os.path.getsize(file_path) > 0):
                raise RuntimeError(f"Missing file after download: {file_path}")

            return Media(
                id=file_id,
                duration=time.strftime("%M:%S", time.gmtime(duration or 0)),
                duration_sec=duration or 0,
                file_path=file_path,
                local_path=file_path,
                message_id=sent.id,
                url=source_url,
                title=(file_title or "Telegram File")[:80],
                video=is_video,
                source="telegram_local",
            )
        except asyncio.CancelledError:
            with suppress(Exception):
                await sent.edit_text(sent.lang.get("dl_timeout", "Download cancelled."))
            return None
        except asyncio.TimeoutError:
            try:
                await sent.edit_text(sent.lang.get("dl_timeout", "Download timed out."))
            except Exception:
                pass
            return None
        except Exception as ex:
            logger.warning("Telegram download failed for msg_id=%s: %s", msg_id, ex)
            try:
                tpl = sent.lang.get(
                    "error_no_file",
                    "Download failed.\n\nIf the issue persists, report it to the support chat.",
                )
                # Prefer support-chat template when available
                if "{0}" in str(tpl):
                    await utils.edit_formatted(
                        sent,
                        tpl,
                        getattr(config, "SUPPORT_CHAT", ""),
                        template_key="error_no_file",
                    )
                else:
                    await sent.edit_text(
                        sent.lang.get("dl_failed", "Download failed: {err}").format(
                            err=type(ex).__name__
                        )
                    )
            except Exception:
                pass
            return None
        finally:
            self.events.pop(msg_id, None)
            self.last_edit.pop(msg_id, None)
            if file_id in self.active:
                self.active.remove(file_id)

    async def resolve(
        self, msg: types.Message, message_id: int, video: bool = False
    ) -> Media | None:
        """Resolve Telegram media metadata without downloading — for direct stream."""
        media_obj = self._media_obj(msg)
        if not media_obj:
            story = getattr(msg, "story", None)
            if story:
                return await self.resolve_story(story, message_id, video)
            return None

        file_id, file_ext, file_size, duration, is_video, file_title = self._describe_media(
            msg, media_obj
        )
        if (
            getattr(msg, "video", None)
            or getattr(msg, "video_note", None)
            or getattr(msg, "animation", None)
        ):
            is_video = True

        source_url = None
        with suppress(Exception):
            source_url = msg.link

        tg_file_id = getattr(media_obj, "file_id", None)
        safe_file_id = self._safe_path_token(str(tg_file_id)) if tg_file_id else None
        local_path = (
            f"downloads/tg_{safe_file_id}.{file_ext}" if safe_file_id else None
        )

        result = Media(
            id=str(tg_file_id or file_id),
            file_path=None,  # Not downloaded yet
            local_path=local_path,
            message_id=message_id,
            url=source_url or "",
            title=file_title or "Telegram Media",
            duration=self._duration_label(duration) if duration else "",
            duration_sec=duration,
            # Preserve the actual Telegram media type. A /vplay command cannot
            # manufacture a video track from an audio/voice/document source.
            video=is_video,
            source="telegram_remote",
            telegram_file_id=tg_file_id,
            telegram_file_size=file_size,
        )
        setattr(result, "telegram_message", msg)
        return result

    async def resolve_story(
        self, story, message_id: int, video: bool = False
    ) -> Media | None:
        """Resolve story metadata for direct stream."""
        from AnonX_3 import app as bot_app

        vid = getattr(story, "video", None)
        if not vid:
            return None
        fid = getattr(vid, "file_id", None)
        safe_fid = self._safe_path_token(str(fid)) if fid else None
        duration = getattr(vid, "duration", 0) or 0
        result = Media(
            id=str(fid or f"story_{message_id}"),
            file_path=None,
            local_path=f"downloads/tg_story_{safe_fid}.mp4" if safe_fid else None,
            message_id=message_id,
            url="",
            title="Telegram Story",
            duration=self._duration_label(duration),
            duration_sec=duration,
            video=True,
            source="telegram_remote",
            telegram_file_id=fid,
            telegram_file_size=int(getattr(vid, "file_size", 0) or 0),
        )
        setattr(result, "telegram_story", story)
        return result

    async def download(self, msg: types.Message, sent: types.Message) -> Media | None:
        media = self._media_obj(msg)
        if not media:
            # Message may wrap a story with video but no top-level media fields.
            story = getattr(msg, "story", None)
            if story:
                return await self.download_story(
                    story, sent, group_chat_id=getattr(msg.chat, "id", None)
                )
            return None

        file_id, file_ext, file_size, duration, is_video, file_title = self._describe_media(
            msg, media
        )
        # Forwarded messages often still expose audio/video/document file ids.
        if (
            getattr(msg, "video", None)
            or getattr(msg, "video_note", None)
            or getattr(msg, "animation", None)
        ):
            is_video = True
        file_path = os.path.join("downloads", f"{file_id}.{file_ext}")
        source_url = None
        with suppress(Exception):
            source_url = msg.link

        # Keep telegram file_id for later cache/re-download helpers.
        tg_file_id = getattr(media, "file_id", None)

        async def _factory(path, progress):
            return await self._download_bytes_to_path(msg, path, progress)

        result = await self._download_with_progress(
            download_coro_factory=_factory,
            file_id=file_id,
            file_path=file_path,
            file_size=file_size,
            duration=duration,
            is_video=is_video,
            file_title=file_title,
            source_url=source_url,
            sent=sent,
        )
        if result is not None and tg_file_id:
            with suppress(Exception):
                setattr(result, "telegram_file_id", tg_file_id)
        return result

    async def download_story(
        self,
        story,
        sent: types.Message,
        group_chat_id: int | None = None,
    ) -> Media | None:
        story = await self.hydrate_story(story, group_chat_id)
        if not story:
            try:
                await sent.edit_text(
                    sent.lang.get("play_not_found", "Story not found.").format(
                        getattr(config, "SUPPORT_CHAT", "")
                    )
                )
            except Exception:
                pass
            return None

        media = getattr(story, "video", None)
        if not media:
            # Photo-only stories cannot be streamed as audio/video tracks.
            try:
                await sent.edit_text(
                    sent.lang.get(
                        "play_not_found",
                        "Only video stories are playable.",
                    ).format(getattr(config, "SUPPORT_CHAT", ""))
                )
            except Exception:
                pass
            return None

        file_id, file_ext, file_size, duration, _is_video, file_title = self._describe_media(
            story, media, default_title="Telegram Story"
        )
        if not file_ext:
            file_ext = "mp4"
        file_path = f"downloads/{file_id}.{file_ext}"
        source_url = None
        with suppress(Exception):
            source_url = getattr(story, "link", None)

        async def _factory(path, progress):
            if hasattr(story, "download"):
                return await story.download(file_name=path, progress=progress)
            client = getattr(story, "_client", None)
            if client is None:
                clients = await self._assistant_clients(group_chat_id)
                client = clients[0] if clients else None
            if client is None:
                raise RuntimeError("No client available to download story")
            return await client.download_media(story, file_name=path, progress=progress)

        result = await self._download_with_progress(
            download_coro_factory=_factory,
            file_id=file_id,
            file_path=file_path,
            file_size=file_size,
            duration=duration,
            is_video=True,
            file_title=file_title,
            source_url=source_url,
            sent=sent,
        )
        if result is not None:
            result.source = "telegram_story"
        return result

    async def process_m3u8(self, url: str, msg_id: int, video: bool) -> Media:
        return Media(
            id=str(msg_id),
            file_path=url,
            message_id=msg_id,
            url=url,
            title="M3U8 Stream",
            video=video,
        )
