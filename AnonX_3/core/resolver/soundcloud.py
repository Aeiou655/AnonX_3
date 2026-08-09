# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""SoundCloud search/resolve via yt-dlp (stream-oriented, no account required)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import yt_dlp

from AnonX_3 import config, logger
from AnonX_3.core.resource_manager import resource_manager
from AnonX_3.core.ytdlp_runtime import create_youtube_dl
from AnonX_3.core.resolver.error_classifier import classify_error
from AnonX_3.helpers import Track


_SC_URL_RE = re.compile(
    r"https?://(?:www\.)?soundcloud\.com/[^\s]+", re.I
)


class SoundCloudTransportError(RuntimeError):
    """Raised when SoundCloud cannot be reached within the bounded retry path."""


class _YTDLPLogCapture:
    """Capture yt-dlp failures so provider noise is logged once and sanitized."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, _message: str) -> None:
        pass

    def info(self, _message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.messages.append(str(message or ""))

    def error(self, message: str) -> None:
        self.messages.append(str(message or ""))

    @property
    def last_message(self) -> str:
        return self.messages[-1] if self.messages else ""


def is_soundcloud_url(value: str | None) -> bool:
    return bool(value and _SC_URL_RE.search(value))


def _unplayable_reason(value: dict | BaseException | str | None) -> str | None:
    if isinstance(value, dict):
        if value.get("_has_drm") or value.get("has_drm"):
            return "drm"
        formats = value.get("formats")
        if isinstance(formats, list) and formats:
            usable = [
                item
                for item in formats
                if isinstance(item, dict)
                and item.get("url")
                and not item.get("has_drm")
            ]
            if not usable and any(
                isinstance(item, dict) and item.get("has_drm")
                for item in formats
            ):
                return "drm"
        availability = str(value.get("availability") or "").casefold()
        if availability in {
            "private",
            "premium_only",
            "subscriber_only",
            "needs_auth",
            "unavailable",
        }:
            return availability
        return None

    message = str(value or "").casefold()
    markers = {
        "drm": (
            "drm protected",
            "has drm",
        ),
        "private": (
            "private track",
            "this track is private",
        ),
        "unavailable": (
            "unplayable",
            "not available in your country",
            "not available in your region",
            "not permitted to stream",
            "track is not available",
            "has been removed",
        ),
    }
    for reason, values in markers.items():
        if any(marker in message for marker in values):
            return reason
    return None


def _duration_str(seconds: float | int | None) -> str:
    try:
        sec = int(float(seconds or 0))
    except Exception:
        sec = 0
    if sec <= 0:
        return "00:00"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _entry_to_track(entry: dict, *, message_id: int = 0, video: bool = False) -> Track | None:
    if not isinstance(entry, dict):
        return None
    # Skip playlists / non-streamable
    if entry.get("_type") == "playlist" or _unplayable_reason(entry):
        return None
    url = (entry.get("webpage_url") or entry.get("url") or "").strip()
    sc_id = str(entry.get("id") or "").strip()
    title = (entry.get("title") or "").strip()
    if not title and not url:
        return None
    if not sc_id:
        sc_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", url)[-40:] or "sc"
    duration = entry.get("duration")
    try:
        duration_sec = int(float(duration or 0))
    except Exception:
        duration_sec = 0
    artist = (
        (entry.get("uploader") or entry.get("artist") or entry.get("creator") or "")
        .strip()
    )
    thumb = ""
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        t = thumbs[-1] or {}
        if isinstance(t, dict):
            thumb = (t.get("url") or "").split("?")[0]
    if not thumb:
        thumb = (entry.get("thumbnail") or "").split("?")[0]

    track = Track(
        id=f"sc_{sc_id}"[:64],
        channel_name=artist or None,
        duration=_duration_str(duration_sec),
        duration_sec=int(duration_sec or 0),
        title=(title or "SoundCloud track")[:120],
        thumbnail=thumb or None,
        url=url or None,
        message_id=message_id,
        video=bool(video),
        view_count=str(entry.get("view_count") or entry.get("playback_count") or "")
        or None,
        source="soundcloud",
    )
    return track


class SoundCloudResolver:
    """Search and resolve SoundCloud tracks with yt-dlp."""

    def __init__(self) -> None:
        self._search_limit = max(
            1, int(getattr(config, "FALLBACK_SEARCH_LIMIT", 8) or 8)
        )

    def enabled(self) -> bool:
        return bool(getattr(config, "FALLBACK_SOUNDCLOUD", True))

    @staticmethod
    def _is_proxy_transport_failure(exc: BaseException | str) -> bool:
        messages: list[str] = []
        if isinstance(exc, BaseException):
            current: BaseException | None = exc
            seen: set[int] = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                messages.append(str(current))
                current = current.__cause__ or current.__context__
        else:
            messages.append(str(exc or ""))
        message = " | ".join(messages).casefold()
        return any(
            marker in message
            for marker in (
                "unable to connect to proxy",
                "cannot connect to proxy",
                "proxyerror",
                "proxy error",
                "proxy connect",
                "proxy authentication",
                "407 proxy",
                "tunnel connection failed",
                "proxy tunnel",
                "connect tunnel",
                "socks proxy",
                "socks5",
                "bad gateway",
                "connection refused",
                "read timed out",
                "httpsconnectionpool",
                "transporterror",
            )
        )

    @staticmethod
    def _metadata_attempt_timeout(use_proxy: str | None) -> float:
        setting = (
            "SOUNDCLOUD_PROXY_TIMEOUT_SEC"
            if use_proxy
            else "SOUNDCLOUD_DIRECT_TIMEOUT_SEC"
        )
        default = 8.0 if use_proxy else 10.0
        try:
            return max(1.0, float(getattr(config, setting, default) or default))
        except Exception:
            return default

    @classmethod
    async def _run_metadata_attempt(
        cls,
        worker,
        use_proxy: str | None,
    ):
        # yt-dlp's socket timeout owns ordinary network cancellation. The
        # extra second is only a hard guard for extractor code outside a socket
        # read; retries remain disabled so a timed-out worker does not overlap
        # the following direct attempt under normal transport failures.
        timeout = cls._metadata_attempt_timeout(use_proxy) + 1.0
        return await asyncio.wait_for(
            asyncio.to_thread(worker, use_proxy),
            timeout=timeout,
        )

    @classmethod
    def _metadata_options(
        cls,
        use_proxy: str | None,
        *,
        flat_search: bool = False,
    ) -> tuple[dict, _YTDLPLogCapture]:
        attempt_timeout = cls._metadata_attempt_timeout(use_proxy)
        captured = _YTDLPLogCapture()
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": not flat_search,
            "geo_bypass": True,
            "socket_timeout": attempt_timeout,
            "retries": 0,
            "fragment_retries": 0,
            "extractor_retries": 0,
            "ignoreerrors": True,
            "logger": captured,
        }
        if flat_search:
            # Search must not resolve every result. A single DRM/private item
            # must never abort or delay the complete candidate list.
            opts["extract_flat"] = "in_playlist"
        cls._set_proxy(opts, use_proxy)
        return opts, captured

    @staticmethod
    def _set_proxy(opts: dict, proxy: str | None) -> None:
        # An omitted yt-dlp proxy option can still inherit HTTP(S)_PROXY.
        # An empty option explicitly makes the fallback attempt direct.
        opts["proxy"] = proxy or ""

    @staticmethod
    def _proxy_attempts(proxy: str | None) -> tuple[str | None, ...]:
        return (proxy, None) if proxy else (None,)

    async def search(
        self,
        query: str,
        *,
        message_id: int = 0,
        video: bool = False,
        limit: int | None = None,
    ) -> list[Track]:
        if not self.enabled():
            return []
        if video:
            # SoundCloud is audio-only; it cannot satisfy /vplay.
            return []
        q = (query or "").strip()
        if not q:
            return []
        # Direct URL
        if is_soundcloud_url(q):
            track = await self.resolve_url(q, message_id=message_id, video=video)
            return [track] if track else []

        n = max(1, min(int(limit or self._search_limit), 15))
        search_url = f"scsearch{n}:{q}"
        try:
            from config import get_youtube_proxy

            proxy = (get_youtube_proxy() or "").strip() or None
        except Exception:
            proxy = (getattr(config, "YOUTUBE_PROXY", "") or "").strip() or None

        def _run(use_proxy: str | None) -> tuple[list[dict], str]:
            opts, captured = self._metadata_options(
                use_proxy,
                flat_search=True,
            )
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                info = ydl.extract_info(search_url, download=False)
            if not isinstance(info, dict):
                return [], captured.last_message
            if info.get("_type") == "playlist":
                return list(info.get("entries") or []), captured.last_message
            return [info], captured.last_message

        entries: list[dict] = []
        last_ex: Exception | None = None
        # Retry direct only when the configured proxy transport itself is broken.
        for use_proxy in self._proxy_attempts(proxy):
            try:
                resource_manager.note_extract(+1)
                async with resource_manager.extract_semaphore():
                    entries, captured_error = await self._run_metadata_attempt(
                        _run,
                        use_proxy,
                    )
                if captured_error and not entries:
                    raise RuntimeError(captured_error)
                # A completed direct attempt supersedes an earlier proxy
                # transport exception, even when the search legitimately
                # returns no matches.
                last_ex = None
                break
            except asyncio.TimeoutError:
                last_ex = TimeoutError("SoundCloud metadata attempt timed out")
                if use_proxy:
                    logger.info(
                        "soundcloud proxy timed out; retrying direct query=%r",
                        q[:80],
                    )
                    continue
                break
            except Exception as ex:
                last_ex = ex
                if use_proxy and self._is_proxy_transport_failure(ex):
                    logger.info(
                        "soundcloud proxy transport failed; retrying direct query=%r",
                        q[:80],
                    )
                    continue
                break
            finally:
                resource_manager.note_extract(-1)

        if not entries and last_ex is not None:
            classified = classify_error(last_ex)
            logger.warning(
                "soundcloud search unavailable query=%r class=%s",
                q[:80],
                classified.name,
            )
            raise SoundCloudTransportError(
                f"SoundCloud search unavailable ({classified.name})"
            ) from last_ex

        tracks: list[Track] = []
        for entry in entries:
            if not entry:
                continue
            tr = _entry_to_track(entry, message_id=message_id, video=video)
            if tr and tr.url:
                tracks.append(tr)
        logger.info(
            "soundcloud search query=%r count=%s",
            q[:80],
            len(tracks),
        )
        return tracks

    async def resolve_url_status(
        self,
        url: str,
        *,
        message_id: int = 0,
        video: bool = False,
    ) -> tuple[Track | None, str]:
        if not self.enabled():
            return None, "disabled"
        if video:
            return None, "video_unsupported"
        clean = (url or "").strip()
        if not clean:
            return None, "empty"

        try:
            from config import get_youtube_proxy

            proxy = (get_youtube_proxy() or "").strip() or None
        except Exception:
            proxy = (getattr(config, "YOUTUBE_PROXY", "") or "").strip() or None

        def _run(use_proxy: str | None) -> tuple[dict | None, str]:
            opts, captured = self._metadata_options(use_proxy)
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                info = ydl.extract_info(clean, download=False)
            return (
                info if isinstance(info, dict) else None,
                captured.last_message,
            )

        info: dict | None = None
        last_ex: Exception | None = None
        for use_proxy in self._proxy_attempts(proxy):
            try:
                resource_manager.note_extract(+1)
                async with resource_manager.extract_semaphore():
                    info, captured_error = await self._run_metadata_attempt(
                        _run,
                        use_proxy,
                    )
                unplayable = _unplayable_reason(info or captured_error)
                if unplayable:
                    logger.info(
                        "soundcloud resolve skipped unplayable reason=%s url=%s",
                        unplayable,
                        clean[:80],
                    )
                    return None, "unplayable"
                if captured_error and not info:
                    raise RuntimeError(captured_error)
                # Do not report the previous proxy failure as the outcome of a
                # successful direct attempt that simply found no media.
                last_ex = None
                if info:
                    break
                if not use_proxy:
                    break
            except asyncio.TimeoutError:
                last_ex = TimeoutError("SoundCloud metadata attempt timed out")
                if use_proxy:
                    logger.info(
                        "soundcloud proxy timed out; retrying direct url=%s",
                        clean[:80],
                    )
                    continue
                break
            except Exception as ex:
                last_ex = ex
                if use_proxy and self._is_proxy_transport_failure(ex):
                    logger.info(
                        "soundcloud proxy transport failed; retrying direct url=%s",
                        clean[:80],
                    )
                    continue
                break
            finally:
                resource_manager.note_extract(-1)

        if not info:
            if last_ex is not None:
                logger.warning(
                    "soundcloud resolve unavailable url=%s class=%s",
                    clean[:80],
                    classify_error(last_ex).name,
                )
                return None, "transport"
            return None, "empty"
        if info.get("_type") == "playlist":
            for entry in info.get("entries") or []:
                tr = _entry_to_track(entry, message_id=message_id, video=video)
                if tr:
                    return tr, "ok"
            return None, "unplayable"
        track = _entry_to_track(info, message_id=message_id, video=video)
        return (track, "ok") if track else (None, "unplayable")

    async def resolve_url(
        self,
        url: str,
        *,
        message_id: int = 0,
        video: bool = False,
    ) -> Track | None:
        track, _reason = await self.resolve_url_status(
            url,
            message_id=message_id,
            video=video,
        )
        return track

    async def download(
        self,
        url: str,
        *,
        media_id: str | None = None,
        video: bool = False,
    ) -> str | None:
        """Download streamable media for playback. Prefer audio-only remux-free."""
        if not self.enabled() or not url or video:
            return None
        mid = (media_id or "sc").replace("/", "_")[:64]
        outtmpl = f"downloads/{mid}.%(ext)s"
        try:
            from config import get_youtube_proxy

            proxy = (get_youtube_proxy() or "").strip() or None
        except Exception:
            proxy = (getattr(config, "YOUTUBE_PROXY", "") or "").strip() or None

        def _run(use_proxy: str | None) -> str | None:
            captured = _YTDLPLogCapture()
            opts = {
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "geo_bypass": True,
                "overwrites": False,
                "nocheckcertificate": True,
                "format": "bestaudio/best" if not video else "best",
                "socket_timeout": 30,
                "retries": 1,
                "fragment_retries": 1,
                "logger": captured,
            }
            self._set_proxy(opts, use_proxy)
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                info = ydl.extract_info(url, download=True)
            if not isinstance(info, dict):
                return None
            # requested_downloads or filepath
            req = info.get("requested_downloads") or []
            if req and isinstance(req[0], dict) and req[0].get("filepath"):
                return str(req[0]["filepath"]).replace("\\", "/")
            fp = info.get("filepath") or info.get("_filename")
            if fp:
                return str(fp).replace("\\", "/")
            # glob fallback
            from pathlib import Path

            cands = sorted(
                Path("downloads").glob(f"{mid}.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for p in cands:
                if p.suffix.lower() not in {".part", ".ytdl", ".tmp"}:
                    return str(p).replace("\\", "/")
            return None

        last_ex: Exception | None = None
        resource_manager.note_download(+1)
        try:
            for use_proxy in self._proxy_attempts(proxy):
                try:
                    async with resource_manager.download_semaphore():
                        path = await asyncio.to_thread(_run, use_proxy)
                    if path:
                        return path
                    if use_proxy:
                        logger.info(
                            "soundcloud proxy returned no file; retrying direct url=%s",
                            url[:80],
                        )
                        continue
                    return None
                except Exception as ex:
                    last_ex = ex
                    if use_proxy and self._is_proxy_transport_failure(ex):
                        logger.info(
                            "soundcloud download proxy failed; retrying direct url=%s",
                            url[:80],
                        )
                        continue
                    break
            if last_ex is not None:
                logger.warning(
                    "soundcloud download failed url=%s: %s", url[:80], last_ex
                )
            return None
        finally:
            resource_manager.note_download(-1)

    async def resolve_direct_stream(
        self,
        url: str,
    ) -> tuple[str | None, str | None]:
        """Return (stream_url, None) for progressive playback when possible."""
        if not url:
            return None, None
        try:
            from config import get_youtube_proxy

            proxy = (get_youtube_proxy() or "").strip() or None
        except Exception:
            proxy = (getattr(config, "YOUTUBE_PROXY", "") or "").strip() or None

        def _run(use_proxy: str | None) -> tuple[str | None, str]:
            opts, captured = self._metadata_options(use_proxy)
            opts["format"] = "bestaudio/best"
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                return None, captured.last_message
            if _unplayable_reason(info):
                return None, "unplayable"
            u = info.get("url")
            if isinstance(u, str) and u.startswith("http"):
                return u, captured.last_message
            for fmt in info.get("formats") or []:
                if not isinstance(fmt, dict):
                    continue
                fu = fmt.get("url")
                if isinstance(fu, str) and fu.startswith("http"):
                    if fmt.get("acodec") not in (None, "none"):
                        return fu, captured.last_message
            return None, captured.last_message

        last_ex: Exception | None = None
        resource_manager.note_extract(+1)
        try:
            for use_proxy in self._proxy_attempts(proxy):
                try:
                    async with resource_manager.extract_semaphore():
                        remote, captured_error = await self._run_metadata_attempt(
                            _run,
                            use_proxy,
                        )
                    unplayable = _unplayable_reason(captured_error)
                    if unplayable:
                        logger.info(
                            "soundcloud direct stream skipped unplayable reason=%s",
                            unplayable,
                        )
                        return None, None
                    if captured_error and not remote:
                        raise RuntimeError(captured_error)
                    if remote:
                        return remote, None
                    if use_proxy:
                        logger.info(
                            "soundcloud proxy returned no stream; retrying direct"
                        )
                        continue
                    return None, None
                except asyncio.TimeoutError:
                    last_ex = TimeoutError("SoundCloud stream attempt timed out")
                    if use_proxy:
                        logger.info(
                            "soundcloud stream proxy timed out; retrying direct"
                        )
                        continue
                    break
                except Exception as ex:
                    last_ex = ex
                    if use_proxy and self._is_proxy_transport_failure(ex):
                        logger.info(
                            "soundcloud stream proxy failed; retrying direct"
                        )
                        continue
                    break
            if last_ex is not None:
                logger.warning("soundcloud direct stream failed: %s", last_ex)
            return None, None
        finally:
            resource_manager.note_extract(-1)


soundcloud = SoundCloudResolver()
