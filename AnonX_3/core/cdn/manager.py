# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""CDN download manager: READY hit / await inflight / download+publish."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from AnonX_3 import config, logger, tiktok, yt
from AnonX_3.core.cache.keys import detect_source, legacy_asset_key, make_cache_key
from AnonX_3.core.cache.states import CacheState
from AnonX_3.core.cdn.publisher import atomic_publish, safe_filename
from AnonX_3.core.cdn.store import MediaStore
from AnonX_3.core.downloader.singleflight import download_flight
from AnonX_3.core.downloader.validation import is_valid_media_file, validate_ready_file
from AnonX_3.helpers import Media, Track


@dataclass
class CdnAsset:
    key: str
    local_path: str
    play_url: str | None
    filename: str
    status: str = "ready"


class CdnManager:
    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._store: MediaStore | None = None
        self._root: Path | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(config, "CDN_ENABLED", False))

    def media_root(self) -> Path:
        if self._root is not None:
            return self._root
        root = Path(getattr(config, "CDN_MEDIA_ROOT", "media") or "media")
        if not root.is_absolute():
            root = Path.cwd().resolve() / root
        self._root = root
        return root

    def tmp_dir(self) -> Path:
        p = self.media_root() / "tmp"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def ready_dir(self) -> Path:
        p = self.media_root() / "ready"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def store(self) -> MediaStore:
        if self._store is None:
            self._store = MediaStore(self.media_root() / "media.db")
        return self._store

    def asset_key(
        self,
        media_id: str,
        video: bool = False,
        quality_tier: str | None = None,
        *,
        source: str = "youtube",
        quality: str | int | None = None,
    ) -> str:
        """Canonical cache key (source:youtube:id:audio:best)."""
        return make_cache_key(
            source=source or "youtube",
            source_id=str(media_id or ""),
            video=video,
            quality=quality,
            quality_tier=quality_tier,
        )

    def legacy_key(
        self,
        media_id: str,
        video: bool = False,
        quality_tier: str | None = None,
    ) -> str:
        return legacy_asset_key(media_id, video=video, quality_tier=quality_tier)

    def public_base(self) -> str:
        base = (getattr(config, "CDN_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        if base:
            return base
        origin = (getattr(config, "CDN_ORIGIN_PUBLIC_BASE", "") or "").strip().rstrip("/")
        return origin

    def build_cdn_url(self, filename: str) -> str | None:
        base = self.public_base()
        if not base:
            return None
        prefix = getattr(config, "CDN_URL_PREFIX", "/media") or "/media"
        if not prefix.startswith("/"):
            prefix = f"/{prefix}"
        prefix = prefix.rstrip("/")
        name = Path(filename).name
        return f"{base}{prefix}/{name}"

    def resolve_play_path(self, local_path: str, filename: str) -> str:
        """
        hybrid/auto (default): prefer local ready/ path for stable PyTgCalls play.
        cdn: prefer public/origin HTTP URL, fall back to local.
        local: always local ready/ path.
        """
        mode = (getattr(config, "CDN_PLAY_MODE", "hybrid") or "hybrid").lower()
        url = self.build_cdn_url(filename)
        if mode == "local":
            return local_path
        if mode == "cdn":
            return url or local_path
        # hybrid / auto / dynamic — local first (stable), URL optional for remote
        if local_path and Path(local_path).is_file():
            return local_path
        return url or local_path

    def _find_ready_file(
        self,
        media_id: str,
        video: bool,
        quality_tier: str | None,
    ) -> Path | None:
        ready = self.ready_dir()
        # Exact preferred names first.
        candidates: list[str] = []
        if video and quality_tier:
            candidates.append(safe_filename(media_id, "mp4", quality_tier, video=True))
            candidates.append(safe_filename(media_id, "webm", quality_tier, video=True))
        if video:
            candidates.append(safe_filename(media_id, "mp4", video=True))
            candidates.append(safe_filename(media_id, "webm", video=True))
            candidates.append(safe_filename(media_id, "mkv", video=True))
        else:
            candidates.append(safe_filename(media_id, "webm", video=False))
            candidates.append(safe_filename(media_id, "m4a", video=False))
            candidates.append(safe_filename(media_id, "mp3", video=False))
            candidates.append(safe_filename(media_id, "opus", video=False))

        for name in candidates:
            path = ready / name
            if path.is_file() and path.stat().st_size > 0:
                return path

        # Fuzzy: any ready file starting with media_id.
        prefix = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in str(media_id or "")
        )
        if prefix:
            for path in ready.glob(f"{prefix}*"):
                if path.is_file() and path.stat().st_size > 0 and not path.name.endswith(
                    (".part", ".publishing")
                ):
                    return path
        return None

    def _ready_asset(
        self,
        key: str,
        ready_path: Path,
        *,
        touch: bool = True,
    ) -> CdnAsset | None:
        ok, reason = validate_ready_file(
            ready_path,
            video=ready_path.suffix.lower() in {".mp4", ".mkv", ".webm"}
            and "audio" not in ready_path.name.lower(),
        )
        if not ok:
            logger.info("CDN READY rejected key=%s path=%s reason=%s", key, ready_path, reason)
            return None
        if touch:
            try:
                self.store().touch(key)
            except Exception:
                pass
        filename = ready_path.name
        local = str(ready_path)
        play = self.resolve_play_path(local, filename)
        logger.info("CDN READY hit key=%s file=%s play=%s", key, filename, play)
        return CdnAsset(
            key=key,
            local_path=local,
            play_url=play if str(play).startswith("http") else None,
            filename=filename,
            status=CacheState.READY.value,
        )

    async def ensure_ready(
        self,
        media: Media | Track,
        *,
        quality_tier: str | None = None,
        progress_message=None,
        progress_lang: dict | None = None,
        progress_throttle: float = 5.0,
    ) -> CdnAsset | None:
        if not self.enabled:
            return None
        media_id = getattr(media, "id", None)
        if not media_id:
            return None

        video = bool(getattr(media, "video", False))
        source = detect_source(media)
        key = self.asset_key(
            media_id, video=video, quality_tier=quality_tier, source=source
        )
        wait_sec = float(getattr(config, "CDN_READY_WAIT_SEC", 15) or 15)
        # Cap cold waits — long CDN waits made /play feel stuck ~30s+.
        wait_sec = max(5.0, min(wait_sec, 30.0))

        # 1) READY hit (filesystem + store verify)
        ready_path = self._find_ready_file(media_id, video, quality_tier)
        if ready_path:
            asset = self._ready_asset(key, ready_path)
            if asset:
                return asset

        # Store-row READY with path
        try:
            row = self.store().get(key)
            if not row:
                row = self.store().get(
                    self.legacy_key(media_id, video=video, quality_tier=quality_tier)
                )
            if row and row.ready_path and is_valid_media_file(row.ready_path, video=video):
                return self._ready_asset(key, Path(row.ready_path))
        except Exception:
            pass

        # 2) Singleflight: one download/publish job per key
        async def _job() -> CdnAsset | None:
            # Re-check ready after joining the flight.
            path = self._find_ready_file(media_id, video, quality_tier)
            if path:
                return self._ready_asset(key, path)
            try:
                self.store().upsert_status(
                    key=key,
                    media_id=str(media_id),
                    video=video,
                    quality_tier=quality_tier or "",
                    status=CacheState.DOWNLOADING.value,
                    source=source,
                )
            except Exception:
                pass
            return await self._download_and_publish(
                media,
                key=key,
                quality_tier=quality_tier,
                progress_message=progress_message,
                progress_lang=progress_lang,
                progress_throttle=progress_throttle,
            )

        try:
            return await download_flight.do(key, _job, timeout=wait_sec)
        except asyncio.TimeoutError:
            logger.warning("CDN ready wait timed out key=%s", key)
            return None
        except Exception as ex:
            logger.warning("CDN download/publish failed key=%s: %s", key, ex)
            try:
                self.store().upsert_status(
                    key=key,
                    media_id=str(media_id),
                    video=video,
                    quality_tier=quality_tier or "",
                    status=CacheState.FAILED_TEMPORARY.value,
                    failure_reason=str(ex)[:200],
                    increment_retry=True,
                    source=source,
                )
            except Exception:
                pass
            return None

    async def _download_and_publish(
        self,
        media: Media | Track,
        *,
        key: str,
        quality_tier: str | None,
        progress_message=None,
        progress_lang: dict | None = None,
        progress_throttle: float = 5.0,
    ) -> CdnAsset | None:
        media_id = str(media.id)
        video = bool(getattr(media, "video", False))
        source = getattr(media, "source", None)
        started = time.time()

        # Prefer already-downloaded local path on the media object.
        local_src = getattr(media, "file_path", None) or getattr(media, "local_path", None)
        if local_src and str(local_src).startswith("http"):
            local_src = None
        min_bytes = 512 * 1024 if video else 64 * 1024
        if local_src and not yt.is_complete_media_file(local_src, min_bytes=min_bytes):
            # A direct-stream handoff may expose the destination filename while
            # yt-dlp is still writing it.  Never publish/move a partial file;
            # the owning one-shot task will make the validated local result
            # available for a later CDN publish.
            local_src = None

        if not local_src:
            if source == "tiktok_remote":
                local_src = await tiktok.download(
                    url=getattr(media, "url", "") or "",
                    media_id=media_id,
                    video=video,
                    message_id=(
                        getattr(progress_message, "id", None)
                        if progress_message and video
                        else None
                    ),
                )
            else:
                local_src = await yt.download(
                    media_id,
                    video=video,
                    quality_tier=quality_tier,
                    message_id=(
                        getattr(progress_message, "id", None)
                        if progress_message and video
                        else None
                    ),
                    progress_message=progress_message if video else None,
                    progress_lang=progress_lang if video else None,
                    progress_throttle=progress_throttle,
                    progress_media=media,
                    one_shot=True,
                )

        if not local_src or not yt.is_complete_media_file(
            local_src, min_bytes=min_bytes
        ):
            logger.warning("CDN source download missing key=%s", key)
            return None

        src = Path(local_src)
        ext = src.suffix.lstrip(".") or ("mp4" if video else "webm")
        filename = safe_filename(media_id, ext, quality_tier if video else None, video=video)
        dest = self.ready_dir() / filename

        # Stage into tmp as .part then atomic publish (matches architecture notes).
        part = self.tmp_dir() / f"{filename}.part"
        try:
            if part.exists():
                part.unlink()
        except Exception:
            pass

        try:
            # Move/copy into tmp.part first when source is outside ready tree.
            if src.resolve() != dest.resolve():
                try:
                    src.replace(part)
                except OSError:
                    import shutil

                    shutil.copy2(str(src), str(part))
                    try:
                        src.unlink()
                    except Exception:
                        pass
                ready_path = atomic_publish(part, dest)
            else:
                ready_path = dest
        except Exception as ex:
            logger.warning("CDN atomic publish failed key=%s: %s", key, ex)
            return None

        size = ready_path.stat().st_size if ready_path.exists() else 0
        ok, reason = validate_ready_file(ready_path, video=video)
        if not ok:
            logger.warning(
                "CDN published file failed validation key=%s reason=%s", key, reason
            )
            try:
                self.store().upsert_status(
                    key=key,
                    media_id=media_id,
                    video=video,
                    quality_tier=quality_tier or "",
                    status=CacheState.FAILED_TEMPORARY.value,
                    failure_reason=reason,
                    increment_retry=True,
                    source=detect_source(media),
                )
            except Exception:
                pass
            return None

        cdn_url = self.build_cdn_url(filename) or ""
        ttl = float(getattr(config, "CDN_TTL_HOURS", 24) or 24)
        title = str(getattr(media, "title", "") or "")
        artist = str(
            getattr(media, "channel_name", None)
            or getattr(media, "artist", None)
            or ""
        )
        duration = 0.0
        try:
            duration = float(
                getattr(media, "duration_sec", 0)
                or getattr(media, "duration", 0)
                or 0
            )
        except Exception:
            duration = 0.0
        self.store().upsert_ready(
            key=key,
            media_id=media_id,
            video=video,
            quality_tier=quality_tier or "",
            filename=filename,
            ready_path=str(ready_path),
            size_bytes=size,
            cdn_url=cdn_url,
            source=detect_source(media),
            query=str(
                getattr(media, "normalized_query", None)
                or getattr(media, "original_query", None)
                or ""
            ),
            title=title,
            artist=artist,
            duration=duration,
            thumbnail=str(getattr(media, "thumbnail", "") or ""),
            quality=quality_tier or "",
            ttl_hours=ttl,
        )
        local = str(ready_path)
        play = self.resolve_play_path(local, filename)
        logger.info(
            "CDN published key=%s file=%s size=%s elapsed=%.1fs play=%s",
            key,
            filename,
            size,
            time.time() - started,
            play,
        )
        return CdnAsset(
            key=key,
            local_path=local,
            play_url=play if str(play).startswith("http") else None,
            filename=filename,
            status=CacheState.READY.value,
        )


cdn = CdnManager()
