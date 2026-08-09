# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""TTL + disk high-water garbage collector for CDN ready/ and stale tmp parts."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from AnonX_3 import config, logger
from AnonX_3.core.cdn.manager import cdn
from AnonX_3.core.resource_manager import resource_manager


def _live_refcount(key: str, cache_hub) -> int:
    if cache_hub is None:
        return 0
    try:
        return int(cache_hub.refcount(key) or 0)
    except Exception:
        return 0


def _is_active(row, cache_hub) -> bool:
    live_rc = _live_refcount(row.key, cache_hub)
    row_rc = int(getattr(row, "refcount", 0) or 0)
    return live_rc > 0 or row_rc > 0


def _is_popular(row, now: float, popular_window_sec: float = 6 * 3600) -> bool:
    """Check if a song is popular (accessed within the popular window)."""
    last = float(getattr(row, "last_access", 0) or 0)
    if not last:
        return False
    return (now - last) < popular_window_sec


def _safe_unlink(path: Path) -> bool:
    try:
        if path.is_file():
            path.unlink()
            return True
    except Exception as ex:
        logger.warning("CDN GC delete failed %s: %s", path, ex)
    return False


def _dir_size_bytes(root: Path) -> int:
    total = 0
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


async def cdn_gc_loop() -> None:
    """Background loop: TTL expiry, orphan parts, disk high-water LRU.

    Popular songs (accessed within last 6 hours) are NOT deleted unless
    disk usage exceeds 80% threshold.
    """
    if not getattr(config, "CDN_ENABLED", False):
        return

    interval = max(60, int(getattr(config, "CDN_GC_INTERVAL_SEC", 900) or 900))
    ttl_hours = max(1.0, float(getattr(config, "CDN_TTL_HOURS", 24) or 24))
    popular_ttl_hours = max(
        ttl_hours,
        float(getattr(config, "CDN_POPULAR_TTL_HOURS", ttl_hours * 2) or ttl_hours * 2),
    )
    uncommon_ttl_hours = max(
        1.0, float(getattr(config, "CDN_UNCOMMON_TTL_HOURS", 6) or 6)
    )
    tmp_grace_sec = max(
        600, int(getattr(config, "CDN_TMP_GRACE_SEC", 2 * 3600) or 2 * 3600)
    )
    popular_window_sec = 6 * 3600  # 6 hours = popular

    logger.info(
        "CDN GC started ttl_hours=%s uncommon=%s popular=%s interval_sec=%s "
        "high_water=%s%% root=%s",
        ttl_hours,
        uncommon_ttl_hours,
        popular_ttl_hours,
        interval,
        resource_manager.disk_high_water_pct(),
        cdn.media_root(),
    )

    while True:
        try:
            deleted = 0
            store = cdn.store()
            try:
                from AnonX_3.core.cache.hub import cache_hub
            except Exception:
                cache_hub = None

            now = time.time()
            disk_over_threshold = resource_manager.over_disk_high_water(cdn.media_root())

            # --- 1) TTL / expired entries ---
            # Popular songs are NEVER deleted here (only in high-water cleanup)
            for row in list(store.expired(ttl_hours)):
                if _is_active(row, cache_hub):
                    logger.debug("CDN GC skip active key=%s", row.key)
                    continue

                # Skip popular songs unless disk is over threshold
                if _is_popular(row, now, popular_window_sec) and not disk_over_threshold:
                    logger.debug("CDN GC skip popular key=%s", row.key)
                    try:
                        store.touch(row.key, extend_ttl_hours=popular_ttl_hours)
                    except Exception:
                        pass
                    continue

                last = float(getattr(row, "last_access", 0) or 0)
                age_hours = (now - last) / 3600.0 if last else 999.0

                # Uncommon: allow earlier purge with uncommon TTL
                if age_hours < uncommon_ttl_hours and age_hours < ttl_hours:
                    continue

                path = Path(row.ready_path) if row.ready_path else None
                if path and _safe_unlink(path):
                    deleted += 1
                elif path is None or not path.exists():
                    deleted += 1  # metadata-only cleanup counts
                try:
                    store.delete(row.key)
                except Exception:
                    pass

            # --- 2) Metadata for missing files ---
            try:
                for row in store.list_by_status("ready"):
                    if not row.ready_path:
                        continue
                    if _is_active(row, cache_hub):
                        continue
                    p = Path(row.ready_path)
                    if not p.is_file():
                        try:
                            store.delete(row.key)
                            deleted += 1
                        except Exception:
                            pass
            except Exception:
                pass

            # --- 3) Orphan tmp .part files ---
            cutoff = now - tmp_grace_sec
            try:
                for part in cdn.tmp_dir().glob("*.part"):
                    try:
                        if part.stat().st_mtime < cutoff:
                            if _safe_unlink(part):
                                deleted += 1
                    except Exception:
                        pass
                for part in cdn.tmp_dir().glob("*.publishing"):
                    try:
                        if part.stat().st_mtime < cutoff:
                            if _safe_unlink(part):
                                deleted += 1
                    except Exception:
                        pass
            except Exception:
                pass

            # --- 4) Disk high-water (>80%): LRU delete including popular songs ---
            if disk_over_threshold:
                target = resource_manager.disk_target_pct()
                logger.warning(
                    "CDN GC disk high-water disk_pct=%s target=%s — LRU reclaim (including popular)",
                    resource_manager.disk_usage_pct(cdn.media_root()),
                    target,
                )
                try:
                    candidates = store.lru_candidates(limit=200)
                except Exception:
                    candidates = []

                # Sort: non-popular first, then popular (delete non-popular first)
                non_popular = [r for r in candidates if not _is_popular(r, now, popular_window_sec)]
                popular = [r for r in candidates if _is_popular(r, now, popular_window_sec)]
                sorted_candidates = non_popular + popular

                for row in sorted_candidates:
                    if not resource_manager.over_disk_high_water(cdn.media_root()):
                        pct = resource_manager.disk_usage_pct(cdn.media_root())
                        if pct is not None and pct <= target:
                            break
                    if _is_active(row, cache_hub):
                        continue
                    path = Path(row.ready_path) if row.ready_path else None
                    if path and _safe_unlink(path):
                        deleted += 1
                    try:
                        store.delete(row.key)
                    except Exception:
                        pass
                    pct = resource_manager.disk_usage_pct(cdn.media_root())
                    if pct is not None and pct <= target:
                        break

            if deleted:
                logger.info(
                    "CDN GC removed %s asset(s)/part(s) disk_pct=%s",
                    deleted,
                    resource_manager.disk_usage_pct(cdn.media_root()),
                )
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.warning("CDN GC cycle failed: %s", ex)

        await asyncio.sleep(interval)
