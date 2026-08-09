"""Media file validator."""

import logging
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass

from AnonX_3.downloader_api.core.constants import MediaType, ValidationState, AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.processing.ffprobe_manager import ffprobe_manager, MediaInfo
from AnonX_3.downloader_api.storage.file_manager import file_manager

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    is_valid: bool
    state: ValidationState
    media_info: Optional[MediaInfo] = None
    error: Optional[str] = None
    file_size: int = 0


class MediaValidator:
    def __init__(self):
        self.min_file_size = 1024
        self.min_duration = 0.5

    async def validate(
        self,
        file_path: Path,
        media_type: MediaType,
        expected_duration: Optional[float] = None,
    ) -> ValidationResult:
        if not file_path.exists():
            return ValidationResult(
                is_valid=False,
                state=ValidationState.INVALID,
                error="File does not exist",
            )

        file_size = file_path.stat().st_size
        if file_size < self.min_file_size:
            return ValidationResult(
                is_valid=False,
                state=ValidationState.INVALID,
                error=f"File too small: {file_size} bytes",
                file_size=file_size,
            )

        ext = file_path.suffix.lower()
        if media_type == MediaType.AUDIO:
            if ext not in AUDIO_EXTENSIONS:
                return ValidationResult(
                    is_valid=False,
                    state=ValidationState.INVALID,
                    error=f"Invalid audio extension: {ext}",
                    file_size=file_size,
                )
        else:
            if ext not in VIDEO_EXTENSIONS and ext not in AUDIO_EXTENSIONS:
                return ValidationResult(
                    is_valid=False,
                    state=ValidationState.INVALID,
                    error=f"Invalid video extension: {ext}",
                    file_size=file_size,
                )

        media_info = await ffprobe_manager.probe(file_path)

        if not media_info.is_valid:
            return ValidationResult(
                is_valid=False,
                state=ValidationState.INVALID,
                media_info=media_info,
                error=media_info.error or "FFprobe validation failed",
                file_size=file_size,
            )

        if media_type == MediaType.AUDIO:
            if not media_info.has_audio:
                return ValidationResult(
                    is_valid=False,
                    state=ValidationState.INVALID,
                    media_info=media_info,
                    error="No audio stream found in audio file",
                    file_size=file_size,
                )

        if media_type == MediaType.VIDEO:
            if not media_info.has_video:
                return ValidationResult(
                    is_valid=False,
                    state=ValidationState.INVALID,
                    media_info=media_info,
                    error="No video stream found in video file",
                    file_size=file_size,
                )

        if media_info.duration is not None:
            if media_info.duration < self.min_duration:
                return ValidationResult(
                    is_valid=False,
                    state=ValidationState.INVALID,
                    media_info=media_info,
                    error=f"Duration too short: {media_info.duration}s",
                    file_size=file_size,
                )

            if expected_duration is not None:
                duration_diff = abs(media_info.duration - expected_duration)
                tolerance = max(expected_duration * 0.1, 5.0)
                if duration_diff > tolerance:
                    logger.warning(
                        f"Duration mismatch: expected {expected_duration}s, got {media_info.duration}s"
                    )

        return ValidationResult(
            is_valid=True,
            state=ValidationState.VALID,
            media_info=media_info,
            file_size=file_size,
        )

    async def validate_and_quarantine(
        self,
        file_path: Path,
        media_type: MediaType,
        job_id: str,
        expected_duration: Optional[float] = None,
    ) -> Tuple[ValidationResult, Optional[Path]]:
        result = await self.validate(file_path, media_type, expected_duration)

        if not result.is_valid:
            quarantine_path = file_manager.move_to_quarantine(
                source_path=file_path,
                job_id=job_id,
                reason=result.error or "validation_failed",
            )
            return result, quarantine_path

        return result, None

    async def quick_validate(self, file_path: Path) -> bool:
        if not file_path.exists():
            return False

        if file_path.stat().st_size < self.min_file_size:
            return False

        return await ffprobe_manager.is_valid(file_path)


media_validator = MediaValidator()
