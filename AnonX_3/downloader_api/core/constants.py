"""Application constants."""

from enum import Enum, auto
from typing import Final


class MediaType(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"


class AudioFormat(str, Enum):
    AUTO = "auto"
    ORIGINAL = "original"
    MP3 = "mp3"
    M4A = "m4a"
    OPUS = "opus"
    AAC = "aac"
    WEBM = "webm"


class VideoFormat(str, Enum):
    AUTO = "auto"
    ORIGINAL = "original"
    MP4 = "mp4"
    WEBM = "webm"
    MKV = "mkv"


class Quality(str, Enum):
    AUTO = "auto"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    Q360 = "360"
    Q480 = "480"
    Q720 = "720"
    Q1080 = "1080"


class ResourceState(str, Enum):
    IDLE = "idle"
    NORMAL = "normal"
    BUSY = "busy"
    HIGH_LOAD = "high_load"
    CRITICAL = "critical"
    RECOVERY = "recovery"


class JobState(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    CACHE_CHECK = "cache_check"
    QUEUED = "queued"
    EXTRACTING = "extracting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    VALIDATING_FILE = "validating_file"
    SAVING = "saving"
    READY = "ready"
    STREAMING = "streaming"
    COMPLETED = "completed"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class JobPriority(int, Enum):
    INTERNAL = 0
    CACHE_HIT = 1
    SHORT_AUDIO = 2
    NORMAL_AUDIO = 3
    SHORT_VIDEO = 4
    NORMAL_VIDEO = 5
    CONVERSION_HEAVY = 6
    LARGE_MEDIA = 7


class ProcessingMode(str, Enum):
    NONE = "none"
    REMUX = "remux"
    MERGE = "merge"
    CONVERT = "convert"
    REENCODE = "reencode"


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BUSY = "busy"
    CRITICAL = "critical"


class CacheStatus(str, Enum):
    HIT = "hit"
    MISS = "miss"
    EXPIRED = "expired"
    INVALID = "invalid"


class DiskState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    HIGH_PRESSURE = "high_pressure"
    CRITICAL = "critical"


class ValidationState(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    QUARANTINED = "quarantined"
    PENDING = "pending"


QUALITY_TO_HEIGHT: Final[dict[Quality, int]] = {
    Quality.LOW: 360,
    Quality.MEDIUM: 480,
    Quality.HIGH: 720,
    Quality.Q360: 360,
    Quality.Q480: 480,
    Quality.Q720: 720,
    Quality.Q1080: 1080,
}

SUPPORTED_SOURCES: Final[set[str]] = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "m.youtube.com",
    "music.youtube.com",
}

AUDIO_EXTENSIONS: Final[set[str]] = {".mp3", ".m4a", ".opus", ".aac", ".webm", ".ogg", ".wav", ".flac"}
VIDEO_EXTENSIONS: Final[set[str]] = {".mp4", ".webm", ".mkv", ".avi", ".mov"}

AUDIO_MIME_TYPES: Final[dict[str, str]] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/opus",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}

VIDEO_MIME_TYPES: Final[dict[str, str]] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
}

MAX_RETRIES: Final[int] = 5
DEFAULT_SOCKET_TIMEOUT: Final[int] = 30
DEFAULT_METADATA_TIMEOUT: Final[int] = 30
DEFAULT_DOWNLOAD_TIMEOUT: Final[int] = 900
DEFAULT_PROCESSING_TIMEOUT: Final[int] = 900

SHORT_AUDIO_DURATION: Final[int] = 300
SHORT_VIDEO_DURATION: Final[int] = 180

CHUNK_SIZE: Final[int] = 256 * 1024
