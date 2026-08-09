"""Metadata storage."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

from AnonX_3.downloader_api.core.config import settings

logger = logging.getLogger(__name__)


class MetadataStore:
    def __init__(self, metadata_dir: Optional[Path] = None):
        self.metadata_dir = metadata_dir or settings.metadata_dir
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, video_id: str) -> Path:
        return self.metadata_dir / f"{video_id}.json"

    def save(self, video_id: str, metadata: dict[str, Any]) -> bool:
        try:
            path = self._get_path(video_id)
            data = {
                "video_id": video_id,
                "metadata": metadata,
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"Failed to save metadata for {video_id}: {e}")
            return False

    def load(self, video_id: str, max_age_seconds: int = 3600) -> Optional[dict[str, Any]]:
        try:
            path = self._get_path(video_id)
            if not path.exists():
                return None

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            cached_at = datetime.fromisoformat(data.get("cached_at", ""))
            age = (datetime.now(timezone.utc) - cached_at).total_seconds()

            if age > max_age_seconds:
                path.unlink(missing_ok=True)
                return None

            return data.get("metadata")
        except Exception as e:
            logger.error(f"Failed to load metadata for {video_id}: {e}")
            return None

    def delete(self, video_id: str) -> bool:
        try:
            path = self._get_path(video_id)
            if path.exists():
                path.unlink()
            return True
        except Exception as e:
            logger.error(f"Failed to delete metadata for {video_id}: {e}")
            return False

    def cleanup_old(self, max_age_seconds: int = 86400) -> int:
        removed = 0
        try:
            now = datetime.now(timezone.utc)
            for path in self.metadata_dir.glob("*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    cached_at = datetime.fromisoformat(data.get("cached_at", ""))
                    age = (now - cached_at).total_seconds()
                    if age > max_age_seconds:
                        path.unlink()
                        removed += 1
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Failed to cleanup metadata: {e}")
        return removed


metadata_store = MetadataStore()
