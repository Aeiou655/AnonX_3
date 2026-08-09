"""Concurrency manager for resource-aware worker limits."""

import logging
from typing import Optional
from dataclasses import dataclass

from AnonX_3.downloader_api.core.constants import MediaType, ResourceState
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.dynamic.resource_monitor import resource_monitor

logger = logging.getLogger(__name__)


@dataclass
class ConcurrencyLimits:
    audio_workers: int
    video_workers: int
    processing_workers: int
    metadata_workers: int
    fragment_concurrency: int


class ConcurrencyManager:
    def __init__(self):
        self.limits_by_state = {
            ResourceState.IDLE: ConcurrencyLimits(
                audio_workers=4,
                video_workers=2,
                processing_workers=2,
                metadata_workers=4,
                fragment_concurrency=6,
            ),
            ResourceState.NORMAL: ConcurrencyLimits(
                audio_workers=3,
                video_workers=1,
                processing_workers=1,
                metadata_workers=4,
                fragment_concurrency=4,
            ),
            ResourceState.BUSY: ConcurrencyLimits(
                audio_workers=2,
                video_workers=1,
                processing_workers=1,
                metadata_workers=2,
                fragment_concurrency=2,
            ),
            ResourceState.HIGH_LOAD: ConcurrencyLimits(
                audio_workers=1,
                video_workers=0,
                processing_workers=1,
                metadata_workers=2,
                fragment_concurrency=1,
            ),
            ResourceState.CRITICAL: ConcurrencyLimits(
                audio_workers=1,
                video_workers=0,
                processing_workers=0,
                metadata_workers=1,
                fragment_concurrency=1,
            ),
            ResourceState.RECOVERY: ConcurrencyLimits(
                audio_workers=2,
                video_workers=1,
                processing_workers=1,
                metadata_workers=2,
                fragment_concurrency=2,
            ),
        }

    async def get_limits(
        self,
        resource_state: Optional[ResourceState] = None,
    ) -> ConcurrencyLimits:
        if resource_state is None:
            resource_state = await resource_monitor.get_resource_state()

        limits = self.limits_by_state.get(
            resource_state,
            self.limits_by_state[ResourceState.NORMAL],
        )

        return ConcurrencyLimits(
            audio_workers=min(limits.audio_workers, settings.max_audio_workers),
            video_workers=min(limits.video_workers, settings.max_video_workers),
            processing_workers=min(limits.processing_workers, settings.max_processing_workers),
            metadata_workers=min(limits.metadata_workers, settings.max_metadata_workers),
            fragment_concurrency=limits.fragment_concurrency,
        )

    async def get_audio_worker_limit(
        self,
        resource_state: Optional[ResourceState] = None,
    ) -> int:
        limits = await self.get_limits(resource_state)
        return limits.audio_workers

    async def get_video_worker_limit(
        self,
        resource_state: Optional[ResourceState] = None,
    ) -> int:
        limits = await self.get_limits(resource_state)
        return limits.video_workers

    async def get_fragment_concurrency(
        self,
        resource_state: Optional[ResourceState] = None,
    ) -> int:
        limits = await self.get_limits(resource_state)
        return limits.fragment_concurrency

    async def can_accept_job(
        self,
        media_type: MediaType,
        active_audio_jobs: int,
        active_video_jobs: int,
        resource_state: Optional[ResourceState] = None,
    ) -> bool:
        limits = await self.get_limits(resource_state)

        if media_type == MediaType.AUDIO:
            return active_audio_jobs < limits.audio_workers
        else:
            return active_video_jobs < limits.video_workers


concurrency_manager = ConcurrencyManager()
