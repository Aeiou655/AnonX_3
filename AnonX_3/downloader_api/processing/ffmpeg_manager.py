"""FFmpeg manager for media processing."""

import asyncio
import logging
import shutil
import os
from pathlib import Path
from typing import Optional, List
from enum import Enum

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import ProcessingMode, ResourceState
from AnonX_3.downloader_api.core.exceptions import ProcessingFailedError

logger = logging.getLogger(__name__)


class FFmpegPreset(str, Enum):
    ULTRAFAST = "ultrafast"
    SUPERFAST = "superfast"
    VERYFAST = "veryfast"
    FASTER = "faster"
    FAST = "fast"
    MEDIUM = "medium"


class FFmpegManager:
    def __init__(self):
        self.ffmpeg_path = self._find_ffmpeg()
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}

    def _find_ffmpeg(self) -> str:
        if settings.ffmpeg_path:
            return settings.ffmpeg_path

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            return ffmpeg

        return "ffmpeg"

    def is_available(self) -> bool:
        try:
            result = shutil.which(self.ffmpeg_path)
            return result is not None or Path(self.ffmpeg_path).exists()
        except Exception:
            return False

    async def convert_to_mp3(
        self,
        input_path: Path,
        output_path: Path,
        bitrate: int = 192,
        timeout: int = 900,
        job_id: Optional[str] = None,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", f"{bitrate}k",
            "-ar", "44100",
            "-ac", "2",
            str(output_path),
        ]

        return await self._execute(cmd, timeout, job_id)

    async def convert_to_m4a(
        self,
        input_path: Path,
        output_path: Path,
        bitrate: int = 192,
        timeout: int = 900,
        job_id: Optional[str] = None,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-vn",
            "-acodec", "aac",
            "-b:a", f"{bitrate}k",
            "-ar", "44100",
            "-ac", "2",
            str(output_path),
        ]

        return await self._execute(cmd, timeout, job_id)

    async def remux_audio(
        self,
        input_path: Path,
        output_path: Path,
        container: str = "m4a",
        timeout: int = 300,
        job_id: Optional[str] = None,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-vn",
            "-acodec", "copy",
            str(output_path),
        ]

        return await self._execute(cmd, timeout, job_id)

    async def merge_video_audio(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
        timeout: int = 900,
        job_id: Optional[str] = None,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c:v", "copy",
            "-c:a", "copy",
            "-shortest",
            str(output_path),
        ]

        return await self._execute(cmd, timeout, job_id)

    async def remux_video(
        self,
        input_path: Path,
        output_path: Path,
        container: str = "mp4",
        timeout: int = 600,
        job_id: Optional[str] = None,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-c:v", "copy",
            "-c:a", "copy",
            str(output_path),
        ]

        return await self._execute(cmd, timeout, job_id)

    async def convert_video(
        self,
        input_path: Path,
        output_path: Path,
        height: int = 720,
        preset: FFmpegPreset = FFmpegPreset.FAST,
        crf: int = 23,
        audio_bitrate: int = 128,
        timeout: int = 1800,
        job_id: Optional[str] = None,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-c:v", "libx264",
            "-preset", preset.value,
            "-crf", str(crf),
            "-vf", f"scale=-2:{height}",
            "-c:a", "aac",
            "-b:a", f"{audio_bitrate}k",
            "-movflags", "+faststart",
            str(output_path),
        ]

        return await self._execute(cmd, timeout, job_id)

    async def extract_audio(
        self,
        input_path: Path,
        output_path: Path,
        timeout: int = 300,
        job_id: Optional[str] = None,
    ) -> bool:
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-i", str(input_path),
            "-vn",
            "-acodec", "copy",
            str(output_path),
        ]

        return await self._execute(cmd, timeout, job_id)

    async def _execute(
        self,
        cmd: List[str],
        timeout: int,
        job_id: Optional[str] = None,
    ) -> bool:
        logger.debug(f"Executing FFmpeg: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            if job_id:
                self.active_processes[job_id] = process

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )

                if process.returncode != 0:
                    error_output = stderr.decode() if stderr else "Unknown error"
                    logger.error(f"FFmpeg failed: {error_output[:500]}")
                    return False

                return True

            except asyncio.TimeoutError:
                logger.error(f"FFmpeg timed out after {timeout}s")
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                return False

            finally:
                if job_id and job_id in self.active_processes:
                    del self.active_processes[job_id]

        except Exception as e:
            logger.error(f"FFmpeg execution error: {e}")
            return False

    async def cancel(self, job_id: str) -> bool:
        if job_id in self.active_processes:
            process = self.active_processes[job_id]
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
            del self.active_processes[job_id]
            return True
        return False

    def get_preset_for_state(self, resource_state: ResourceState) -> FFmpegPreset:
        mapping = {
            ResourceState.IDLE: FFmpegPreset.FAST,
            ResourceState.NORMAL: FFmpegPreset.FASTER,
            ResourceState.BUSY: FFmpegPreset.VERYFAST,
            ResourceState.HIGH_LOAD: FFmpegPreset.SUPERFAST,
            ResourceState.CRITICAL: FFmpegPreset.ULTRAFAST,
            ResourceState.RECOVERY: FFmpegPreset.VERYFAST,
        }
        return mapping.get(resource_state, FFmpegPreset.FAST)

    def get_crf_for_state(self, resource_state: ResourceState) -> int:
        mapping = {
            ResourceState.IDLE: 20,
            ResourceState.NORMAL: 23,
            ResourceState.BUSY: 26,
            ResourceState.HIGH_LOAD: 28,
            ResourceState.CRITICAL: 30,
            ResourceState.RECOVERY: 26,
        }
        return mapping.get(resource_state, 23)


ffmpeg_manager = FFmpegManager()
