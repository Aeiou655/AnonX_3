"""Quality manager for dynamic quality selection."""

import logging
from typing import Optional

from AnonX_3.downloader_api.core.constants import Quality, MediaType, ResourceState, QUALITY_TO_HEIGHT
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor

logger = logging.getLogger(__name__)


class QualityManager:
    def __init__(self):
        self.audio_bitrates = {
            ResourceState.IDLE: 192,
            ResourceState.NORMAL: 192,
            ResourceState.BUSY: 160,
            ResourceState.HIGH_LOAD: 128,
            ResourceState.CRITICAL: 128,
            ResourceState.RECOVERY: 160,
        }

        self.video_max_heights = {
            ResourceState.IDLE: 1080,
            ResourceState.NORMAL: 720,
            ResourceState.BUSY: 480,
            ResourceState.HIGH_LOAD: 360,
            ResourceState.CRITICAL: 360,
            ResourceState.RECOVERY: 480,
        }

    async def get_effective_quality(
        self,
        requested_quality: Quality,
        media_type: MediaType,
        resource_state: Optional[ResourceState] = None,
    ) -> int:
        if resource_state is None:
            resource_state = await resource_monitor.get_resource_state()

        if media_type == MediaType.AUDIO:
            return self.audio_bitrates.get(resource_state, 192)

        if requested_quality == Quality.AUTO:
            max_height = self.video_max_heights.get(resource_state, 720)
            return min(max_height, settings.video_auto_max_height)

        requested_height = QUALITY_TO_HEIGHT.get(requested_quality, 720)

        state_limit = self.video_max_heights.get(resource_state, 720)
        config_limit = settings.video_absolute_max_height

        return min(requested_height, state_limit, config_limit)

    async def get_audio_bitrate(
        self,
        resource_state: Optional[ResourceState] = None,
    ) -> int:
        if resource_state is None:
            resource_state = await resource_monitor.get_resource_state()

        return self.audio_bitrates.get(resource_state, settings.audio_mp3_bitrate)

    async def get_video_max_height(
        self,
        resource_state: Optional[ResourceState] = None,
    ) -> int:
        if resource_state is None:
            resource_state = await resource_monitor.get_resource_state()

        max_height = self.video_max_heights.get(resource_state, 720)
        return min(max_height, settings.video_auto_max_height)

    def quality_to_height(self, quality: Quality) -> int:
        return QUALITY_TO_HEIGHT.get(quality, 720)

    def height_to_quality_label(self, height: int) -> str:
        if height >= 1080:
            return "1080p"
        elif height >= 720:
            return "720p"
        elif height >= 480:
            return "480p"
        elif height >= 360:
            return "360p"
        else:
            return f"{height}p"


quality_manager = QualityManager()
