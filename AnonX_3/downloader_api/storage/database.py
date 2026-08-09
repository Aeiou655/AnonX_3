"""SQLite database for cache metadata."""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List
from contextlib import contextmanager

from AnonX_3.downloader_api.core.config import settings
from AnonX_3.downloader_api.core.constants import MediaType, ValidationState
from AnonX_3.downloader_api.schemas.cache import CacheEntry, CacheStats

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or settings.database_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'youtube',
                    media_type TEXT NOT NULL,
                    format TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    title TEXT,
                    duration INTEGER,
                    file_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mime_type TEXT NOT NULL,
                    validation_state TEXT NOT NULL DEFAULT 'valid',
                    created_at TEXT NOT NULL,
                    last_accessed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_cache_video_id ON cache_entries(video_id);
                CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
                CREATE INDEX IF NOT EXISTS idx_cache_media_type ON cache_entries(media_type);
                CREATE INDEX IF NOT EXISTS idx_cache_last_accessed ON cache_entries(last_accessed_at);

                CREATE TABLE IF NOT EXISTS job_history (
                    job_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    duration_ms INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_job_video_id ON job_history(video_id);
                CREATE INDEX IF NOT EXISTS idx_job_created ON job_history(created_at);
            """)

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_cache_entry(self, entry: CacheEntry) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO cache_entries (
                        cache_key, video_id, source, media_type, format, quality,
                        title, duration, file_path, file_size, mime_type,
                        validation_state, created_at, last_accessed_at, expires_at, hit_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    entry.cache_key,
                    entry.video_id,
                    entry.source,
                    entry.media_type.value,
                    entry.format,
                    entry.quality,
                    entry.title,
                    entry.duration,
                    entry.file_path,
                    entry.file_size,
                    entry.mime_type,
                    entry.validation_state.value,
                    entry.created_at.isoformat(),
                    entry.last_accessed_at.isoformat(),
                    entry.expires_at.isoformat(),
                    entry.hit_count,
                ))
            return True
        except Exception as e:
            logger.error(f"Failed to add cache entry: {e}")
            return False

    def get_cache_entry(self, cache_key: str) -> Optional[CacheEntry]:
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM cache_entries WHERE cache_key = ?",
                    (cache_key,)
                ).fetchone()

                if row:
                    return self._row_to_entry(row)
        except Exception as e:
            logger.error(f"Failed to get cache entry: {e}")
        return None

    def update_access(self, cache_key: str) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    UPDATE cache_entries
                    SET last_accessed_at = ?, hit_count = hit_count + 1
                    WHERE cache_key = ?
                """, (datetime.now(timezone.utc).isoformat(), cache_key))
            return True
        except Exception as e:
            logger.error(f"Failed to update access: {e}")
            return False

    def delete_cache_entry(self, cache_key: str) -> bool:
        try:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM cache_entries WHERE cache_key = ?", (cache_key,))
            return True
        except Exception as e:
            logger.error(f"Failed to delete cache entry: {e}")
            return False

    def get_expired_entries(self) -> List[CacheEntry]:
        entries = []
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM cache_entries WHERE expires_at < ?",
                    (now,)
                ).fetchall()
                entries = [self._row_to_entry(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get expired entries: {e}")
        return entries

    def get_low_hit_entries(self, max_hits: int = 1, limit: int = 100) -> List[CacheEntry]:
        entries = []
        try:
            with self._get_connection() as conn:
                rows = conn.execute("""
                    SELECT * FROM cache_entries
                    WHERE hit_count <= ?
                    ORDER BY last_accessed_at ASC
                    LIMIT ?
                """, (max_hits, limit)).fetchall()
                entries = [self._row_to_entry(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get low hit entries: {e}")
        return entries

    def get_oldest_entries(self, limit: int = 100) -> List[CacheEntry]:
        entries = []
        try:
            with self._get_connection() as conn:
                rows = conn.execute("""
                    SELECT * FROM cache_entries
                    ORDER BY last_accessed_at ASC
                    LIMIT ?
                """, (limit,)).fetchall()
                entries = [self._row_to_entry(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get oldest entries: {e}")
        return entries

    def get_largest_video_entries(self, limit: int = 50) -> List[CacheEntry]:
        entries = []
        try:
            with self._get_connection() as conn:
                rows = conn.execute("""
                    SELECT * FROM cache_entries
                    WHERE media_type = 'video'
                    ORDER BY file_size DESC, last_accessed_at ASC
                    LIMIT ?
                """, (limit,)).fetchall()
                entries = [self._row_to_entry(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get largest video entries: {e}")
        return entries

    def get_stats(self) -> CacheStats:
        stats = CacheStats()
        try:
            with self._get_connection() as conn:
                row = conn.execute("""
                    SELECT
                        COUNT(*) as total_entries,
                        COALESCE(SUM(file_size), 0) as total_size,
                        COALESCE(SUM(hit_count), 0) as hit_count_total,
                        MIN(created_at) as oldest_entry,
                        MAX(created_at) as newest_entry
                    FROM cache_entries
                """).fetchone()

                stats.total_entries = row["total_entries"]
                stats.total_size_bytes = row["total_size"]
                stats.hit_count_total = row["hit_count_total"]

                if row["oldest_entry"]:
                    stats.oldest_entry = datetime.fromisoformat(row["oldest_entry"])
                if row["newest_entry"]:
                    stats.newest_entry = datetime.fromisoformat(row["newest_entry"])

                audio_row = conn.execute("""
                    SELECT COUNT(*) as count, COALESCE(SUM(file_size), 0) as size
                    FROM cache_entries WHERE media_type = 'audio'
                """).fetchone()
                stats.audio_entries = audio_row["count"]
                stats.audio_size_bytes = audio_row["size"]

                video_row = conn.execute("""
                    SELECT COUNT(*) as count, COALESCE(SUM(file_size), 0) as size
                    FROM cache_entries WHERE media_type = 'video'
                """).fetchone()
                stats.video_entries = video_row["count"]
                stats.video_size_bytes = video_row["size"]

        except Exception as e:
            logger.error(f"Failed to get stats: {e}")

        return stats

    def clear_all(self) -> int:
        try:
            with self._get_connection() as conn:
                result = conn.execute("DELETE FROM cache_entries")
                return result.rowcount
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            return 0

    def _row_to_entry(self, row: sqlite3.Row) -> CacheEntry:
        return CacheEntry(
            cache_key=row["cache_key"],
            video_id=row["video_id"],
            source=row["source"],
            media_type=MediaType(row["media_type"]),
            format=row["format"],
            quality=row["quality"],
            title=row["title"],
            duration=row["duration"],
            file_path=row["file_path"],
            file_size=row["file_size"],
            mime_type=row["mime_type"],
            validation_state=ValidationState(row["validation_state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            last_accessed_at=datetime.fromisoformat(row["last_accessed_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            hit_count=row["hit_count"],
        )

    def add_job_history(
        self,
        job_id: str,
        video_id: str,
        media_type: str,
        status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> bool:
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO job_history (
                        job_id, video_id, media_type, status,
                        error_code, error_message, duration_ms,
                        created_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id, video_id, media_type, status,
                    error_code, error_message, duration_ms,
                    now, now if status in ("completed", "failed") else None,
                ))
            return True
        except Exception as e:
            logger.error(f"Failed to add job history: {e}")
            return False
