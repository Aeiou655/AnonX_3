"""Remuxer for container format conversion without re-encoding."""

import logging
from pathlib import Path
from typing import Optional

from AnonX_3.downloader_api.processing.ffmpeg_manager import ffmpeg_manager

logger = logging.getLogger(__name__)


class Remuxer:
    async def remux_audio(
        self,
        input_path: Path,
        output_path: Path,
        container: str = "m4a",
        job_id: Optional[str] = None,
    ) -> bool:
        return await ffmpeg_manager.remux_audio(
            input_path=input_path,
            output_path=output_path,
            container=container,
            job_id=job_id,
        )

    async def remux_video(
        self,
        input_path: Path,
        output_path: Path,
        container: str = "mp4",
        job_id: Optional[str] = None,
    ) -> bool:
        return await ffmpeg_manager.remux_video(
            input_path=input_path,
            output_path=output_path,
            container=container,
            job_id=job_id,
        )


remuxer = Remuxer()
