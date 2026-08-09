"""Retry policy for downloads."""

import logging
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


class RetryReason(str, Enum):
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    FORMAT_UNAVAILABLE = "format_unavailable"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    EXTRACTION_FAILED = "extraction_failed"
    UNKNOWN = "unknown"


@dataclass
class RetryAttempt:
    attempt: int
    max_attempts: int
    reason: RetryReason
    error_message: Optional[str]
    should_refresh_formats: bool
    should_reduce_quality: bool
    should_use_fallback: bool
    delay_seconds: float


class RetryPolicy:
    def __init__(self, max_attempts: int = 5):
        self.max_attempts = max_attempts
        self.base_delay = 2.0
        self.max_delay = 60.0

    def should_retry(
        self,
        attempt: int,
        error: Exception,
    ) -> Optional[RetryAttempt]:
        if attempt >= self.max_attempts:
            logger.warning(f"Max retry attempts ({self.max_attempts}) reached")
            return None

        reason = self._classify_error(error)
        error_message = str(error)

        should_refresh = attempt >= 2 or reason == RetryReason.FORMAT_UNAVAILABLE
        should_reduce = attempt >= 4
        should_fallback = attempt >= 3

        delay = self._calculate_delay(attempt, reason)

        return RetryAttempt(
            attempt=attempt + 1,
            max_attempts=self.max_attempts,
            reason=reason,
            error_message=error_message,
            should_refresh_formats=should_refresh,
            should_reduce_quality=should_reduce,
            should_use_fallback=should_fallback,
            delay_seconds=delay,
        )

    def _classify_error(self, error: Exception) -> RetryReason:
        error_str = str(error).lower()

        if "timeout" in error_str or "timed out" in error_str:
            return RetryReason.TIMEOUT

        if "429" in error_str or "rate limit" in error_str:
            return RetryReason.RATE_LIMITED

        if "format" in error_str and ("unavailable" in error_str or "not available" in error_str):
            return RetryReason.FORMAT_UNAVAILABLE

        if "500" in error_str or "502" in error_str or "503" in error_str:
            return RetryReason.SERVER_ERROR

        if "connection" in error_str or "network" in error_str:
            return RetryReason.NETWORK_ERROR

        if "extract" in error_str:
            return RetryReason.EXTRACTION_FAILED

        return RetryReason.UNKNOWN

    def _calculate_delay(self, attempt: int, reason: RetryReason) -> float:
        base = self.base_delay * (2 ** attempt)

        if reason == RetryReason.RATE_LIMITED:
            base = max(base, 30.0)
        elif reason == RetryReason.SERVER_ERROR:
            base = max(base, 10.0)

        return min(base, self.max_delay)

    def get_quality_reduction(self, attempt: int, current_height: int) -> int:
        reductions = {
            4: {1080: 720, 720: 480, 480: 360, 360: 360},
            5: {1080: 480, 720: 360, 480: 360, 360: 360},
        }

        if attempt in reductions and current_height in reductions[attempt]:
            return reductions[attempt][current_height]

        return current_height


retry_policy = RetryPolicy()
