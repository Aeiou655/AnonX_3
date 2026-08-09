"""Format selection logic."""

import logging
from typing import Optional, List, Tuple

from AnonX_3.downloader_api.core.constants import (
    MediaType, AudioFormat, VideoFormat, Quality,
    ResourceState, QUALITY_TO_HEIGHT,
)
from AnonX_3.downloader_api.core.config import settings

logger = logging.getLogger(__name__)


class FormatSelector:
    def __init__(self):
        self.audio_codec_preference = ["mp4a", "aac", "opus", "vorbis", "mp3"]
        self.video_codec_preference = ["avc1", "h264", "vp9", "av01"]
        self.container_preference_audio = ["m4a", "webm", "mp3", "ogg"]
        self.container_preference_video = ["mp4", "webm", "mkv"]

    def select_audio_format(
        self,
        formats: List[dict],
        format_preference: str = "original",
        resource_state: ResourceState = ResourceState.NORMAL,
    ) -> Optional[dict]:
        audio_formats = self._filter_audio_formats(formats)

        if not audio_formats:
            logger.warning("No audio formats available")
            return None

        audio_formats = self._sort_audio_formats(audio_formats)

        if format_preference in ("original", "auto"):
            return audio_formats[0] if audio_formats else None

        if format_preference == "m4a":
            m4a_formats = [f for f in audio_formats if f.get("ext") == "m4a"]
            if m4a_formats:
                return m4a_formats[0]

        if format_preference == "opus":
            opus_formats = [f for f in audio_formats if f.get("acodec") == "opus"]
            if opus_formats:
                return opus_formats[0]

        return audio_formats[0] if audio_formats else None

    def select_video_format(
        self,
        formats: List[dict],
        quality: Quality = Quality.AUTO,
        format_preference: str = "mp4",
        resource_state: ResourceState = ResourceState.NORMAL,
    ) -> Tuple[Optional[dict], Optional[dict]]:
        max_height = self._get_max_height(quality, resource_state)

        combined = self._find_combined_format(formats, max_height, format_preference)
        if combined:
            return combined, None

        video_format = self._select_video_stream(formats, max_height, format_preference)
        audio_format = self._select_audio_for_video(formats)

        return video_format, audio_format

    def _get_max_height(
        self,
        quality: Quality,
        resource_state: ResourceState,
    ) -> int:
        if quality == Quality.AUTO:
            state_limits = {
                ResourceState.IDLE: 1080,
                ResourceState.NORMAL: 720,
                ResourceState.BUSY: 480,
                ResourceState.HIGH_LOAD: 360,
                ResourceState.CRITICAL: 360,
                ResourceState.RECOVERY: 480,
            }
            return min(
                state_limits.get(resource_state, 720),
                settings.video_auto_max_height,
            )

        if quality in QUALITY_TO_HEIGHT:
            return QUALITY_TO_HEIGHT[quality]

        return settings.video_auto_max_height

    def _filter_audio_formats(self, formats: List[dict]) -> List[dict]:
        audio_formats = []
        for f in formats:
            if f.get("vcodec") in ("none", None) and f.get("acodec") not in ("none", None):
                audio_formats.append(f)
            elif f.get("audio_ext") not in ("none", None) and f.get("video_ext") in ("none", None):
                audio_formats.append(f)
            elif f.get("resolution") == "audio only":
                audio_formats.append(f)
        return audio_formats

    def _sort_audio_formats(self, formats: List[dict]) -> List[dict]:
        def score(f: dict) -> tuple:
            abr = f.get("abr") or f.get("tbr") or 0
            ext = f.get("ext", "")
            acodec = f.get("acodec", "")

            ext_score = 0
            for i, pref in enumerate(self.container_preference_audio):
                if ext == pref:
                    ext_score = len(self.container_preference_audio) - i
                    break

            codec_score = 0
            for i, pref in enumerate(self.audio_codec_preference):
                if pref in acodec:
                    codec_score = len(self.audio_codec_preference) - i
                    break

            return (abr, ext_score, codec_score)

        return sorted(formats, key=score, reverse=True)

    def _find_combined_format(
        self,
        formats: List[dict],
        max_height: int,
        format_preference: str,
    ) -> Optional[dict]:
        combined = []
        for f in formats:
            height = f.get("height") or 0
            vcodec = f.get("vcodec")
            acodec = f.get("acodec")

            if vcodec and vcodec != "none" and acodec and acodec != "none":
                if height <= max_height:
                    combined.append(f)

        if not combined:
            return None

        def score(f: dict) -> tuple:
            height = f.get("height") or 0
            ext = f.get("ext", "")
            ext_match = 1 if ext == format_preference else 0
            tbr = f.get("tbr") or 0
            return (height, ext_match, tbr)

        combined.sort(key=score, reverse=True)
        return combined[0] if combined else None

    def _select_video_stream(
        self,
        formats: List[dict],
        max_height: int,
        format_preference: str,
    ) -> Optional[dict]:
        video_only = []
        for f in formats:
            vcodec = f.get("vcodec")
            acodec = f.get("acodec")
            height = f.get("height") or 0

            if vcodec and vcodec != "none":
                if acodec in ("none", None) or not acodec:
                    if height <= max_height:
                        video_only.append(f)

        if not video_only:
            for f in formats:
                vcodec = f.get("vcodec")
                height = f.get("height") or 0
                if vcodec and vcodec != "none" and height <= max_height:
                    video_only.append(f)

        if not video_only:
            return None

        def score(f: dict) -> tuple:
            height = f.get("height") or 0
            ext = f.get("ext", "")
            vcodec = f.get("vcodec", "")

            ext_match = 1 if ext == format_preference else 0

            codec_score = 0
            for i, pref in enumerate(self.video_codec_preference):
                if pref in vcodec:
                    codec_score = len(self.video_codec_preference) - i
                    break

            vbr = f.get("vbr") or f.get("tbr") or 0
            return (height, ext_match, codec_score, vbr)

        video_only.sort(key=score, reverse=True)
        return video_only[0] if video_only else None

    def _select_audio_for_video(self, formats: List[dict]) -> Optional[dict]:
        audio_formats = self._filter_audio_formats(formats)
        if not audio_formats:
            return None

        m4a_formats = [f for f in audio_formats if f.get("ext") == "m4a"]
        if m4a_formats:
            return self._sort_audio_formats(m4a_formats)[0]

        return self._sort_audio_formats(audio_formats)[0]

    def build_format_string(
        self,
        video_format: Optional[dict],
        audio_format: Optional[dict],
    ) -> str:
        if video_format and audio_format:
            return f"{video_format['format_id']}+{audio_format['format_id']}"
        elif video_format:
            return video_format["format_id"]
        elif audio_format:
            return audio_format["format_id"]
        return "bestaudio/best"

    def estimate_file_size(
        self,
        formats: List[dict],
        duration: Optional[int],
        media_type: MediaType,
    ) -> Optional[int]:
        if not duration:
            return None

        for f in formats:
            filesize = f.get("filesize") or f.get("filesize_approx")
            if filesize:
                return filesize

        for f in formats:
            tbr = f.get("tbr")
            if tbr:
                return int((tbr * 1000 / 8) * duration)

        if media_type == MediaType.AUDIO:
            return int(192 * 1000 / 8 * duration)
        else:
            return int(2000 * 1000 / 8 * duration)


format_selector = FormatSelector()
