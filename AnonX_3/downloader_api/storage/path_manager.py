"""Path management utilities."""

from pathlib import Path
from typing import Optional

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import MediaType


class PathManager:
    def __init__(self):
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        settings.ensure_directories()

    @property
    def data_dir(self) -> Path:
        return settings.data_dir

    @property
    def audio_dir(self) -> Path:
        return settings.audio_dir

    @property
    def video_dir(self) -> Path:
        return settings.video_dir

    @property
    def temp_dir(self) -> Path:
        return settings.temp_dir

    @property
    def quarantine_dir(self) -> Path:
        return settings.quarantine_dir

    @property
    def metadata_dir(self) -> Path:
        return settings.metadata_dir

    @property
    def database_path(self) -> Path:
        return settings.database_path

    def get_cache_dir(self, media_type: MediaType) -> Path:
        if media_type == MediaType.AUDIO:
            return self.audio_dir
        return self.video_dir

    def get_cache_subdir(self, cache_key: str) -> str:
        return cache_key[:2]

    def get_cache_path(
        self,
        cache_key: str,
        extension: str,
        media_type: MediaType,
    ) -> Path:
        base_dir = self.get_cache_dir(media_type)
        subdir = self.get_cache_subdir(cache_key)

        if not extension.startswith("."):
            extension = f".{extension}"

        cache_dir = base_dir / subdir
        cache_dir.mkdir(parents=True, exist_ok=True)

        return cache_dir / f"{cache_key}{extension}"

    def get_temp_job_dir(self, job_id: str) -> Path:
        job_dir = self.temp_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir

    def get_temp_download_path(self, job_id: str, extension: str = ".part") -> Path:
        job_dir = self.get_temp_job_dir(job_id)
        return job_dir / f"source{extension}"

    def get_temp_output_path(self, job_id: str, extension: str = ".tmp") -> Path:
        job_dir = self.get_temp_job_dir(job_id)
        return job_dir / f"output{extension}"

    def get_quarantine_path(self, job_id: str, extension: str) -> Path:
        if not extension.startswith("."):
            extension = f".{extension}"
        return self.quarantine_dir / f"{job_id}{extension}"

    def get_metadata_path(self, video_id: str) -> Path:
        return self.metadata_dir / f"{video_id}.json"

    def cleanup_temp_job(self, job_id: str) -> bool:
        import shutil
        job_dir = self.temp_dir / job_id
        if job_dir.exists():
            try:
                shutil.rmtree(job_dir)
                return True
            except Exception:
                return False
        return True

    def list_temp_jobs(self) -> list[str]:
        if not self.temp_dir.exists():
            return []
        return [d.name for d in self.temp_dir.iterdir() if d.is_dir()]

    def get_cache_files(self, media_type: MediaType) -> list[Path]:
        cache_dir = self.get_cache_dir(media_type)
        if not cache_dir.exists():
            return []

        files = []
        for subdir in cache_dir.iterdir():
            if subdir.is_dir():
                files.extend(f for f in subdir.iterdir() if f.is_file())
        return files


path_manager = PathManager()
