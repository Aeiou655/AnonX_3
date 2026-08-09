"""Simple alerting system."""

import logging
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable
from enum import Enum

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertManager:
    def __init__(self):
        self._handlers: list[Callable[[str, AlertLevel, dict], Awaitable[None]]] = []

    def add_handler(
        self,
        handler: Callable[[str, AlertLevel, dict], Awaitable[None]],
    ) -> None:
        self._handlers.append(handler)

    async def send(
        self,
        message: str,
        level: AlertLevel = AlertLevel.WARNING,
        **details,
    ) -> None:
        alert_data = {
            "message": message,
            "level": level.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **details,
        }

        if level == AlertLevel.CRITICAL:
            logger.critical(message, extra=alert_data)
        elif level == AlertLevel.ERROR:
            logger.error(message, extra=alert_data)
        elif level == AlertLevel.WARNING:
            logger.warning(message, extra=alert_data)
        else:
            logger.info(message, extra=alert_data)

        for handler in self._handlers:
            try:
                await handler(message, level, alert_data)
            except Exception as e:
                logger.error(f"Alert handler failed: {e}")

    async def disk_critical(self, free_gb: float) -> None:
        await self.send(
            f"Disk space critical: {free_gb:.2f} GB free",
            level=AlertLevel.CRITICAL,
            free_gb=free_gb,
        )

    async def resource_critical(self, cpu: float, memory: float) -> None:
        await self.send(
            f"Resource usage critical: CPU {cpu:.1f}%, Memory {memory:.1f}%",
            level=AlertLevel.CRITICAL,
            cpu_percent=cpu,
            memory_percent=memory,
        )

    async def queue_full(self, queue_size: int) -> None:
        await self.send(
            f"Download queue is full: {queue_size} jobs",
            level=AlertLevel.WARNING,
            queue_size=queue_size,
        )


alert_manager = AlertManager()
