"""yt-dlp configuration options."""

from typing import Any, Optional
from pathlib import Path

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import ResourceState


def get_base_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": False,
        "ignoreerrors": False,
        "no_color": True,
        "geo_bypass": True,
        "extractor_retries": settings.extractor_retries,
        "file_access_retries": settings.file_access_retries,
        "socket_timeout": settings.socket_timeout_seconds,
        "retries": settings.http_retries,
        "fragment_retries": settings.fragment_retries,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "overwrites": False,
        "continuedl": True,
        "noprogress": True,
    }


def get_metadata_options() -> dict[str, Any]:
    opts = get_base_options()
    opts.update({
        "skip_download": True,
        "extract_flat": False,
        "simulate": True,
    })
    return opts


def get_download_options(
    output_path: Path,
    format_id: str,
    resource_state: ResourceState = ResourceState.NORMAL,
    progress_hook: Optional[callable] = None,
) -> dict[str, Any]:
    opts = get_base_options()

    concurrent_fragments = get_concurrent_fragments(resource_state)

    opts.update({
        "format": format_id,
        "outtmpl": str(output_path),
        "nopart": False,
        "concurrent_fragment_downloads": concurrent_fragments,
        "buffersize": 1024 * 16,
        "http_chunk_size": 1024 * 1024 * 10,
    })

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    return opts


def get_concurrent_fragments(resource_state: ResourceState) -> int:
    mapping = {
        ResourceState.IDLE: 6,
        ResourceState.NORMAL: 4,
        ResourceState.BUSY: 2,
        ResourceState.HIGH_LOAD: 1,
        ResourceState.CRITICAL: 1,
        ResourceState.RECOVERY: 2,
    }
    return mapping.get(resource_state, 4)


def get_audio_format_string(
    format_preference: str = "original",
    quality: str = "auto",
) -> str:
    if format_preference in ("original", "auto"):
        return "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"

    if format_preference == "m4a":
        return "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio"

    if format_preference == "opus":
        return "bestaudio[ext=webm][acodec=opus]/bestaudio[acodec=opus]/bestaudio"

    if format_preference == "mp3":
        return "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"

    return "bestaudio/best"


def get_video_format_string(
    max_height: int = 720,
    format_preference: str = "mp4",
) -> str:
    if format_preference == "original":
        return f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"

    if format_preference == "mp4":
        return (
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}][ext=mp4]/"
            f"best[height<={max_height}]"
        )

    if format_preference == "webm":
        return (
            f"bestvideo[height<={max_height}][ext=webm]+bestaudio[ext=webm]/"
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}]"
        )

    return f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"


def get_retry_options(attempt: int, max_attempts: int = 5) -> dict[str, Any]:
    base_retries = max(1, settings.http_retries - attempt)
    fragment_retries = max(1, settings.fragment_retries - attempt)

    return {
        "retries": base_retries,
        "fragment_retries": fragment_retries,
        "extractor_retries": max(1, settings.extractor_retries - attempt // 2),
    }
