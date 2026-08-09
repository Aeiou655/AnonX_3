"""Disk space guard."""

import logging
from typing import Optional

from AnonX_3.downloader_api.core.constants import DiskState
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor

logger = logging.getLogger(__name__)


class DiskGuard:
    def __init__(self):
        self.emergency_cleanup_triggered = False

    async def check_disk_space(self, required_bytes: Optional[int] = None) -> bool:
        snapshot = await resource_monitor.get_snapshot()

        if snapshot.disk_state == DiskState.CRITICAL:
            logger.warning("Disk space critical")
            return False

        if required_bytes:
            required_gb = required_bytes / (1024 ** 3)
            buffer_gb = settings.min_free_disk_gb

            if snapshot.disk_free_gb < (required_gb + buffer_gb):
                logger.warning(
                    f"Insufficient disk space: need {required_gb + buffer_gb:.2f} GB, "
                    f"have {snapshot.disk_free_gb:.2f} GB"
                )
                return False

        return True

    async def should_trigger_cleanup(self) -> bool:
        snapshot = await resource_monitor.get_snapshot()
        return snapshot.disk_state in (DiskState.HIGH_PRESSURE, DiskState.CRITICAL)

    async def should_pause_video_downloads(self) -> bool:
        snapshot = await resource_monitor.get_snapshot()
        return snapshot.disk_state == DiskState.CRITICAL

    async def get_max_allowed_download_size(self) -> int:
        snapshot = await resource_monitor.get_snapshot()

        available = snapshot.disk_free_gb - settings.min_free_disk_gb
        available = max(0, available)

        return int(available * 0.5 * (1024 ** 3))


disk_guard = DiskGuard()
