# Copyright (c) 2025 AnonX
# Licensed under the MIT License.
# This file is part of မဂ်လာပါ မြန်မာ 🇲🇲


import asyncio
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import yt_dlp

from AnonX_3 import config, logger
from AnonX_3.core import social_urls
from AnonX_3.core.downloader.singleflight import SingleFlight
from AnonX_3.core.ytdlp_runtime import create_youtube_dl
from AnonX_3.helpers import Media, Track, utils


_tiktok_download_flight = SingleFlight("tiktok-download")

_COOKIE_DIR = "AnonX_3/cookies"


def _get_cookie_file() -> str | None:
    """Return the first valid .txt cookie file from the shared cookie dir."""
    try:
        for fname in sorted(os.listdir(_COOKIE_DIR)):
            if fname.endswith(".txt"):
                path = os.path.join(_COOKIE_DIR, fname)
                if os.path.isfile(path) and os.path.getsize(path) > 64:
                    return path
    except OSError:
        pass
    return None


class TikTok:
    def __init__(self) -> None:
        # Any subdomain, including the bare host: canonicalisation in resolve()
        # rewrites whatever lands here into a shape yt-dlp actually accepts.
        self.regex = re.compile(
            r"^https?://(?:[\w-]+\.)?tiktok\.com/.*",
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
    def _has_stream(path: str | Path, stream: str) -> bool:
        """Verify the final artifact with ffprobe before ntgcalls sees it."""
        target = Path(path)
        if (
            not target.is_file()
            or target.stat().st_size < 8 * 1024
            or ".part" in target.name.lower()
            or target.name.lower().endswith(".ytdl")
        ):
            return False
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return False
        selector = "a:0" if stream == "audio" else "v:0"
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    selector,
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            return False

    @classmethod
    def _is_playable(cls, path: str | Path, *, video: bool) -> bool:
        if not cls._has_stream(path, "audio"):
            return False
        return not video or cls._has_stream(path, "video")

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
                if incomplete or not cls._is_playable(candidate, video=video):
                    candidate.unlink(missing_ok=True)
            except Exception:
                continue

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
                return f"tt_{safe}"
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        return f"tt_{digest}"

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

    async def resolve(self, url: str, message_id: int, video: bool = False) -> Track | None:
        # Story, photo and share links carry no shape yt-dlp knows. Rewrite
        # first so every downstream consumer of Track.url — download, direct
        # stream, cache key — sees the same canonical link.
        cookie = _get_cookie_file()
        url = await social_urls.canonical_tiktok(url, cookie_file=cookie)
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "socket_timeout": 15,
            "proxy": "",  # disable global proxy — TikTok needs direct connection
        }
        if cookie:
            opts["cookiefile"] = cookie
        # TikTok often blocks default yt-dlp UA; use a real browser UA
        opts.setdefault(
            "http_headers",
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
        )

        def _extract() -> dict | None:
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                try:
                    return ydl.extract_info(url, download=False)
                except Exception as ex:
                    logger.warning("TikTok resolve extract_info failed url=%s: %s", url[:80], ex)
                    return None

        info = await asyncio.to_thread(_extract)
        if not isinstance(info, dict):
            logger.warning("TikTok resolve returned non-dict for url=%s", url[:80])
            return None

        duration_sec = int(info.get("duration") or 0)
        title = (info.get("title") or "TikTok Video").strip()[:80]
        channel = (info.get("uploader") or info.get("channel") or "TikTok").strip()
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
                # Prefer a single muxed stream with both audio and video first,
                # then fall back to separate streams merged, then any playable mp4.
                "best[ext=mp4][acodec!=none][vcodec!=none]",
                "best[acodec!=none][vcodec!=none]",
                "bestvideo[ext=mp4][vcodec!=none]+bestaudio[acodec!=none]",
                "bestvideo[vcodec!=none]+bestaudio[acodec!=none]",
                "best[ext=mp4]/best",
            ]
            if video
            else [
                # Audio: require an audio codec; never select a video-only stream.
                "bestaudio[acodec!=none]/best[acodec!=none]",
                "best[acodec!=none]",
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
                cookie = _get_cookie_file()
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
                if not isinstance(stream_url, str) or not stream_url.startswith(("http://", "https://")):
                    continue
                # Validate the resolved stream actually has the requested media type.
                acodec = str(info.get("acodec") or "").lower()
                vcodec = str(info.get("vcodec") or "").lower()
                if not video and acodec in ("", "none"):
                    # Resolved audio stream has no audio codec — likely video-only.
                    continue
                if video and vcodec in ("", "none") and acodec in ("", "none"):
                    # Neither audio nor video codec available.
                    continue
                return stream_url
            return None

        remote = await asyncio.to_thread(_extract)
        return remote, local_path

    async def start_current_cache(self, chat_id: int, media: Media | Track) -> None:
        if self._shutting_down:
            return
        if not config.TIKTOK_DIRECT_CACHE_BG:
            return
        if not media or getattr(media, "source", None) != "tiktok_remote":
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
                            config.TIKTOK_DIRECT_CACHE_TIMEOUT_SEC,
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
        if await asyncio.to_thread(self._is_playable, filename, video=video):
            return filename
        for candidate in self._artifact_candidates(media_id):
            if await asyncio.to_thread(
                self._is_playable,
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
            # This cleanup runs only inside the media-ID singleflight owner.
            # A waiter cancellation cannot release ownership while yt-dlp's
            # worker thread is still writing its .part file.
            self._purge_bad_artifacts(media_id, video=video)
            max_rounds = 2
            for round_idx in range(1, max_rounds + 1):
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
                    cookie = _get_cookie_file()
                    if cookie:
                        ydl_opts["cookiefile"] = cookie
                    if progress_hook is not None:
                        ydl_opts["progress_hooks"] = [progress_hook]
                    if video:
                        ydl_opts["merge_output_format"] = "mp4"
                    else:
                        # TikTok frequently exposes a video container for
                        # "best". Normalize the verified audio stream to a
                        # call-safe AAC/M4A file before playback.
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
                                "TikTok download failed (round=%s format=%s): %s",
                                round_idx,
                                format_selector,
                                ex,
                            )
                            continue
                    for candidate in self._artifact_candidates(media_id):
                        if self._is_playable(candidate, video=video):
                            logger.info(
                                "TikTok artifact verified media_id=%s "
                                "video=%s path=%s",
                                media_id,
                                int(video),
                                candidate,
                            )
                            return str(candidate).replace("\\", "/")
                    self._purge_bad_artifacts(media_id, video=video)
                if round_idx < max_rounds:
                    logger.warning(
                        "TikTok download retrying media_id=%s next_round=%s",
                        media_id,
                        round_idx + 1,
                    )
            logger.warning(
                "TikTok download failed after all format fallbacks media_id=%s",
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
            # Audio and video use one media-ID flight because both yt-dlp
            # branches write the same output prefix. If a video caller joined
            # an audio owner, serialize one follow-up video flight after the
            # audio result is complete.
            result = await _tiktok_download_flight.do(
                f"tiktok:{media_id}",
                _run_download,
            )
            if not result or await asyncio.to_thread(
                self._is_playable,
                result,
                video=video,
            ):
                return result
            return await _tiktok_download_flight.do(
                f"tiktok:{media_id}",
                _run_download,
            )
        finally:
            if message_id and self._active_tasks.get(message_id) is waiter:
                self._active_tasks.pop(message_id, None)

    async def cancel(self, message_id: int) -> bool:
        task = self._active_tasks.pop(message_id, None)
        if task and not task.done():
            # Never cancel a shared CDN/singleflight owner on behalf of one
            # request. Ordinary handler/cache waiters are safe to cancel:
            # TikTok's inner download flight is shielded and keeps ownership
            # until the yt-dlp worker thread has stopped touching `.part`.
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
                    _tiktok_download_flight.shutdown(),
                    return_exceptions=True,
                )
                failures.extend(
                    result for result in results if isinstance(result, Exception)
                )
            finally:
                self._shutdown_complete = True

            if failures:
                raise ExceptionGroup("TikTok provider shutdown failed", failures)
