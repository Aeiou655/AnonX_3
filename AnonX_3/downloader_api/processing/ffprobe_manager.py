"""FFprobe manager for media validation."""

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Optional, Any
from dataclasses import dataclass

from AnonX_3.downloader_api.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class MediaInfo:
    duration: Optional[float] = None
    format_name: Optional[str] = None
    format_long_name: Optional[str] = None
    size: Optional[int] = None
    bit_rate: Optional[int] = None
    has_audio: bool = False
    has_video: bool = False
    audio_codec: Optional[str] = None
    video_codec: Optional[str] = None
    audio_channels: Optional[int] = None
    audio_sample_rate: Optional[int] = None
    video_width: Optional[int] = None
    video_height: Optional[int] = None
    video_fps: Optional[float] = None
    is_valid: bool = False
    error: Optional[str] = None


class FFprobeManager:
    def __init__(self):
        self.ffprobe_path = self._find_ffprobe()

    def _find_ffprobe(self) -> str:
        if settings.ffprobe_path:
            return settings.ffprobe_path

        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            return ffprobe

        return "ffprobe"

    async def probe(
        self,
        file_path: Path,
        timeout: int = 30,
    ) -> MediaInfo:
        if not file_path.exists():
            return MediaInfo(is_valid=False, error="File does not exist")

        cmd = [
            self.ffprobe_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )

            if process.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "FFprobe failed"
                return MediaInfo(is_valid=False, error=error_msg)

            data = json.loads(stdout.decode())
            return self._parse_probe_data(data)

        except asyncio.TimeoutError:
            return MediaInfo(is_valid=False, error="FFprobe timed out")
        except json.JSONDecodeError as e:
            return MediaInfo(is_valid=False, error=f"Invalid JSON output: {e}")
        except Exception as e:
            logger.error(f"FFprobe error: {e}")
            return MediaInfo(is_valid=False, error=str(e))

    def _parse_probe_data(self, data: dict[str, Any]) -> MediaInfo:
        info = MediaInfo(is_valid=True)

        format_info = data.get("format", {})
        info.format_name = format_info.get("format_name")
        info.format_long_name = format_info.get("format_long_name")
        info.size = int(format_info.get("size", 0)) if format_info.get("size") else None
        info.bit_rate = int(format_info.get("bit_rate", 0)) if format_info.get("bit_rate") else None

        duration_str = format_info.get("duration")
        if duration_str:
            try:
                info.duration = float(duration_str)
            except ValueError:
                pass

        streams = data.get("streams", [])
        for stream in streams:
            codec_type = stream.get("codec_type")

            if codec_type == "audio":
                info.has_audio = True
                info.audio_codec = stream.get("codec_name")
                info.audio_channels = stream.get("channels")
                sample_rate = stream.get("sample_rate")
                if sample_rate:
                    try:
                        info.audio_sample_rate = int(sample_rate)
                    except ValueError:
                        pass

            elif codec_type == "video":
                codec_name = stream.get("codec_name", "").lower()
                if codec_name not in ("mjpeg", "png", "gif"):
                    info.has_video = True
                    info.video_codec = stream.get("codec_name")
                    info.video_width = stream.get("width")
                    info.video_height = stream.get("height")

                    fps_str = stream.get("r_frame_rate", "0/1")
                    try:
                        num, den = fps_str.split("/")
                        if int(den) > 0:
                            info.video_fps = int(num) / int(den)
                    except (ValueError, ZeroDivisionError):
                        pass

        return info

    async def get_duration(self, file_path: Path) -> Optional[float]:
        info = await self.probe(file_path)
        return info.duration if info.is_valid else None

    async def has_audio_stream(self, file_path: Path) -> bool:
        info = await self.probe(file_path)
        return info.has_audio

    async def has_video_stream(self, file_path: Path) -> bool:
        info = await self.probe(file_path)
        return info.has_video

    async def is_valid(self, file_path: Path) -> bool:
        info = await self.probe(file_path)
        return info.is_valid

    def is_available(self) -> bool:
        try:
            result = shutil.which(self.ffprobe_path)
            return result is not None or Path(self.ffprobe_path).exists()
        except Exception:
            return False


ffprobe_manager = FFprobeManager()
