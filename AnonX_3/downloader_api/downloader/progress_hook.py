"""Progress hook for yt-dlp downloads."""

import logging
from typing import Callable, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class DownloadProgress:
    status: str = "downloading"
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed: Optional[float] = None
    eta: Optional[int] = None
    filename: Optional[str] = None
    fragment_index: Optional[int] = None
    fragment_count: Optional[int] = None
    elapsed: Optional[float] = None
    percent: float = 0.0
    updated_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def speed_str(self) -> str:
        if not self.speed:
            return "N/A"
        if self.speed < 1024:
            return f"{self.speed:.0f} B/s"
        elif self.speed < 1024 * 1024:
            return f"{self.speed / 1024:.1f} KB/s"
        else:
            return f"{self.speed / (1024 * 1024):.2f} MB/s"

    @property
    def eta_str(self) -> str:
        if not self.eta:
            return "N/A"
        if self.eta < 60:
            return f"{self.eta}s"
        elif self.eta < 3600:
            return f"{self.eta // 60}m {self.eta % 60}s"
        else:
            return f"{self.eta // 3600}h {(self.eta % 3600) // 60}m"


class ProgressHook:
    def __init__(
        self,
        job_id: str,
        callback: Optional[Callable[[DownloadProgress], None]] = None,
    ):
        self.job_id = job_id
        self.callback = callback
        self.progress = DownloadProgress()
        self._last_log_percent = 0

    def __call__(self, d: dict) -> None:
        status = d.get("status", "downloading")
        self.progress.status = status
        self.progress.updated_at = datetime.utcnow()

        if status == "downloading":
            self._update_downloading(d)
        elif status == "finished":
            self._update_finished(d)
        elif status == "error":
            self._update_error(d)

        if self.callback:
            try:
                self.callback(self.progress)
            except Exception as e:
                logger.error(f"Progress callback error: {e}")

    def _update_downloading(self, d: dict) -> None:
        self.progress.downloaded_bytes = d.get("downloaded_bytes", 0)
        self.progress.total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
        self.progress.speed = d.get("speed")
        self.progress.eta = d.get("eta")
        self.progress.filename = d.get("filename")
        self.progress.fragment_index = d.get("fragment_index")
        self.progress.fragment_count = d.get("fragment_count")
        self.progress.elapsed = d.get("elapsed")

        if self.progress.total_bytes and self.progress.total_bytes > 0:
            self.progress.percent = (
                self.progress.downloaded_bytes / self.progress.total_bytes * 100
            )
        elif self.progress.fragment_count and self.progress.fragment_index:
            self.progress.percent = (
                self.progress.fragment_index / self.progress.fragment_count * 100
            )

        if self.progress.percent - self._last_log_percent >= 10:
            logger.debug(
                f"Download progress: {self.progress.percent:.1f}%",
                extra={
                    "job_id": self.job_id,
                    "percent": self.progress.percent,
                    "speed": self.progress.speed_str,
                },
            )
            self._last_log_percent = self.progress.percent

    def _update_finished(self, d: dict) -> None:
        self.progress.percent = 100.0
        self.progress.filename = d.get("filename")
        self.progress.downloaded_bytes = d.get("downloaded_bytes", 0)
        if d.get("total_bytes"):
            self.progress.total_bytes = d["total_bytes"]

        logger.info(
            f"Download finished",
            extra={
                "job_id": self.job_id,
                "filename": self.progress.filename,
                "size": self.progress.downloaded_bytes,
            },
        )

    def _update_error(self, d: dict) -> None:
        logger.error(
            f"Download error",
            extra={
                "job_id": self.job_id,
                "error": d.get("error"),
            },
        )

    def get_progress(self) -> DownloadProgress:
        return self.progress
