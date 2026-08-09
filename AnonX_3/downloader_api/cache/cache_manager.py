"""Persistent downloader-cache facade.

The cache is durable by default. Automatic expiry is metadata only; files are
not removed simply because TTL elapsed. Physical reclaim is owned by
``cleanup_manager`` and starts only above the disk high-water threshold.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from AnonX_3.downloader_api.core.constants import CacheStatus, ValidationState
from AnonX_3.downloader_api.schemas.cache import CacheEntry, CacheStats
from AnonX_3.downloader_api.storage.database import Database


class CacheManager:
    def __init__(self) -> None:
        self._database: Database | None = None
        self._lock = asyncio.Lock()

    def database(self) -> Database:
        if self._database is None:
            self._database = Database()
        return self._database

    async def get(
        self, cache_key: str
    ) -> tuple[CacheStatus, CacheEntry | None, Path | None]:
        # SQLite operations are short, but execute them off the event loop so a
        # busy disk cannot stall Telegram dispatch.
        entry = await asyncio.to_thread(self.database().get_cache_entry, cache_key)
        if entry is None:
            return CacheStatus.MISS, None, None

        path = Path(entry.file_path)
        if entry.validation_state != ValidationState.VALID or not path.is_file():
            return CacheStatus.INVALID, entry, None

        # TTL is intentionally advisory. A still-present validated file remains
        # a cache HIT until disk-pressure cleanup reclaims it.
        await asyncio.to_thread(self.database().update_access, cache_key)
        try:
            entry.last_accessed_at = datetime.now(timezone.utc)
            entry.hit_count += 1
        except Exception:
            pass
        return CacheStatus.HIT, entry, path

    async def put(self, entry: CacheEntry) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self.database().add_cache_entry, entry)

    async def remove(self, cache_key: str, *, delete_file: bool = True) -> bool:
        async with self._lock:
            entry = await asyncio.to_thread(self.database().get_cache_entry, cache_key)
            if entry is None:
                return True
            if delete_file:
                try:
                    Path(entry.file_path).unlink(missing_ok=True)
                except Exception:
                    return False
            return await asyncio.to_thread(
                self.database().delete_cache_entry, cache_key
            )

    def get_stats(self) -> CacheStats:
        return self.database().get_stats()


cache_manager = CacheManager()
