"""Health monitoring."""

import time
import logging
import shutil
from typing import Optional
from dataclasses import dataclass

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import HealthState, ResourceState, DiskState
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor
from AnonX_3.downloader_api.processing.ffmpeg_manager import ffmpeg_manager
from AnonX_3.downloader_api.processing.ffprobe_manager import ffprobe_manager
from AnonX_3.downloader_api.queue.queue_manager import queue_manager
from AnonX_3.downloader_api.cache.cache_manager import cache_manager

logger = logging.getLogger(__name__)


@dataclass
class ComponentStatus:
    name: str
    healthy: bool
    message: Optional[str] = None
    latency_ms: Optional[float] = None


class HealthMonitor:
    def __init__(self):
        self._start_time = time.time()

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    async def get_health_state(self) -> HealthState:
        snapshot = await resource_monitor.get_snapshot()

        if snapshot.resource_state == ResourceState.CRITICAL:
            return HealthState.CRITICAL

        if snapshot.disk_state == DiskState.CRITICAL:
            return HealthState.CRITICAL

        if snapshot.resource_state in (ResourceState.HIGH_LOAD, ResourceState.BUSY):
            return HealthState.BUSY

        if snapshot.disk_state in (DiskState.HIGH_PRESSURE, DiskState.WARNING):
            return HealthState.DEGRADED

        queue_stats = queue_manager.get_queue_stats()
        if queue_stats["queued_jobs"] > settings.max_queue_size * 0.8:
            return HealthState.BUSY

        return HealthState.HEALTHY

    async def check_components(self) -> list[ComponentStatus]:
        components = []

        ytdlp_status = await self._check_ytdlp()
        components.append(ytdlp_status)

        ffmpeg_status = self._check_ffmpeg()
        components.append(ffmpeg_status)

        ffprobe_status = self._check_ffprobe()
        components.append(ffprobe_status)

        db_status = self._check_database()
        components.append(db_status)

        disk_status = await self._check_disk()
        components.append(disk_status)

        return components

    async def _check_ytdlp(self) -> ComponentStatus:
        try:
            import yt_dlp
            return ComponentStatus(
                name="yt-dlp",
                healthy=True,
                message=f"Version: {yt_dlp.version.__version__}",
            )
        except ImportError:
            return ComponentStatus(
                name="yt-dlp",
                healthy=False,
                message="yt-dlp not installed",
            )
        except Exception as e:
            return ComponentStatus(
                name="yt-dlp",
                healthy=False,
                message=str(e),
            )

    def _check_ffmpeg(self) -> ComponentStatus:
        if ffmpeg_manager.is_available():
            return ComponentStatus(
                name="ffmpeg",
                healthy=True,
                message="Available",
            )
        return ComponentStatus(
            name="ffmpeg",
            healthy=False,
            message="FFmpeg not found in PATH",
        )

    def _check_ffprobe(self) -> ComponentStatus:
        if ffprobe_manager.is_available():
            return ComponentStatus(
                name="ffprobe",
                healthy=True,
                message="Available",
            )
        return ComponentStatus(
            name="ffprobe",
            healthy=False,
            message="FFprobe not found in PATH",
        )

    def _check_database(self) -> ComponentStatus:
        try:
            stats = cache_manager.get_stats()
            return ComponentStatus(
                name="database",
                healthy=True,
                message=f"{stats.total_entries} cache entries",
            )
        except Exception as e:
            return ComponentStatus(
                name="database",
                healthy=False,
                message=str(e),
            )

    async def _check_disk(self) -> ComponentStatus:
        snapshot = await resource_monitor.get_snapshot()

        if snapshot.disk_state == DiskState.CRITICAL:
            return ComponentStatus(
                name="disk",
                healthy=False,
                message=f"Critical: {snapshot.disk_free_gb:.1f} GB free",
            )
        elif snapshot.disk_state == DiskState.HIGH_PRESSURE:
            return ComponentStatus(
                name="disk",
                healthy=True,
                message=f"High pressure: {snapshot.disk_free_gb:.1f} GB free",
            )
        else:
            return ComponentStatus(
                name="disk",
                healthy=True,
                message=f"{snapshot.disk_free_gb:.1f} GB free",
            )

    def is_ytdlp_ready(self) -> bool:
        try:
            import yt_dlp
            return True
        except ImportError:
            return False

    def is_ffmpeg_ready(self) -> bool:
        return ffmpeg_manager.is_available()

    def is_ffprobe_ready(self) -> bool:
        return ffprobe_manager.is_available()


health_monitor = HealthMonitor()
