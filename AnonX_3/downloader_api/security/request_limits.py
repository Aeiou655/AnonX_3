"""Request limits validation."""

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.exceptions import (
    DurationLimitExceededError,
    FileSizeLimitExceededError,
    InvalidURLError,
)
from AnonX_3.downloader_api.core.constants import MediaType


class RequestLimits:
    def __init__(self):
        self.max_url_length = settings.max_url_length
        self.max_audio_duration = settings.max_audio_duration_seconds
        self.max_video_duration = settings.max_video_duration_seconds
        self.max_audio_size = settings.max_audio_file_size_bytes
        self.max_video_size = settings.max_video_file_size_bytes

    def validate_url_length(self, url: str) -> None:
        if len(url) > self.max_url_length:
            raise InvalidURLError(
                f"URL length exceeds maximum of {self.max_url_length} characters"
            )

    def validate_duration(
        self,
        duration: int,
        media_type: MediaType,
    ) -> None:
        if media_type == MediaType.AUDIO:
            max_duration = self.max_audio_duration
        else:
            max_duration = self.max_video_duration

        if duration > max_duration:
            raise DurationLimitExceededError(
                f"Media duration ({duration}s) exceeds maximum of {max_duration}s"
            )

    def validate_file_size(
        self,
        estimated_size: int,
        media_type: MediaType,
    ) -> None:
        if media_type == MediaType.AUDIO:
            max_size = self.max_audio_size
        else:
            max_size = self.max_video_size

        if estimated_size > max_size:
            max_size_mb = max_size / (1024 * 1024)
            estimated_mb = estimated_size / (1024 * 1024)
            raise FileSizeLimitExceededError(
                f"Estimated file size ({estimated_mb:.1f} MB) exceeds maximum of {max_size_mb:.1f} MB"
            )


request_limits = RequestLimits()
