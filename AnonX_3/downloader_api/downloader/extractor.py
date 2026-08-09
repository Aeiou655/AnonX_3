"""Metadata extractor using yt-dlp."""

import asyncio
import logging
from typing import Optional, Any

import yt_dlp

from AnonX_3.core.ytdlp_runtime import create_youtube_dl
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.exceptions import (
    ExtractionFailedError,
    MediaUnavailableError,
    DownloadTimeoutError,
)
from AnonX_3.downloader_api.downloader.yt_dlp_options import get_metadata_options
from AnonX_3.downloader_api.downloader.result_parser import parse_extraction_result
from AnonX_3.downloader_api.schemas.metadata import ExtractedMetadata
from AnonX_3.downloader_api.storage.metadata_store import metadata_store

logger = logging.getLogger(__name__)


class MetadataExtractor:
    def __init__(self):
        self.cache_ttl = 300

    async def extract(
        self,
        url: str,
        video_id: str,
        use_cache: bool = True,
        timeout: Optional[int] = None,
    ) -> ExtractedMetadata:
        timeout = timeout or settings.metadata_timeout_seconds

        if use_cache:
            cached = metadata_store.load(video_id, max_age_seconds=self.cache_ttl)
            if cached:
                logger.debug(f"Using cached metadata for {video_id}")
                return ExtractedMetadata(**cached)

        try:
            info = await asyncio.wait_for(
                self._extract_info(url),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise DownloadTimeoutError(f"Metadata extraction timed out after {timeout}s")
        except Exception as e:
            logger.error(f"Extraction failed for {video_id}: {e}")
            raise ExtractionFailedError(f"Failed to extract metadata: {e}")

        if not info:
            raise MediaUnavailableError("Media is unavailable or cannot be accessed")

        if info.get("is_live"):
            raise MediaUnavailableError("Live streams are not supported")

        parsed = parse_extraction_result(info)
        metadata = ExtractedMetadata(**parsed)

        if use_cache:
            metadata_store.save(video_id, metadata.model_dump())

        return metadata

    async def _extract_info(self, url: str) -> Optional[dict[str, Any]]:
        opts = get_metadata_options()

        def _do_extract():
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                try:
                    return ydl.extract_info(url, download=False)
                except yt_dlp.utils.DownloadError as e:
                    error_str = str(e).lower()
                    if "private" in error_str or "unavailable" in error_str:
                        raise MediaUnavailableError(str(e))
                    if "removed" in error_str or "deleted" in error_str:
                        raise MediaUnavailableError(str(e))
                    raise ExtractionFailedError(str(e))

        return await asyncio.to_thread(_do_extract)

    async def get_formats(
        self,
        url: str,
        video_id: str,
    ) -> list[dict]:
        metadata = await self.extract(url, video_id, use_cache=False)
        return metadata.formats

    def get_audio_formats(self, formats: list[dict]) -> list[dict]:
        audio_formats = []
        for f in formats:
            vcodec = f.get("vcodec")
            acodec = f.get("acodec")
            if vcodec in ("none", None) and acodec not in ("none", None):
                audio_formats.append(f)
        return audio_formats

    def get_video_formats(self, formats: list[dict]) -> list[dict]:
        video_formats = []
        for f in formats:
            vcodec = f.get("vcodec")
            if vcodec not in ("none", None):
                video_formats.append(f)
        return video_formats


metadata_extractor = MetadataExtractor()
