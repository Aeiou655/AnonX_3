# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import hashlib
import os
import re
from pathlib import Path

import yt_dlp

from AnonX_3 import config, logger
from AnonX_3.core import social_urls
from AnonX_3.core.downloader.singleflight import SingleFlight
from AnonX_3.core.downloader.validation import is_playable_media
from AnonX_3.core.ytdlp_runtime import create_youtube_dl
from AnonX_3.helpers import Media, Track, utils


_facebook_download_flight = SingleFlight("facebook-download")

_FB_COOKIE_DIR = "AnonX_3/cookies"


def _fb_get_cookie_file() -> str | None:
    """Return the first valid .txt cookie file from the shared cookie dir."""
    try:
        for fname in sorted(os.listdir(_FB_COOKIE_DIR)):
            if fname.endswith(".txt"):
                path = os.path.join(_FB_COOKIE_DIR, fname)
                if os.path.isfile(path) and os.path.getsize(path) > 64:
                    return path
    except OSError:
        pass
    return None


class Facebook:
    def __init__(self) -> None:
        # Any subdomain of the Facebook hosts, plus both shorteners.
        # canonical_facebook() in resolve() normalises whatever lands here.
        self.regex = re.compile(
            r"^https?://(?:[\w-]+\.)?"
            r"(?:facebook\.com|fb\.watch|fb\.com|fb\.me|fb\.gg)/.*",
            re.IGNORECASE,
        )
        self._active_tasks: dict[int, asyncio.Task] = {}
        self.current_cache: dict[int, tuple[Media | Track, asyncio.Task]] = {}
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_complete = False
        try:
            from AnonX_3.core.resource_manager import resource_manager
            self._download_semaphore = resource_manager.download_semaphore()
        except Exception:
            self._download_semaphore = asyncio.Semaphore(2)

    def valid(self, url: str) -> bool:
        return bool(url and self.regex.match(url.strip()))

    @staticmethod
    def _duration_label(seconds: int) -> str:
        total = max(0, int(seconds or 0))
        mm, ss = divmod(total, 60)
        hh, mm = divmod(mm, 60)
        if hh:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return f"{mm:02d}:{ss:02d}"

    @staticmethod
    def _build_media_id(info: dict, url: str) -> str:
        raw_id = str(info.get("id") or "").strip()
        if raw_id:
            safe = re.sub(r"[^A-Za-z0-9_-]+", "", raw_id)
            if safe:
                return f"fb_{safe}"
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        return f"fb_{digest}"

    @staticmethod
    def _pick_thumbnail(info: dict) -> str | None:
        thumb = info.get("thumbnail")
        if isinstance(thumb, str) and thumb.startswith(("http://", "https://")):
            return thumb
        thumbs = info.get("thumbnails") or []
        if thumbs and isinstance(thumbs, list):
            candidate = thumbs[-1].get("url") if isinstance(thumbs[-1], dict) else None
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
        return None

    @staticmethod
    def _artifact_candidates(media_id: str) -> list[Path]:
        return sorted(
            (
                path
                for path in Path("downloads").glob(f"{media_id}.*")
                if path.is_file()
                and ".part" not in path.name.lower()
                and not path.name.lower().endswith(".ytdl")
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    @classmethod
    def _purge_bad_artifacts(cls, media_id: str, *, video: bool) -> None:
        root = Path("downloads").resolve()
        for candidate in root.glob(f"{media_id}.*"):
            try:
                resolved = candidate.resolve()
                if resolved.parent != root or not resolved.is_file():
                    continue
                incomplete = (
                    ".part" in candidate.name.lower()
                    or candidate.name.lower().endswith(".ytdl")
                )
                if incomplete or not is_playable_media(candidate, video=video):
                    candidate.unlink(missing_ok=True)
            except Exception:
                continue

    async def resolve(self, url: str, message_id: int, video: bool = False) -> Track | None:
        # /share/v/<token>/ and fb.watch/<code>/ match no extractor. Resolve
        # them to a canonical watch/reel URL before yt-dlp ever sees the link,
        # so download, direct stream and the cache key all agree. The same
        # cookie jar yt-dlp uses turns a login interstitial back into a
        # redirect.
        cookie = _fb_get_cookie_file()
        url = await social_urls.canonical_facebook(url, cookie_file=cookie)
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "socket_timeout": 15,
            "proxy": "",  # disable global proxy — Facebook needs direct connection
        }
        if cookie:
            opts["cookiefile"] = cookie
        opts.setdefault(
            "http_headers",
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
        )

        def _extract() -> dict | None:
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                try:
                    return ydl.extract_info(url, download=False)
                except Exception as ex:
                    logger.warning("Facebook resolve extract_info failed url=%s: %s", url[:80], ex)
                    return None

        info = await asyncio.to_thread(_extract)
        if not isinstance(info, dict):
            logger.warning("Facebook resolve returned non-dict for url=%s", url[:80])
            return None

        duration_sec = int(info.get("duration") or 0)
        title = (info.get("title") or "Facebook Video").strip()[:80]
        channel = (info.get("uploader") or info.get("channel") or "Facebook").strip()
        media_id = self._build_media_id(info, url)
        thumbnail = self._pick_thumbnail(info)

        return Track(
            id=media_id,
            channel_name=channel,
            duration=self._duration_label(duration_sec),
            duration_sec=duration_sec,
            message_id=message_id,
            title=title,
            thumbnail=thumbnail,
            url=url,
            view_count=str(info.get("view_count") or "N/A"),
            video=video,
        )

    async def resolve_direct_stream(
        self,
        *,
        url: str,
        media_id: str,
        video: bool = False,
    ) -> tuple[str | None, str]:
        ext = "mp4" if video else "m4a"
        local_path = f"downloads/{media_id}.{ext}"
        format_candidates = (
            [
                "best[ext=mp4][acodec!=none][vcodec!=none]/best[ext=mp4]/best",
                "best",
            ]
            if video
            else [
                "bestaudio/best",
                "best",
            ]
        )

        def _extract() -> str | None:
            for format_selector in format_candidates:
                opts: dict = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "nocheckcertificate": True,
                    "format": format_selector,
                    "socket_timeout": 10,
                    "proxy": "",
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    },
                }
                cookie = _fb_get_cookie_file()
                if cookie:
                    opts["cookiefile"] = cookie
                with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                    try:
                        info = ydl.extract_info(url, download=False)
                    except Exception:
                        continue
                if not isinstance(info, dict):
                    continue
                stream_url = info.get("url")
                if isinstance(stream_url, str) and stream_url.startswith(("http://", "https://")):
                    return stream_url
            return None

        remote = await asyncio.to_thread(_extract)
        return remote, local_path

    async def start_current_cache(self, chat_id: int, media: Media | Track) -> None:
        if self._shutting_down:
            return
        if not config.FACEBOOK_DIRECT_CACHE_BG:
            return
        if not media or getattr(media, "source", None) != "facebook_remote":
            return
        existing = self.current_cache.get(chat_id)
        if existing and existing[0] is media and not existing[1].done():
            return

        async def _runner(target: Media | Track) -> None:
            try:
                await self.download(
                    url=getattr(target, "url", ""),
                    media_id=str(getattr(target, "id", "")),
                    video=bool(getattr(target, "video", False)),
                    progress_media=target,
                )
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
        media: Media | Track,
        ping: float | None = None,
        message_id: int | None = None,
    ) -> str | None:
        current = self.current_cache.get(chat_id)
        if current and current[0] is media:
            task = current[1]
            if not task.done():
                try:
                    await asyncio.wait_for(
                        task,
                        timeout=config.resolve_cache_timeout(
                            config.FACEBOOK_DIRECT_CACHE_TIMEOUT_SEC,
                            ping=ping,
                        ),
                    )
                except Exception:
                    pass
            self.current_cache.pop(chat_id, None)

        return await self.download(
            url=getattr(media, "url", ""),
            media_id=str(getattr(media, "id", "")),
            video=bool(getattr(media, "video", False)),
            message_id=message_id,
            progress_media=media,
        )

    async def download(
        self,
        *,
        url: str,
        media_id: str,
        video: bool = False,
        message_id: int | None = None,
        progress_media: Media | Track | None = None,
    ) -> str | None:
        ext = "mp4" if video else "m4a"
        filename = f"downloads/{media_id}.{ext}"
        if await asyncio.to_thread(is_playable_media, filename, video=video):
            return filename
        for candidate in self._artifact_candidates(media_id):
            if await asyncio.to_thread(
                is_playable_media,
                candidate,
                video=video,
            ):
                return str(candidate).replace("\\", "/")

        format_candidates = (
            [
                "best[ext=mp4][acodec!=none][vcodec!=none]",
                "best[acodec!=none][vcodec!=none]",
                "bestvideo[ext=mp4][vcodec!=none]+bestaudio[acodec!=none]",
                "bestvideo[vcodec!=none]+bestaudio[acodec!=none]",
                "best[ext=mp4]/best",
            ]
            if video
            else [
                "bestaudio[acodec!=none]/best[acodec!=none]",
                "best[acodec!=none]",
            ]
        )
        progress_hook = utils.make_download_progress_hook(progress_media)

        def _download_owned() -> str | None:
            self._purge_bad_artifacts(media_id, video=video)
            for format_selector in format_candidates:
                ydl_opts: dict = {
                    "outtmpl": f"downloads/{media_id}.%(ext)s",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "overwrites": True,
                    "continuedl": False,
                    "nocheckcertificate": True,
                    "retries": 3,
                    "fragment_retries": 3,
                    "format": format_selector,
                    "socket_timeout": 15,
                    "proxy": "",
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    },
                }
                cookie = _fb_get_cookie_file()
                if cookie:
                    ydl_opts["cookiefile"] = cookie
                if progress_hook is not None:
                    ydl_opts["progress_hooks"] = [progress_hook]
                if video:
                    ydl_opts["merge_output_format"] = "mp4"
                else:
                    ydl_opts["postprocessors"] = [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "m4a",
                            "preferredquality": "192",
                        }
                    ]
                with create_youtube_dl(ydl_opts, yt_dlp.YoutubeDL) as ydl:
                    try:
                        ydl.download([url])
                    except Exception as ex:
                        logger.warning(
                            "Facebook download failed (format=%s): %s",
                            format_selector,
                            ex,
                        )
                        continue
                for candidate in self._artifact_candidates(media_id):
                    if is_playable_media(candidate, video=video):
                        logger.info(
                            "Facebook artifact verified media_id=%s video=%s path=%s",
                            media_id,
                            int(video),
                            candidate,
                        )
                        return str(candidate).replace("\\", "/")
                self._purge_bad_artifacts(media_id, video=video)
            logger.warning(
                "Facebook download failed after all format fallbacks media_id=%s",
                media_id,
            )
            return None

        async def _run_download() -> str | None:
            async with self._download_semaphore:
                return await asyncio.to_thread(_download_owned)

        waiter = asyncio.current_task()
        if message_id and waiter:
            self._active_tasks[message_id] = waiter
        try:
            result = await _facebook_download_flight.do(
                f"facebook:{media_id}",
                _run_download,
            )
            if not result or await asyncio.to_thread(
                is_playable_media,
                result,
                video=video,
            ):
                return result
            return await _facebook_download_flight.do(
                f"facebook:{media_id}",
                _run_download,
            )
        finally:
            if message_id and self._active_tasks.get(message_id) is waiter:
                self._active_tasks.pop(message_id, None)

    async def cancel(self, message_id: int) -> bool:
        task = self._active_tasks.pop(message_id, None)
        if task and not task.done():
            if not task.get_name().startswith("sf:"):
                task.cancel()
            return True
        return False

    async def shutdown(self) -> None:
        """Cancel and await every cache/download task owned by this provider."""

        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutting_down = True
            current = asyncio.current_task()
            owned = [
                *(entry[1] for entry in self.current_cache.values()),
                *self._active_tasks.values(),
            ]
            self.current_cache.clear()
            self._active_tasks.clear()
            tasks = list(dict.fromkeys(task for task in owned if task is not current))
            for task in tasks:
                if not task.done():
                    task.cancel()

            failures: list[Exception] = []
            try:
                results = await asyncio.gather(
                    *tasks,
                    _facebook_download_flight.shutdown(),
                    return_exceptions=True,
                )
                failures.extend(
                    result for result in results if isinstance(result, Exception)
                )
            finally:
                self._shutdown_complete = True

            if failures:
                raise ExceptionGroup("Facebook provider shutdown failed", failures)
