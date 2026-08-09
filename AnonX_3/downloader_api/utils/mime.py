"""MIME type utilities."""

from pathlib import Path
from typing import Optional

from AnonX_3.downloader_api.core.constants import AUDIO_MIME_TYPES, VIDEO_MIME_TYPES, MediaType


def get_mime_type(file_path: str | Path, media_type: Optional[MediaType] = None) -> str:
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext in AUDIO_MIME_TYPES:
        return AUDIO_MIME_TYPES[ext]

    if ext in VIDEO_MIME_TYPES:
        return VIDEO_MIME_TYPES[ext]

    if media_type == MediaType.AUDIO:
        return "audio/octet-stream"
    elif media_type == MediaType.VIDEO:
        return "video/octet-stream"

    return "application/octet-stream"


def get_extension_for_mime(mime_type: str) -> str:
    for ext, mime in AUDIO_MIME_TYPES.items():
        if mime == mime_type:
            return ext

    for ext, mime in VIDEO_MIME_TYPES.items():
        if mime == mime_type:
            return ext

    return ".bin"


def is_audio_mime(mime_type: str) -> bool:
    return mime_type.startswith("audio/")


def is_video_mime(mime_type: str) -> bool:
    return mime_type.startswith("video/")


def validate_mime_type(mime_type: str, expected_media_type: MediaType) -> bool:
    if expected_media_type == MediaType.AUDIO:
        return is_audio_mime(mime_type)
    elif expected_media_type == MediaType.VIDEO:
        return is_video_mime(mime_type) or is_audio_mime(mime_type)
    return False
