"""Video processing utilities."""

import logging
from pathlib import Path
from typing import Optional

from AnonX_3.downloader_api.core.constants import VideoFormat, ProcessingMode, ResourceState
from AnonX_3.downloader_api.processing.ffmpeg_manager import ffmpeg_manager, FFmpegPreset
from AnonX_3.downloader_api.processing.ffprobe_manager import ffprobe_manager

logger = logging.getLogger(__name__)


class VideoProcessor:
    def __init__(self):
        pass

    async def process(
        self,
        input_path: Path,
        output_path: Path,
        target_format: VideoFormat = VideoFormat.MP4,
        target_height: Optional[int] = None,
        resource_state: ResourceState = ResourceState.NORMAL,
        job_id: Optional[str] = None,
    ) -> tuple[bool, ProcessingMode]:
        info = await ffprobe_manager.probe(input_path)

        if not info.is_valid or not info.has_video:
            logger.error(f"Invalid video input: {input_path}")
            return False, ProcessingMode.NONE

        input_ext = input_path.suffix.lower()
        output_ext = output_path.suffix.lower()

        needs_transcode = False

        if target_height and info.video_height:
            if info.video_height > target_height:
                needs_transcode = True

        if target_format != VideoFormat.ORIGINAL:
            target_ext = f".{target_format.value}"
            if input_ext != target_ext:
                if self._can_remux(info, target_format):
                    success = await ffmpeg_manager.remux_video(
                        input_path=input_path,
                        output_path=output_path,
                        container=target_format.value,
                        job_id=job_id,
                    )
                    return success, ProcessingMode.REMUX
                else:
                    needs_transcode = True

        if needs_transcode:
            preset = ffmpeg_manager.get_preset_for_state(resource_state)
            crf = ffmpeg_manager.get_crf_for_state(resource_state)
            height = target_height or info.video_height or 720

            success = await ffmpeg_manager.convert_video(
                input_path=input_path,
                output_path=output_path,
                height=height,
                preset=preset,
                crf=crf,
                job_id=job_id,
            )
            return success, ProcessingMode.REENCODE

        if input_ext != output_ext:
            success = await ffmpeg_manager.remux_video(
                input_path=input_path,
                output_path=output_path,
                job_id=job_id,
            )
            return success, ProcessingMode.REMUX

        return True, ProcessingMode.NONE

    async def merge_streams(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
        job_id: Optional[str] = None,
    ) -> tuple[bool, ProcessingMode]:
        success = await ffmpeg_manager.merge_video_audio(
            video_path=video_path,
            audio_path=audio_path,
            output_path=output_path,
            job_id=job_id,
        )
        return success, ProcessingMode.MERGE

    def _can_remux(self, info, target_format: VideoFormat) -> bool:
        if target_format == VideoFormat.MP4:
            compatible_video = info.video_codec in ("h264", "avc1", "hevc", "h265")
            compatible_audio = info.audio_codec in ("aac", "mp4a", "mp3") if info.has_audio else True
            return compatible_video and compatible_audio

        if target_format == VideoFormat.WEBM:
            compatible_video = info.video_codec in ("vp8", "vp9", "av1")
            compatible_audio = info.audio_codec in ("opus", "vorbis") if info.has_audio else True
            return compatible_video and compatible_audio

        if target_format == VideoFormat.MKV:
            return True

        return False

    def needs_processing(
        self,
        input_ext: str,
        target_format: VideoFormat,
        current_height: Optional[int],
        target_height: Optional[int],
    ) -> bool:
        if target_format not in (VideoFormat.ORIGINAL, VideoFormat.AUTO):
            target_ext = f".{target_format.value}"
            if input_ext.lower() != target_ext:
                return True

        if target_height and current_height:
            if current_height > target_height:
                return True

        return False


video_processor = VideoProcessor()
