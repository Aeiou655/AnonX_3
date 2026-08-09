"""Unified cache catalog facade used by playback and CDN paths."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from AnonX_3.core.cache.keys import detect_source, make_cache_key
from AnonX_3.core.cache.states import CacheState, can_transition
from AnonX_3.core.downloader.validation import (
    matches_exact_playback_mode,
    validate_ready_file,
)


@dataclass
class CacheEntry:
    key: str
    media_id: str
    video: bool
    local_path: str = ""
    public_url: str = ""
    status: str = CacheState.READY.value
    source: str = "youtube"
    query: str = ""
    title: str = ""
    artist: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    quality: str = ""
    format_id: str = ""
    size_bytes: int = 0
    local_durable: bool = False


class CacheHub:
    def __init__(self) -> None:
        self._refcounts: dict[str, int] = {}
        self._lock = threading.RLock()

    def store(self):
        from AnonX_3.core.cdn import cdn

        return cdn.store()

    def key_for(
        self,
        media: Any,
        *,
        source: str | None = None,
        quality_tier: str | None = None,
    ) -> str:
        media_id = str(
            getattr(media, "id", "") or getattr(media, "media_id", "") or ""
        )
        return make_cache_key(
            source=source or detect_source(media),
            source_id=media_id,
            video=bool(getattr(media, "video", False)),
            quality_tier=quality_tier,
        )

    @staticmethod
    def _row_entry(row) -> CacheEntry:
        return CacheEntry(
            key=row.key,
            media_id=row.media_id,
            video=bool(row.video),
            local_path=str(row.ready_path or ""),
            public_url=str(row.public_url or row.cdn_url or ""),
            status=str(row.status or ""),
            source=str(row.source or "youtube"),
            query=str(row.query or ""),
            title=str(row.title or ""),
            artist=str(row.artist or ""),
            duration=float(row.duration or 0),
            thumbnail=str(row.thumbnail or ""),
            quality=str(row.quality or row.quality_tier or ""),
            format_id=str(row.format_id or ""),
            size_bytes=int(row.size_bytes or 0),
            local_durable=bool(getattr(row, "local_durable", False)),
        )

    def _validated_entry(
        self,
        row,
        *,
        video: bool | None = None,
    ) -> CacheEntry | None:
        if row is None or str(getattr(row, "status", "")) != CacheState.READY.value:
            return None
        requested_video = bool(row.video) if video is None else bool(video)
        path = str(getattr(row, "ready_path", "") or "")
        ok, _reason = validate_ready_file(path, video=requested_video)
        if not ok:
            return None
        if not matches_exact_playback_mode(path, video=requested_video):
            return None
        try:
            self.store().touch(row.key)
        except Exception:
            pass
        return self._row_entry(row)

    def lookup_key(
        self,
        key: str,
        *,
        video: bool | None = None,
    ) -> CacheEntry | None:
        try:
            row = self.store().get(key)
        except Exception:
            return None
        return self._validated_entry(row, video=video)

    def lookup_media(
        self,
        media: Any,
        *,
        quality_tier: str | None = None,
    ) -> CacheEntry | None:
        key = self.key_for(media, quality_tier=quality_tier)
        hit = self.lookup_key(key, video=bool(getattr(media, "video", False)))
        if hit is not None or not quality_tier:
            return hit
        fallback = make_cache_key(
            source=detect_source(media),
            source_id=str(getattr(media, "id", "") or ""),
            video=bool(getattr(media, "video", False)),
        )
        return self.lookup_key(
            fallback,
            video=bool(getattr(media, "video", False)),
        )

    def lookup_text(self, value: str, *, video: bool = False) -> CacheEntry | None:
        try:
            row = self.store().find_ready_by_lookup(value, video=bool(video))
        except Exception:
            return None
        return self._validated_entry(row, video=bool(video))

    def acquire(self, key: str) -> int:
        with self._lock:
            count = self._refcounts.get(key, 0) + 1
            self._refcounts[key] = count
        try:
            self.store().set_refcount(key, count)
        except Exception:
            pass
        return count

    def release(self, key: str) -> int:
        with self._lock:
            count = max(0, self._refcounts.get(key, 0) - 1)
            if count:
                self._refcounts[key] = count
            else:
                self._refcounts.pop(key, None)
        try:
            self.store().set_refcount(key, count)
        except Exception:
            pass
        return count

    def refcount(self, key: str) -> int:
        with self._lock:
            return int(self._refcounts.get(key, 0))

    def mark_status(
        self,
        key: str,
        state: CacheState | str,
        *,
        failure_reason: str = "",
        increment_retry: bool = False,
    ) -> None:
        state = state if isinstance(state, CacheState) else CacheState(str(state))
        store = self.store()
        row = store.get(key)
        if row is not None:
            try:
                current = CacheState(str(row.status))
            except ValueError:
                current = CacheState.MISS
            if not can_transition(current, state):
                return
            store.set_status(
                key,
                state.value,
                failure_reason=failure_reason,
                increment_retry=increment_retry,
            )
            return
        try:
            from AnonX_3.core.cache.keys import parse_cache_key

            parsed = parse_cache_key(key)
        except Exception:
            return
        store.upsert_status(
            key=key,
            media_id=str(parsed["media_id"]),
            video=bool(parsed["video"]),
            quality_tier=str(parsed["quality"]),
            status=state.value,
            failure_reason=failure_reason,
            increment_retry=increment_retry,
            source=str(parsed["source"]),
        )


cache_hub = CacheHub()
