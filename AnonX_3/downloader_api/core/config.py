"""Application configuration."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="Self-Hosted Downloader API")
    app_version: str = Field(default="1.0.0")
    environment: str = Field(default="production")
    debug: bool = Field(default=False)

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000)
    api_workers: int = Field(default=1)

    api_key: str = Field(default="")
    admin_api_key: str = Field(default="")

    base_dir: Path = Field(default_factory=lambda: Path(__file__).parent.parent.parent)
    data_dir: Path = Field(default=Path("./data"))
    audio_dir: Path = Field(default=Path("./data/audio"))
    video_dir: Path = Field(default=Path("./data/video"))
    temp_dir: Path = Field(default=Path("./data/temporary"))
    quarantine_dir: Path = Field(default=Path("./data/quarantine"))
    metadata_dir: Path = Field(default=Path("./data/metadata"))
    database_path: Path = Field(default=Path("./data/database/cache.db"))
    log_dir: Path = Field(default=Path("./logs"))

    audio_default_format: str = Field(default="original")
    audio_mp3_bitrate: int = Field(default=192)

    video_default_format: str = Field(default="mp4")
    video_auto_max_height: int = Field(default=720)
    video_absolute_max_height: int = Field(default=1080)

    max_metadata_workers: int = Field(default=4)
    max_audio_workers: int = Field(default=3)
    max_video_workers: int = Field(default=1)
    max_processing_workers: int = Field(default=1)
    max_queue_size: int = Field(default=50)

    dynamic_resource_control: bool = Field(default=True)

    cpu_normal_threshold: int = Field(default=60)
    cpu_busy_threshold: int = Field(default=78)
    cpu_critical_threshold: int = Field(default=90)

    memory_normal_threshold: int = Field(default=70)
    memory_busy_threshold: int = Field(default=82)
    memory_critical_threshold: int = Field(default=92)

    min_free_disk_gb: int = Field(default=5)
    disk_warning_percent: int = Field(default=20)
    disk_critical_percent: int = Field(default=5)

    http_retries: int = Field(default=8)
    fragment_retries: int = Field(default=8)
    extractor_retries: int = Field(default=4)
    file_access_retries: int = Field(default=3)

    socket_timeout_seconds: int = Field(default=30)
    metadata_timeout_seconds: int = Field(default=30)
    download_timeout_seconds: int = Field(default=900)
    processing_timeout_seconds: int = Field(default=900)

    cache_default_ttl_hours: int = Field(default=24)
    cache_popular_ttl_hours: int = Field(default=72)
    temp_ttl_minutes: int = Field(default=60)
    quarantine_ttl_hours: int = Field(default=6)
    cleanup_interval_minutes: int = Field(default=30)

    max_audio_duration_seconds: int = Field(default=14400)
    max_video_duration_seconds: int = Field(default=7200)
    max_audio_file_size_mb: int = Field(default=500)
    max_video_file_size_mb: int = Field(default=2000)
    max_url_length: int = Field(default=2048)

    rate_limit_enabled: bool = Field(default=True)
    rate_limit_requests: int = Field(default=30)
    rate_limit_window_seconds: int = Field(default=60)

    log_level: str = Field(default="INFO")
    log_json: bool = Field(default=True)

    ytdlp_path: Optional[str] = Field(default=None)
    ffmpeg_path: Optional[str] = Field(default=None)
    ffprobe_path: Optional[str] = Field(default=None)

    @field_validator("data_dir", "audio_dir", "video_dir", "temp_dir", "quarantine_dir", "metadata_dir", "log_dir", mode="before")
    @classmethod
    def resolve_path(cls, v: str | Path) -> Path:
        return Path(v).resolve()

    @field_validator("database_path", mode="before")
    @classmethod
    def resolve_db_path(cls, v: str | Path) -> Path:
        return Path(v).resolve()

    def ensure_directories(self) -> None:
        for dir_path in [
            self.data_dir,
            self.audio_dir,
            self.video_dir,
            self.temp_dir,
            self.quarantine_dir,
            self.metadata_dir,
            self.log_dir,
            self.database_path.parent,
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)

    @property
    def max_audio_file_size_bytes(self) -> int:
        return self.max_audio_file_size_mb * 1024 * 1024

    @property
    def max_video_file_size_bytes(self) -> int:
        return self.max_video_file_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
