"""Converter for audio/video format conversion."""

import logging
from pathlib import Path
from typing import Optional

from AnonX_3.downloader_api.core.constants import ResourceState
from AnonX_3.downloader_api.processing.ffmpeg_manager import ffmpeg_manager

logger = logging.getLogger(__name__)


class Converter:
    async def convert_to_mp3(
        self,
        input_path: Path,
        output_path: Path,
        bitrate: int = 192,
        resource_state: ResourceState = ResourceState.NORMAL,
        job_id: Optional[str] = None,
    ) -> bool:
        adjusted_bitrate = self._adjust_bitrate(bitrate, resource_state)
        return await ffmpeg_manager.convert_to_mp3(
            input_path=input_path,
            output_path=output_path,
            bitrate=adjusted_bitrate,
            job_id=job_id,
        )

    async def convert_to_m4a(
        self,
        input_path: Path,
        output_path: Path,
        bitrate: int = 192,
        resource_state: ResourceState = ResourceState.NORMAL,
        job_id: Optional[str] = None,
    ) -> bool:
        adjusted_bitrate = self._adjust_bitrate(bitrate, resource_state)
        return await ffmpeg_manager.convert_to_m4a(
            input_path=input_path,
            output_path=output_path,
            bitrate=adjusted_bitrate,
            job_id=job_id,
        )

    async def convert_video(
        self,
        input_path: Path,
        output_path: Path,
        height: int = 720,
        resource_state: ResourceState = ResourceState.NORMAL,
        job_id: Optional[str] = None,
    ) -> bool:
        preset = ffmpeg_manager.get_preset_for_state(resource_state)
        crf = ffmpeg_manager.get_crf_for_state(resource_state)

        return await ffmpeg_manager.convert_video(
            input_path=input_path,
            output_path=output_path,
            height=height,
            preset=preset,
            crf=crf,
            job_id=job_id,
        )

    def _adjust_bitrate(self, bitrate: int, resource_state: ResourceState) -> int:
        adjustments = {
            ResourceState.IDLE: 1.0,
            ResourceState.NORMAL: 1.0,
            ResourceState.BUSY: 0.85,
            ResourceState.HIGH_LOAD: 0.7,
            ResourceState.CRITICAL: 0.6,
            ResourceState.RECOVERY: 0.85,
        }
        factor = adjustments.get(resource_state, 1.0)
        return int(bitrate * factor)


converter = Converter()
