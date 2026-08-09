"""Decision engine for dynamic resource-aware decisions."""

import logging
from typing import Optional, Tuple
from dataclasses import dataclass

from AnonX_3.downloader_api.core.constants import (
    MediaType, Quality, ResourceState, DiskState,
    JobPriority, ProcessingMode, SHORT_AUDIO_DURATION, SHORT_VIDEO_DURATION,
)
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor, ResourceSnapshot

logger = logging.getLogger(__name__)


@dataclass
class DownloadDecision:
    should_proceed: bool
    max_quality: int
    allow_conversion: bool
    allow_reencode: bool
    priority: JobPriority
    reason: Optional[str] = None
    queued: bool = False
    estimated_wait: Optional[int] = None


class DecisionEngine:
    def __init__(self):
        pass

    async def should_accept_download(
        self,
        media_type: MediaType,
        duration: Optional[int],
        estimated_size: Optional[int],
        requested_quality: Quality,
        queue_length: int,
    ) -> DownloadDecision:
        snapshot = await resource_monitor.get_snapshot()

        if snapshot.resource_state == ResourceState.CRITICAL:
            if media_type == MediaType.VIDEO:
                return DownloadDecision(
                    should_proceed=False,
                    max_quality=0,
                    allow_conversion=False,
                    allow_reencode=False,
                    priority=JobPriority.LARGE_MEDIA,
                    reason="System under critical load, video downloads paused",
                )

            if duration and duration > 600:
                return DownloadDecision(
                    should_proceed=False,
                    max_quality=0,
                    allow_conversion=False,
                    allow_reencode=False,
                    priority=JobPriority.NORMAL_AUDIO,
                    reason="System under critical load, large audio downloads paused",
                )

        if snapshot.disk_state == DiskState.CRITICAL:
            return DownloadDecision(
                should_proceed=False,
                max_quality=0,
                allow_conversion=False,
                allow_reencode=False,
                priority=JobPriority.LARGE_MEDIA,
                reason="Insufficient disk space",
            )

        if estimated_size:
            required_space = estimated_size * 2.5
            if snapshot.disk_free_gb * (1024 ** 3) < required_space:
                return DownloadDecision(
                    should_proceed=False,
                    max_quality=0,
                    allow_conversion=False,
                    allow_reencode=False,
                    priority=JobPriority.LARGE_MEDIA,
                    reason="Insufficient disk space for this download",
                )

        if queue_length >= settings.max_queue_size:
            return DownloadDecision(
                should_proceed=False,
                max_quality=0,
                allow_conversion=False,
                allow_reencode=False,
                priority=JobPriority.LARGE_MEDIA,
                reason="Download queue is full",
                queued=False,
            )

        max_quality = self._get_max_quality(snapshot.resource_state, media_type)
        allow_conversion = self._allow_conversion(snapshot.resource_state)
        allow_reencode = self._allow_reencode(snapshot.resource_state)
        priority = self._calculate_priority(media_type, duration, estimated_size)

        return DownloadDecision(
            should_proceed=True,
            max_quality=max_quality,
            allow_conversion=allow_conversion,
            allow_reencode=allow_reencode,
            priority=priority,
        )

    def _get_max_quality(
        self,
        resource_state: ResourceState,
        media_type: MediaType,
    ) -> int:
        if media_type == MediaType.AUDIO:
            return 0

        quality_limits = {
            ResourceState.IDLE: 1080,
            ResourceState.NORMAL: 720,
            ResourceState.BUSY: 480,
            ResourceState.HIGH_LOAD: 360,
            ResourceState.CRITICAL: 360,
            ResourceState.RECOVERY: 480,
        }

        return min(
            quality_limits.get(resource_state, 720),
            settings.video_auto_max_height,
        )

    def _allow_conversion(self, resource_state: ResourceState) -> bool:
        return resource_state not in (
            ResourceState.CRITICAL,
            ResourceState.HIGH_LOAD,
        )

    def _allow_reencode(self, resource_state: ResourceState) -> bool:
        return resource_state in (
            ResourceState.IDLE,
            ResourceState.NORMAL,
        )

    def _calculate_priority(
        self,
        media_type: MediaType,
        duration: Optional[int],
        estimated_size: Optional[int],
    ) -> JobPriority:
        if media_type == MediaType.AUDIO:
            if duration and duration <= SHORT_AUDIO_DURATION:
                return JobPriority.SHORT_AUDIO
            return JobPriority.NORMAL_AUDIO

        if duration and duration <= SHORT_VIDEO_DURATION:
            return JobPriority.SHORT_VIDEO

        if estimated_size and estimated_size > 500 * 1024 * 1024:
            return JobPriority.LARGE_MEDIA

        return JobPriority.NORMAL_VIDEO

    async def get_processing_mode(
        self,
        input_format: str,
        output_format: str,
        needs_merge: bool,
        resource_state: Optional[ResourceState] = None,
    ) -> ProcessingMode:
        if resource_state is None:
            resource_state = await resource_monitor.get_resource_state()

        if needs_merge:
            return ProcessingMode.MERGE

        if input_format == output_format:
            return ProcessingMode.NONE

        compatible_remux = self._can_remux(input_format, output_format)

        if compatible_remux:
            return ProcessingMode.REMUX

        if resource_state in (ResourceState.IDLE, ResourceState.NORMAL):
            return ProcessingMode.CONVERT
        elif resource_state == ResourceState.BUSY:
            return ProcessingMode.REMUX
        else:
            return ProcessingMode.NONE

    def _can_remux(self, input_format: str, output_format: str) -> bool:
        remux_compatible = {
            ("webm", "mkv"), ("mkv", "webm"),
            ("m4a", "mp4"), ("mp4", "m4a"),
        }

        return (input_format, output_format) in remux_compatible


decision_engine = DecisionEngine()
