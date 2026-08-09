"""Disk-pressure cache cleanup for the optional downloader API."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.schemas.cache import CacheCleanupResult
from AnonX_3.downloader_api.storage.file_manager import file_manager
from AnonX_3.downloader_api.storage.path_manager import path_manager
from .cache_manager import cache_manager


class CleanupManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @staticmethod
    def _disk_percent() -> float:
        try:
            usage = shutil.disk_usage(settings.data_dir)
            return (usage.used / usage.total * 100.0) if usage.total else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _high_water() -> float:
        # v3.4.2 stability invariant: legacy env/config must never lower the
        # reusable-media deletion threshold below 95%.
        return max(
            95.0,
            min(
                99.0,
                float(
                    getattr(settings, "cache_cleanup_high_water_percent", 95)
                    or 95
                ),
            ),
        )

    @staticmethod
    def _target() -> float:
        high = CleanupManager._high_water()
        configured = float(
            getattr(settings, "cache_cleanup_target_percent", 90) or 90
        )
        return max(40.0, min(high - 1.0, configured))

    @staticmethod
    def _cleanup_old_temp(result: CacheCleanupResult) -> None:
        """Temp/quarantine files are incomplete artifacts, not reusable cache."""
        now = time.time()
        temp_cutoff = now - max(60, int(settings.temp_ttl_minutes)) * 60
        quarantine_cutoff = (
            now - max(1, int(settings.quarantine_ttl_hours)) * 3600
        )
        for root, cutoff, field in (
            (settings.temp_dir, temp_cutoff, "removed_temp_files"),
            (
                settings.quarantine_dir,
                quarantine_cutoff,
                "removed_quarantine_files",
            ),
        ):
            if not root.exists():
                continue
            for path in list(root.rglob("*")):
                try:
                    if not path.is_file() or path.stat().st_mtime >= cutoff:
                        continue
                    if file_manager.safe_delete(path):
                        setattr(result, field, getattr(result, field) + 1)
                except Exception as ex:
                    result.errors.append(f"{path}: {type(ex).__name__}")

    async def run_cleanup(
        self,
        force: bool = False,
        emergency: bool = False,
    ) -> CacheCleanupResult:
        result = CacheCleanupResult()
        async with self._lock:
            # Safe hygiene for incomplete artifacts is always allowed.
            await asyncio.to_thread(self._cleanup_old_temp, result)

            current = self._disk_percent()
            # Reusable cache has one invariant across scheduled and admin
            # cleanup calls: do not delete it at or below 95% usage. The
            # explicit DELETE /admin/cache endpoint is the only manual wipe.
            if current <= self._high_water():
                return result

            database = cache_manager.database()
            candidates = await asyncio.to_thread(
                database.get_oldest_entries, 10000
            )
            target = self._target()
            for entry in candidates:
                if self._disk_percent() <= target:
                    break
                path = Path(entry.file_path)
                size = int(entry.file_size or 0)
                try:
                    if path.exists() and not file_manager.safe_delete(path):
                        result.errors.append(f"delete_failed:{path}")
                        continue
                    await asyncio.to_thread(
                        database.delete_cache_entry,
                        entry.cache_key,
                    )
                    result.removed_entries += 1
                    result.removed_size_bytes += max(0, size)
                except Exception as ex:
                    result.errors.append(
                        f"{entry.cache_key}:{type(ex).__name__}"
                    )
            return result

    async def clear_all_cache(self) -> CacheCleanupResult:
        """Explicit admin-only destructive clear; never called automatically."""
        result = CacheCleanupResult()
        async with self._lock:
            database = cache_manager.database()
            candidates = await asyncio.to_thread(
                database.get_oldest_entries,
                1000000,
            )
            for entry in candidates:
                try:
                    Path(entry.file_path).unlink(missing_ok=True)
                    if await asyncio.to_thread(
                        database.delete_cache_entry,
                        entry.cache_key,
                    ):
                        result.removed_entries += 1
                        result.removed_size_bytes += max(
                            0,
                            int(entry.file_size or 0),
                        )
                except Exception as ex:
                    result.errors.append(
                        f"{entry.cache_key}:{type(ex).__name__}"
                    )
            return result


cleanup_manager = CleanupManager()
