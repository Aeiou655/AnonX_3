"""Application exceptions."""

from typing import Optional


class DownloaderAPIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        status_code: int = 500,
        details: Optional[dict] = None,
    ):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class InvalidAPIKeyError(DownloaderAPIError):
    def __init__(self, message: str = "Invalid or missing API key"):
        super().__init__(
            code="INVALID_API_KEY",
            message=message,
            retryable=False,
            status_code=401,
        )


class RateLimitedError(DownloaderAPIError):
    def __init__(self, message: str = "Rate limit exceeded", retry_after: int = 60):
        super().__init__(
            code="RATE_LIMITED",
            message=message,
            retryable=True,
            status_code=429,
            details={"retry_after": retry_after},
        )


class InvalidURLError(DownloaderAPIError):
    def __init__(self, message: str = "Invalid URL provided"):
        super().__init__(
            code="INVALID_URL",
            message=message,
            retryable=False,
            status_code=400,
        )


class UnsupportedSourceError(DownloaderAPIError):
    def __init__(self, message: str = "Unsupported source"):
        super().__init__(
            code="UNSUPPORTED_SOURCE",
            message=message,
            retryable=False,
            status_code=403,
        )


class MediaUnavailableError(DownloaderAPIError):
    def __init__(self, message: str = "Media is unavailable"):
        super().__init__(
            code="MEDIA_UNAVAILABLE",
            message=message,
            retryable=False,
            status_code=404,
        )


class DurationLimitExceededError(DownloaderAPIError):
    def __init__(self, message: str = "Media duration exceeds limit"):
        super().__init__(
            code="DURATION_LIMIT_EXCEEDED",
            message=message,
            retryable=False,
            status_code=413,
        )


class FileSizeLimitExceededError(DownloaderAPIError):
    def __init__(self, message: str = "File size exceeds limit"):
        super().__init__(
            code="FILE_SIZE_LIMIT_EXCEEDED",
            message=message,
            retryable=False,
            status_code=413,
        )


class QueueFullError(DownloaderAPIError):
    def __init__(self, message: str = "Download queue is full"):
        super().__init__(
            code="QUEUE_FULL",
            message=message,
            retryable=True,
            status_code=503,
        )


class ResourceBusyError(DownloaderAPIError):
    def __init__(self, message: str = "Server resources are busy"):
        super().__init__(
            code="RESOURCE_BUSY",
            message=message,
            retryable=True,
            status_code=503,
        )


class InsufficientDiskError(DownloaderAPIError):
    def __init__(self, message: str = "Insufficient disk space"):
        super().__init__(
            code="INSUFFICIENT_DISK",
            message=message,
            retryable=True,
            status_code=507,
        )


class ExtractionFailedError(DownloaderAPIError):
    def __init__(self, message: str = "Failed to extract media information"):
        super().__init__(
            code="EXTRACTION_FAILED",
            message=message,
            retryable=True,
            status_code=502,
        )


class DownloadFailedError(DownloaderAPIError):
    def __init__(self, message: str = "Download failed"):
        super().__init__(
            code="DOWNLOAD_FAILED",
            message=message,
            retryable=True,
            status_code=502,
        )


class ProcessingFailedError(DownloaderAPIError):
    def __init__(self, message: str = "Media processing failed"):
        super().__init__(
            code="PROCESSING_FAILED",
            message=message,
            retryable=True,
            status_code=502,
        )


class ValidationFailedError(DownloaderAPIError):
    def __init__(self, message: str = "File validation failed"):
        super().__init__(
            code="VALIDATION_FAILED",
            message=message,
            retryable=False,
            status_code=502,
        )


class DownloadTimeoutError(DownloaderAPIError):
    def __init__(self, message: str = "Download timed out"):
        super().__init__(
            code="DOWNLOAD_TIMEOUT",
            message=message,
            retryable=True,
            status_code=504,
        )


class InternalError(DownloaderAPIError):
    def __init__(self, message: str = "Internal server error"):
        super().__init__(
            code="INTERNAL_ERROR",
            message=message,
            retryable=False,
            status_code=500,
        )


class JobNotFoundError(DownloaderAPIError):
    def __init__(self, message: str = "Job not found"):
        super().__init__(
            code="JOB_NOT_FOUND",
            message=message,
            retryable=False,
            status_code=404,
        )


class JobNotReadyError(DownloaderAPIError):
    def __init__(self, message: str = "Job is not ready"):
        super().__init__(
            code="JOB_NOT_READY",
            message=message,
            retryable=True,
            status_code=409,
        )


class UnsupportedFormatError(DownloaderAPIError):
    def __init__(self, message: str = "Unsupported format"):
        super().__init__(
            code="UNSUPPORTED_FORMAT",
            message=message,
            retryable=False,
            status_code=422,
        )
