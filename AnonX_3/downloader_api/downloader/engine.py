"""Download engine - core download logic using yt-dlp."""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Optional, Callable, Any
from datetime import datetime

import yt_dlp

from AnonX_3.core.ytdlp_runtime import create_youtube_dl
from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import (
    MediaType, ResourceState, ProcessingMode, JobState,
)
from AnonX_3.downloader_api.core.exceptions import (
    DownloadFailedError,
    DownloadTimeoutError,
    ValidationFailedError,
)
from AnonX_3.downloader_api.downloader.yt_dlp_options import (
    get_download_options,
    get_audio_format_string,
    get_video_format_string,
    get_retry_options,
)
from AnonX_3.downloader_api.downloader.format_selector import format_selector
from AnonX_3.downloader_api.downloader.retry_policy import retry_policy, RetryAttempt
from AnonX_3.downloader_api.downloader.fallback_policy import fallback_policy
from AnonX_3.downloader_api.downloader.progress_hook import ProgressHook, DownloadProgress
from AnonX_3.downloader_api.downloader.extractor import metadata_extractor
from AnonX_3.downloader_api.schemas.job import Job
from AnonX_3.downloader_api.storage.path_manager import path_manager

logger = logging.getLogger(__name__)


class DownloadResult:
    def __init__(
        self,
        success: bool,
        file_path: Optional[Path] = None,
        file_size: int = 0,
        extension: str = "",
        processing_mode: ProcessingMode = ProcessingMode.NONE,
        selected_format: Optional[str] = None,
        selected_quality: Optional[str] = None,
        error: Optional[str] = None,
    ):
        self.success = success
        self.file_path = file_path
        self.file_size = file_size
        self.extension = extension
        self.processing_mode = processing_mode
        self.selected_format = selected_format
        self.selected_quality = selected_quality
        self.error = error


class DownloadEngine:
    def __init__(self):
        self.active_downloads: dict[str, asyncio.Task] = {}

    async def download(
        self,
        job: Job,
        resource_state: ResourceState = ResourceState.NORMAL,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    ) -> DownloadResult:
        job_dir = path_manager.get_temp_job_dir(job.job_id)

        try:
            if job.media_type == MediaType.AUDIO:
                return await self._download_audio(
                    job=job,
                    job_dir=job_dir,
                    resource_state=resource_state,
                    progress_callback=progress_callback,
                )
            else:
                return await self._download_video(
                    job=job,
                    job_dir=job_dir,
                    resource_state=resource_state,
                    progress_callback=progress_callback,
                )
        except asyncio.CancelledError:
            logger.warning(f"Download cancelled: {job.job_id}")
            self._cleanup_job_dir(job_dir)
            raise
        except Exception as e:
            logger.error(f"Download failed: {job.job_id} - {e}")
            return DownloadResult(success=False, error=str(e))

    async def _download_audio(
        self,
        job: Job,
        job_dir: Path,
        resource_state: ResourceState,
        progress_callback: Optional[Callable] = None,
    ) -> DownloadResult:
        format_string = get_audio_format_string(
            format_preference=job.format,
            quality=job.quality,
        )

        output_template = str(job_dir / "audio.%(ext)s")

        progress_hook = ProgressHook(job.job_id, progress_callback)

        opts = get_download_options(
            output_path=Path(output_template),
            format_id=format_string,
            resource_state=resource_state,
            progress_hook=progress_hook,
        )

        attempt = 0
        last_error: Optional[Exception] = None

        while attempt < job.max_attempts:
            try:
                result = await self._execute_download(
                    url=job.normalized_url,
                    opts=opts,
                    timeout=settings.download_timeout_seconds,
                )

                output_file = self._find_output_file(job_dir, "audio")
                if output_file and output_file.exists():
                    return DownloadResult(
                        success=True,
                        file_path=output_file,
                        file_size=output_file.stat().st_size,
                        extension=output_file.suffix,
                        processing_mode=ProcessingMode.NONE,
                        selected_format=result.get("ext", job.format),
                        selected_quality="auto",
                    )

                raise DownloadFailedError("Output file not found after download")

            except Exception as e:
                last_error = e
                attempt += 1

                retry = retry_policy.should_retry(attempt, e)
                if not retry:
                    break

                logger.warning(
                    f"Retry {attempt}/{job.max_attempts} for {job.job_id}: {retry.reason}",
                    extra={"job_id": job.job_id, "attempt": attempt},
                )

                if retry.should_refresh_formats:
                    fallback = fallback_policy.get_audio_fallback(attempt)
                    if fallback:
                        opts["format"] = fallback.format_string

                opts.update(get_retry_options(attempt, job.max_attempts))

                await asyncio.sleep(retry.delay_seconds)

        raise DownloadFailedError(f"Download failed after {attempt} attempts: {last_error}")

    async def _download_video(
        self,
        job: Job,
        job_dir: Path,
        resource_state: ResourceState,
        progress_callback: Optional[Callable] = None,
    ) -> DownloadResult:
        metadata = await metadata_extractor.extract(
            url=job.normalized_url,
            video_id=job.video_id,
            use_cache=True,
        )

        video_format, audio_format = format_selector.select_video_format(
            formats=metadata.formats,
            quality=job.quality,
            format_preference=job.format,
            resource_state=resource_state,
        )

        if video_format and audio_format:
            format_string = format_selector.build_format_string(video_format, audio_format)
            processing_mode = ProcessingMode.MERGE
            selected_quality = str(video_format.get("height", "auto"))
        elif video_format:
            format_string = video_format["format_id"]
            processing_mode = ProcessingMode.NONE
            selected_quality = str(video_format.get("height", "auto"))
        else:
            format_string = get_video_format_string(
                max_height=720,
                format_preference=job.format,
            )
            processing_mode = ProcessingMode.MERGE
            selected_quality = "auto"

        output_template = str(job_dir / "video.%(ext)s")

        progress_hook = ProgressHook(job.job_id, progress_callback)

        opts = get_download_options(
            output_path=Path(output_template),
            format_id=format_string,
            resource_state=resource_state,
            progress_hook=progress_hook,
        )

        if processing_mode == ProcessingMode.MERGE:
            opts["merge_output_format"] = "mp4"

        attempt = 0
        last_error: Optional[Exception] = None

        while attempt < job.max_attempts:
            try:
                result = await self._execute_download(
                    url=job.normalized_url,
                    opts=opts,
                    timeout=settings.download_timeout_seconds,
                )

                output_file = self._find_output_file(job_dir, "video")
                if output_file and output_file.exists():
                    return DownloadResult(
                        success=True,
                        file_path=output_file,
                        file_size=output_file.stat().st_size,
                        extension=output_file.suffix,
                        processing_mode=processing_mode,
                        selected_format=result.get("ext", job.format),
                        selected_quality=selected_quality,
                    )

                raise DownloadFailedError("Output file not found after download")

            except Exception as e:
                last_error = e
                attempt += 1

                retry = retry_policy.should_retry(attempt, e)
                if not retry:
                    break

                logger.warning(
                    f"Retry {attempt}/{job.max_attempts} for {job.job_id}: {retry.reason}",
                    extra={"job_id": job.job_id, "attempt": attempt},
                )

                if retry.should_reduce_quality:
                    current_height = int(selected_quality) if selected_quality.isdigit() else 720
                    new_height = retry_policy.get_quality_reduction(attempt, current_height)
                    opts["format"] = get_video_format_string(new_height, job.format)
                    selected_quality = str(new_height)

                opts.update(get_retry_options(attempt, job.max_attempts))

                await asyncio.sleep(retry.delay_seconds)

        raise DownloadFailedError(f"Download failed after {attempt} attempts: {last_error}")

    async def _execute_download(
        self,
        url: str,
        opts: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        def _do_download():
            with create_youtube_dl(opts, yt_dlp.YoutubeDL) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_do_download),
                timeout=timeout,
            )
            return result or {}
        except asyncio.TimeoutError:
            raise DownloadTimeoutError(f"Download timed out after {timeout}s")

    def _find_output_file(self, job_dir: Path, prefix: str) -> Optional[Path]:
        if not job_dir.exists():
            return None

        for file in job_dir.iterdir():
            if file.is_file() and file.stem.startswith(prefix):
                if not file.suffix.endswith(".part"):
                    return file

        for file in job_dir.iterdir():
            if file.is_file() and not file.suffix.endswith(".part"):
                return file

        return None

    def _cleanup_job_dir(self, job_dir: Path) -> None:
        try:
            if job_dir.exists():
                shutil.rmtree(job_dir)
        except Exception as e:
            logger.error(f"Failed to cleanup job directory: {e}")

    async def cancel(self, job_id: str) -> bool:
        if job_id in self.active_downloads:
            task = self.active_downloads[job_id]
            task.cancel()
            del self.active_downloads[job_id]
            return True
        return False


download_engine = DownloadEngine()
