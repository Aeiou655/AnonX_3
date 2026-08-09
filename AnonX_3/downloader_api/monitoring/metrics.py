"""Metrics collector for monitoring."""

import time
import logging
from dataclasses import dataclass, field
from typing import Dict
from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class Metrics:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    total_extraction_time_ms: float = 0
    total_download_time_ms: float = 0
    total_processing_time_ms: float = 0
    extraction_count: int = 0
    download_count: int = 0
    processing_count: int = 0
    total_bytes_downloaded: int = 0
    error_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))


class MetricsCollector:
    def __init__(self):
        self._metrics = Metrics()
        self._start_time = time.time()

    def record_request(self, success: bool) -> None:
        self._metrics.total_requests += 1
        if success:
            self._metrics.successful_requests += 1
        else:
            self._metrics.failed_requests += 1

    def record_cache_hit(self) -> None:
        self._metrics.cache_hits += 1

    def record_cache_miss(self) -> None:
        self._metrics.cache_misses += 1

    def record_extraction_time(self, duration_ms: float) -> None:
        self._metrics.total_extraction_time_ms += duration_ms
        self._metrics.extraction_count += 1

    def record_download_time(self, duration_ms: float, bytes_downloaded: int = 0) -> None:
        self._metrics.total_download_time_ms += duration_ms
        self._metrics.download_count += 1
        self._metrics.total_bytes_downloaded += bytes_downloaded

    def record_processing_time(self, duration_ms: float) -> None:
        self._metrics.total_processing_time_ms += duration_ms
        self._metrics.processing_count += 1

    def record_error(self, error_code: str) -> None:
        self._metrics.error_counts[error_code] += 1

    @property
    def cache_hit_ratio(self) -> float:
        total = self._metrics.cache_hits + self._metrics.cache_misses
        if total == 0:
            return 0.0
        return self._metrics.cache_hits / total

    @property
    def avg_extraction_time_ms(self) -> float:
        if self._metrics.extraction_count == 0:
            return 0.0
        return self._metrics.total_extraction_time_ms / self._metrics.extraction_count

    @property
    def avg_download_time_ms(self) -> float:
        if self._metrics.download_count == 0:
            return 0.0
        return self._metrics.total_download_time_ms / self._metrics.download_count

    @property
    def avg_processing_time_ms(self) -> float:
        if self._metrics.processing_count == 0:
            return 0.0
        return self._metrics.total_processing_time_ms / self._metrics.processing_count

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    @property
    def download_throughput_mbps(self) -> float:
        if self.uptime_seconds == 0:
            return 0.0
        bytes_per_second = self._metrics.total_bytes_downloaded / self.uptime_seconds
        return bytes_per_second * 8 / (1024 * 1024)

    def get_metrics(self) -> dict:
        return {
            "total_requests": self._metrics.total_requests,
            "successful_requests": self._metrics.successful_requests,
            "failed_requests": self._metrics.failed_requests,
            "cache_hit_ratio": self.cache_hit_ratio,
            "avg_extraction_time_ms": self.avg_extraction_time_ms,
            "avg_download_time_ms": self.avg_download_time_ms,
            "avg_processing_time_ms": self.avg_processing_time_ms,
            "download_throughput_mbps": self.download_throughput_mbps,
            "uptime_seconds": self.uptime_seconds,
            "error_counts": dict(self._metrics.error_counts),
        }

    def reset(self) -> None:
        self._metrics = Metrics()
        self._start_time = time.time()


metrics_collector = MetricsCollector()
