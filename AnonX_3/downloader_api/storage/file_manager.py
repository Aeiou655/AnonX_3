"""File management utilities."""

import os
import shutil
import logging
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

from AnonX_3.downloader_api.core.config import settings

logger = logging.getLogger(__name__)


class FileManager:
    def __init__(self):
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        settings.ensure_directories()

    def atomic_write(
        self,
        source_path: Path,
        target_path: Path,
        overwrite: bool = False,
    ) -> bool:
        if not source_path.exists():
            logger.error(f"Source file does not exist: {source_path}")
            return False

        if target_path.exists() and not overwrite:
            logger.warning(f"Target file already exists: {target_path}")
            return False

        target_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            temp_target = target_path.with_suffix(target_path.suffix + ".atomic")

            shutil.copy2(source_path, temp_target)

            if os.name == "nt":
                if target_path.exists():
                    target_path.unlink()
            os.rename(temp_target, target_path)

            return True
        except Exception as e:
            logger.error(f"Atomic write failed: {e}")
            if temp_target.exists():
                try:
                    temp_target.unlink()
                except Exception:
                    pass
            return False

    def atomic_move(
        self,
        source_path: Path,
        target_path: Path,
        overwrite: bool = False,
    ) -> bool:
        if not source_path.exists():
            logger.error(f"Source file does not exist: {source_path}")
            return False

        if target_path.exists() and not overwrite:
            logger.warning(f"Target file already exists: {target_path}")
            return False

        target_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if source_path.stat().st_dev == target_path.parent.stat().st_dev:
                if os.name == "nt" and target_path.exists():
                    target_path.unlink()
                os.rename(source_path, target_path)
            else:
                if not self.atomic_write(source_path, target_path, overwrite):
                    return False
                source_path.unlink()

            return True
        except Exception as e:
            logger.error(f"Atomic move failed: {e}")
            return False

    def safe_delete(self, path: Path) -> bool:
        if not path.exists():
            return True

        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return True
        except Exception as e:
            logger.error(f"Failed to delete {path}: {e}")
            return False

    def safe_delete_directory(self, path: Path) -> bool:
        return self.safe_delete(path)

    def get_file_size(self, path: Path) -> int:
        if not path.exists():
            return 0
        try:
            return path.stat().st_size
        except Exception:
            return 0

    def file_exists(self, path: Path) -> bool:
        return path.exists() and path.is_file()

    def move_to_quarantine(
        self,
        source_path: Path,
        job_id: str,
        reason: str,
    ) -> Optional[Path]:
        if not source_path.exists():
            return None

        quarantine_path = settings.quarantine_dir / f"{job_id}_{reason}{source_path.suffix}"

        try:
            settings.quarantine_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(quarantine_path))
            logger.warning(f"Moved file to quarantine: {quarantine_path}, reason: {reason}")
            return quarantine_path
        except Exception as e:
            logger.error(f"Failed to move to quarantine: {e}")
            return None

    @contextmanager
    def temp_file(self, suffix: str = ".tmp", dir: Optional[Path] = None):
        import tempfile
        dir = dir or settings.temp_dir
        dir.mkdir(parents=True, exist_ok=True)

        fd, path = tempfile.mkstemp(suffix=suffix, dir=str(dir))
        temp_path = Path(path)
        try:
            os.close(fd)
            yield temp_path
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass


file_manager = FileManager()
