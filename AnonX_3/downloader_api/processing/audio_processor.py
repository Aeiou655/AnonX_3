"""Audio processing utilities."""

import logging
from pathlib import Path
from typing import Optional

from AnonX_3.downloader_api.core.constants import AudioFormat, ProcessingMode, ResourceState
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.processing.ffmpeg_manager import ffmpeg_manager
from AnonX_3.downloader_api.processing.ffprobe_manager import ffprobe_manager

logger = logging.getLogger(__name__)


class AudioProcessor:
    def __init__(self):
        pass

    async def process(
        self,
        input_path: Path,
        output_path: Path,
        target_format: AudioFormat,
        resource_state: ResourceState = ResourceState.NORMAL,
        job_id: Optional[str] = None,
    ) -> tuple[bool, ProcessingMode]:
        info = await ffprobe_manager.probe(input_path)

        if not info.is_valid or not info.has_audio:
            logger.error(f"Invalid input file: {input_path}")
            return False, ProcessingMode.NONE

        input_ext = input_path.suffix.lower()
        output_ext = output_path.suffix.lower()

        if target_format in (AudioFormat.ORIGINAL, AudioFormat.AUTO):
            if input_ext != output_ext:
                success = await ffmpeg_manager.remux_audio(
                    input_path=input_path,
                    output_path=output_path,
                    job_id=job_id,
                )
                return success, ProcessingMode.REMUX
            return True, ProcessingMode.NONE

        if target_format == AudioFormat.MP3:
            bitrate = self._get_mp3_bitrate(resource_state)
            success = await ffmpeg_manager.convert_to_mp3(
                input_path=input_path,
                output_path=output_path,
                bitrate=bitrate,
                job_id=job_id,
            )
            return success, ProcessingMode.CONVERT

        if target_format == AudioFormat.M4A:
            if info.audio_codec in ("aac", "mp4a"):
                success = await ffmpeg_manager.remux_audio(
                    input_path=input_path,
                    output_path=output_path,
                    container="m4a",
                    job_id=job_id,
                )
                return success, ProcessingMode.REMUX
            else:
                bitrate = self._get_aac_bitrate(resource_state)
                success = await ffmpeg_manager.convert_to_m4a(
                    input_path=input_path,
                    output_path=output_path,
                    bitrate=bitrate,
                    job_id=job_id,
                )
                return success, ProcessingMode.CONVERT

        if target_format == AudioFormat.OPUS:
            if info.audio_codec == "opus" and input_ext == ".webm":
                success = await ffmpeg_manager.remux_audio(
                    input_path=input_path,
                    output_path=output_path,
                    job_id=job_id,
                )
                return success, ProcessingMode.REMUX

        return True, ProcessingMode.NONE

    def _get_mp3_bitrate(self, resource_state: ResourceState) -> int:
        bitrates = {
            ResourceState.IDLE: 192,
            ResourceState.NORMAL: 192,
            ResourceState.BUSY: 160,
            ResourceState.HIGH_LOAD: 128,
            ResourceState.CRITICAL: 128,
            ResourceState.RECOVERY: 160,
        }
        return bitrates.get(resource_state, settings.audio_mp3_bitrate)

    def _get_aac_bitrate(self, resource_state: ResourceState) -> int:
        return self._get_mp3_bitrate(resource_state)

    def needs_processing(
        self,
        input_ext: str,
        target_format: AudioFormat,
    ) -> bool:
        if target_format in (AudioFormat.ORIGINAL, AudioFormat.AUTO):
            return False

        format_to_ext = {
            AudioFormat.MP3: ".mp3",
            AudioFormat.M4A: ".m4a",
            AudioFormat.OPUS: ".opus",
            AudioFormat.AAC: ".m4a",
            AudioFormat.WEBM: ".webm",
        }

        target_ext = format_to_ext.get(target_format)
        return target_ext is not None and input_ext.lower() != target_ext


audio_processor = AudioProcessor()
