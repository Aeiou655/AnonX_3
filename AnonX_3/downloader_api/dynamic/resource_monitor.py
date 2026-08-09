"""Resource monitoring using psutil."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import psutil

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import ResourceState, DiskState
from AnonX_3.downloader_api.utils.disk import get_disk_usage, get_disk_state

logger = logging.getLogger(__name__)


@dataclass
class ResourceSnapshot:
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_percent: float = 0.0
    resource_state: ResourceState = ResourceState.NORMAL
    disk_state: DiskState = DiskState.NORMAL
    timestamp: float = field(default_factory=time.time)


class ResourceMonitor:
    def __init__(self):
        self._last_snapshot: Optional[ResourceSnapshot] = None
        self._snapshot_interval = 5.0
        self._recovery_start: Optional[float] = None
        self._recovery_duration = 30.0

    async def get_snapshot(self, force: bool = False) -> ResourceSnapshot:
        now = time.time()

        if not force and self._last_snapshot:
            if now - self._last_snapshot.timestamp < self._snapshot_interval:
                return self._last_snapshot

        snapshot = await self._collect_snapshot()
        self._last_snapshot = snapshot
        return snapshot

    async def _collect_snapshot(self) -> ResourceSnapshot:
        cpu_percent = await asyncio.to_thread(
            psutil.cpu_percent, interval=0.1
        )

        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        memory_used_mb = memory.used / (1024 * 1024)
        memory_total_mb = memory.total / (1024 * 1024)

        disk_total, disk_used, disk_free = get_disk_usage(settings.data_dir)
        disk_free_gb = disk_free / (1024 ** 3)
        disk_total_gb = disk_total / (1024 ** 3)
        disk_percent = (disk_used / disk_total * 100) if disk_total > 0 else 0

        resource_state = self._determine_resource_state(cpu_percent, memory_percent)
        disk_state = get_disk_state(
            settings.data_dir,
            warning_percent=settings.disk_warning_percent,
            critical_percent=settings.disk_critical_percent,
        )

        return ResourceSnapshot(
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            memory_used_mb=memory_used_mb,
            memory_total_mb=memory_total_mb,
            disk_free_gb=disk_free_gb,
            disk_total_gb=disk_total_gb,
            disk_percent=disk_percent,
            resource_state=resource_state,
            disk_state=disk_state,
            timestamp=time.time(),
        )

    def _determine_resource_state(
        self,
        cpu_percent: float,
        memory_percent: float,
    ) -> ResourceState:
        if self._recovery_start is not None:
            elapsed = time.time() - self._recovery_start
            if elapsed < self._recovery_duration:
                if cpu_percent < settings.cpu_normal_threshold and \
                   memory_percent < settings.memory_normal_threshold:
                    return ResourceState.RECOVERY
            else:
                self._recovery_start = None

        if cpu_percent >= settings.cpu_critical_threshold or \
           memory_percent >= settings.memory_critical_threshold:
            return ResourceState.CRITICAL

        if cpu_percent >= settings.cpu_busy_threshold or \
           memory_percent >= settings.memory_busy_threshold:
            was_critical = self._last_snapshot and \
                self._last_snapshot.resource_state == ResourceState.CRITICAL
            if was_critical:
                self._recovery_start = time.time()
                return ResourceState.RECOVERY
            return ResourceState.HIGH_LOAD

        if cpu_percent >= settings.cpu_normal_threshold or \
           memory_percent >= settings.memory_normal_threshold:
            return ResourceState.BUSY

        if cpu_percent < 30 and memory_percent < 50:
            return ResourceState.IDLE

        return ResourceState.NORMAL

    async def get_resource_state(self) -> ResourceState:
        snapshot = await self.get_snapshot()
        return snapshot.resource_state

    async def get_disk_state(self) -> DiskState:
        snapshot = await self.get_snapshot()
        return snapshot.disk_state

    async def has_sufficient_resources(self, min_free_disk_gb: float = 1.0) -> bool:
        snapshot = await self.get_snapshot()

        if snapshot.resource_state == ResourceState.CRITICAL:
            return False

        if snapshot.disk_free_gb < min_free_disk_gb:
            return False

        return True

    def get_cpu_count(self) -> int:
        return psutil.cpu_count(logical=True) or 1

    def get_available_memory_mb(self) -> float:
        memory = psutil.virtual_memory()
        return memory.available / (1024 * 1024)


resource_monitor = ResourceMonitor()
