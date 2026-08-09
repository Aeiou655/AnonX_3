"""Bandwidth manager (placeholder for future network-aware throttling)."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class BandwidthManager:
    def __init__(self):
        self.max_concurrent_downloads = 5
        self.max_bandwidth_mbps: Optional[float] = None

    def get_max_concurrent_downloads(self) -> int:
        return self.max_concurrent_downloads

    def set_max_concurrent_downloads(self, limit: int) -> None:
        self.max_concurrent_downloads = max(1, min(limit, 10))

    def set_bandwidth_limit(self, mbps: Optional[float]) -> None:
        self.max_bandwidth_mbps = mbps

    def get_bandwidth_limit(self) -> Optional[float]:
        return self.max_bandwidth_mbps


bandwidth_manager = BandwidthManager()
