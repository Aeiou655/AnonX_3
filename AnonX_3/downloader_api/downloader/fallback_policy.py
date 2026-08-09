"""Fallback policy for downloads."""

import logging
from dataclasses import dataclass
from typing import Optional, List

from AnonX_3.downloader_api.core.constants import MediaType, Quality

logger = logging.getLogger(__name__)


@dataclass
class FallbackOption:
    format_string: str
    description: str
    quality_level: str
    requires_processing: bool


class FallbackPolicy:
    def __init__(self):
        self.audio_fallbacks = [
            FallbackOption(
                format_string="bestaudio[ext=m4a]/bestaudio",
                description="Best M4A audio",
                quality_level="high",
                requires_processing=False,
            ),
            FallbackOption(
                format_string="bestaudio[ext=webm]/bestaudio",
                description="Best WebM audio",
                quality_level="high",
                requires_processing=False,
            ),
            FallbackOption(
                format_string="bestaudio",
                description="Best available audio",
                quality_level="auto",
                requires_processing=False,
            ),
            FallbackOption(
                format_string="worstaudio",
                description="Lowest quality audio",
                quality_level="low",
                requires_processing=False,
            ),
        ]

        self.video_fallbacks = {
            1080: [
                FallbackOption(
                    format_string="bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
                    description="1080p MP4",
                    quality_level="1080",
                    requires_processing=True,
                ),
                FallbackOption(
                    format_string="bestvideo[height<=720]+bestaudio/best[height<=720]",
                    description="720p fallback",
                    quality_level="720",
                    requires_processing=True,
                ),
            ],
            720: [
                FallbackOption(
                    format_string="bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
                    description="720p MP4",
                    quality_level="720",
                    requires_processing=True,
                ),
                FallbackOption(
                    format_string="bestvideo[height<=480]+bestaudio/best[height<=480]",
                    description="480p fallback",
                    quality_level="480",
                    requires_processing=True,
                ),
            ],
            480: [
                FallbackOption(
                    format_string="bestvideo[height<=480]+bestaudio/best[height<=480]",
                    description="480p",
                    quality_level="480",
                    requires_processing=True,
                ),
                FallbackOption(
                    format_string="best[height<=360]",
                    description="360p combined",
                    quality_level="360",
                    requires_processing=False,
                ),
            ],
            360: [
                FallbackOption(
                    format_string="best[height<=360]",
                    description="360p combined",
                    quality_level="360",
                    requires_processing=False,
                ),
                FallbackOption(
                    format_string="worst",
                    description="Lowest quality",
                    quality_level="lowest",
                    requires_processing=False,
                ),
            ],
        }

    def get_audio_fallback(self, attempt: int) -> Optional[FallbackOption]:
        if attempt < len(self.audio_fallbacks):
            return self.audio_fallbacks[attempt]
        return self.audio_fallbacks[-1] if self.audio_fallbacks else None

    def get_video_fallback(
        self,
        attempt: int,
        current_height: int,
    ) -> Optional[FallbackOption]:
        height_key = self._normalize_height(current_height)

        fallbacks = self.video_fallbacks.get(height_key, self.video_fallbacks[360])

        if attempt < len(fallbacks):
            return fallbacks[attempt]
        return fallbacks[-1] if fallbacks else None

    def _normalize_height(self, height: int) -> int:
        if height >= 1080:
            return 1080
        elif height >= 720:
            return 720
        elif height >= 480:
            return 480
        return 360

    def get_fallback_chain(
        self,
        media_type: MediaType,
        max_height: int = 720,
    ) -> List[FallbackOption]:
        if media_type == MediaType.AUDIO:
            return self.audio_fallbacks.copy()

        height_key = self._normalize_height(max_height)
        chain = []

        for h in [height_key, 720, 480, 360]:
            if h <= height_key and h in self.video_fallbacks:
                chain.extend(self.video_fallbacks[h])

        seen = set()
        unique_chain = []
        for opt in chain:
            if opt.format_string not in seen:
                seen.add(opt.format_string)
                unique_chain.append(opt)

        return unique_chain


fallback_policy = FallbackPolicy()
