# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""SQLite catalog for CDN / cache assets with full state machine metadata."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from AnonX_3.core.cache.keys import normalize_lookup_text


@dataclass
class AssetRow:
    key: str
    media_id: str
    video: bool
    quality_tier: str
    filename: str
    ready_path: str
    status: str
    size_bytes: int
    created_at: float
    last_access: float
    cdn_url: str
    # Extended metadata (prompt cache design)
    source: str = "youtube"
    query: str = ""
    lookup_key: str = ""
    title: str = ""
    artist: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    media_type: str = "audio"
    quality: str = ""
    format_id: str = ""
    public_url: str = ""
    checksum: str = ""
    expires_at: float = 0.0
    failure_reason: str = ""
    retry_count: int = 0
    refcount: int = 0
    local_durable: bool = False


_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source", "TEXT NOT NULL DEFAULT 'youtube'"),
    ("query", "TEXT NOT NULL DEFAULT ''"),
    ("lookup_key", "TEXT NOT NULL DEFAULT ''"),
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("artist", "TEXT NOT NULL DEFAULT ''"),
    ("duration", "REAL NOT NULL DEFAULT 0"),
    ("thumbnail", "TEXT NOT NULL DEFAULT ''"),
    ("media_type", "TEXT NOT NULL DEFAULT 'audio'"),
    ("quality", "TEXT NOT NULL DEFAULT ''"),
    ("format_id", "TEXT NOT NULL DEFAULT ''"),
    ("public_url", "TEXT NOT NULL DEFAULT ''"),
    ("checksum", "TEXT NOT NULL DEFAULT ''"),
    ("expires_at", "REAL NOT NULL DEFAULT 0"),
    ("failure_reason", "TEXT NOT NULL DEFAULT ''"),
    ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ("refcount", "INTEGER NOT NULL DEFAULT 0"),
    # Local yt-dlp results are reusable even when CDN publishing is disabled.
    # Keep their catalog entries out of normal TTL cleanup; capacity pressure
    # still uses the regular LRU path.
    ("local_durable", "INTEGER NOT NULL DEFAULT 0"),
)


class MediaStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_assets (
                        key TEXT PRIMARY KEY,
                        media_id TEXT NOT NULL,
                        video INTEGER NOT NULL DEFAULT 0,
                        quality_tier TEXT NOT NULL DEFAULT '',
                        filename TEXT NOT NULL DEFAULT '',
                        ready_path TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'ready',
                        size_bytes INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        last_access REAL NOT NULL,
                        cdn_url TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_last_access "
                    "ON media_assets(last_access)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_status "
                    "ON media_assets(status)"
                )
                # Migrate extra columns for existing DBs.
                existing = {
                    r[1]
                    for r in conn.execute("PRAGMA table_info(media_assets)").fetchall()
                }
                for col, decl in _EXTRA_COLUMNS:
                    if col not in existing:
                        conn.execute(
                            f"ALTER TABLE media_assets ADD COLUMN {col} {decl}"
                        )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_lookup_ready "
                    "ON media_assets(video, status, lookup_key, last_access)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_lookup_aliases (
                        lookup_key TEXT NOT NULL,
                        asset_key TEXT NOT NULL,
                        video INTEGER NOT NULL DEFAULT 0,
                        last_access REAL NOT NULL,
                        PRIMARY KEY (lookup_key, asset_key)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_lookup_alias_ready "
                    "ON media_lookup_aliases(video, lookup_key, last_access)"
                )
                # Backfill the original single lookup column so upgrades keep
                # existing text-cache hits without a rebuild.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO media_lookup_aliases
                        (lookup_key, asset_key, video, last_access)
                    SELECT lookup_key, key, video, last_access
                    FROM media_assets
                    WHERE lookup_key <> ''
                    """
                )
                conn.commit()

    def get(self, key: str) -> AssetRow | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM media_assets WHERE key = ?", (key,)
                ).fetchone()
        if not row:
            return None
        return self._row_to_asset(row)

    def upsert_ready(
        self,
        *,
        key: str,
        media_id: str,
        video: bool,
        quality_tier: str,
        filename: str,
        ready_path: str,
        size_bytes: int,
        cdn_url: str = "",
        source: str = "youtube",
        query: str = "",
        lookup_key: str = "",
        title: str = "",
        artist: str = "",
        duration: float = 0.0,
        thumbnail: str = "",
        quality: str = "",
        format_id: str = "",
        checksum: str = "",
        expires_at: float = 0.0,
        ttl_hours: float | None = None,
        local_durable: bool = False,
    ) -> AssetRow:
        now = time.time()
        if local_durable:
            expires_at = 0.0
        elif expires_at <= 0 and ttl_hours:
            expires_at = now + max(1.0, float(ttl_hours)) * 3600.0
        media_type = "video" if video else "audio"
        public_url = cdn_url or ""
        lookup_key = (
            normalize_lookup_text(lookup_key)
            or normalize_lookup_text(query)
            or normalize_lookup_text(title)
        )
        saved_row: sqlite3.Row | None = None
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO media_assets (
                        key, media_id, video, quality_tier, filename, ready_path,
                        status, size_bytes, created_at, last_access, cdn_url,
                        source, query, lookup_key, title, artist, duration, thumbnail,
                        media_type, quality, format_id, public_url, checksum,
                        expires_at, failure_reason, retry_count, refcount, local_durable
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, '', 0, 0, ?
                    )
                    ON CONFLICT(key) DO UPDATE SET
                        filename=excluded.filename,
                        ready_path=excluded.ready_path,
                        status='ready',
                        size_bytes=excluded.size_bytes,
                        last_access=excluded.last_access,
                        cdn_url=excluded.cdn_url,
                        public_url=excluded.public_url,
                        source=excluded.source,
                        query=COALESCE(NULLIF(excluded.query, ''), media_assets.query),
                        lookup_key=COALESCE(NULLIF(excluded.lookup_key, ''), media_assets.lookup_key),
                        title=COALESCE(NULLIF(excluded.title, ''), media_assets.title),
                        artist=COALESCE(NULLIF(excluded.artist, ''), media_assets.artist),
                        duration=CASE WHEN excluded.duration > 0
                                 THEN excluded.duration ELSE media_assets.duration END,
                        thumbnail=COALESCE(NULLIF(excluded.thumbnail, ''), media_assets.thumbnail),
                        media_type=excluded.media_type,
                        quality=COALESCE(NULLIF(excluded.quality, ''), media_assets.quality),
                        format_id=COALESCE(NULLIF(excluded.format_id, ''), media_assets.format_id),
                        checksum=COALESCE(NULLIF(excluded.checksum, ''), media_assets.checksum),
                        expires_at=CASE
                                   WHEN excluded.local_durable
                                     OR media_assets.local_durable THEN 0
                                   WHEN excluded.expires_at > 0 THEN excluded.expires_at
                                   ELSE media_assets.expires_at END,
                        failure_reason='',
                        quality_tier=excluded.quality_tier,
                        local_durable=MAX(
                            media_assets.local_durable,
                            excluded.local_durable
                        )
                    """,
                    (
                        key,
                        media_id,
                        1 if video else 0,
                        quality_tier or "",
                        filename,
                        ready_path,
                        int(size_bytes or 0),
                        now,
                        now,
                        cdn_url or "",
                        source or "youtube",
                        query or "",
                        lookup_key,
                        title or "",
                        artist or "",
                        float(duration or 0),
                        thumbnail or "",
                        media_type,
                        quality or quality_tier or "",
                        format_id or "",
                        public_url,
                        checksum or "",
                        float(expires_at or 0),
                        1 if local_durable else 0,
                    ),
                )
                aliases = {
                    normalize_lookup_text(lookup_key),
                    normalize_lookup_text(query),
                    normalize_lookup_text(title),
                }
                for alias in aliases:
                    if not alias:
                        continue
                    conn.execute(
                        """
                        INSERT INTO media_lookup_aliases
                            (lookup_key, asset_key, video, last_access)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(lookup_key, asset_key) DO UPDATE SET
                            video=excluded.video,
                            last_access=excluded.last_access
                        """,
                        (alias, key, 1 if video else 0, now),
                    )
                saved_row = conn.execute(
                    "SELECT * FROM media_assets WHERE key = ?", (key,)
                ).fetchone()
                conn.commit()
        if saved_row is not None:
            return self._row_to_asset(saved_row)
        return AssetRow(
            key=key,
            media_id=media_id,
            video=bool(video),
            quality_tier=quality_tier or "",
            filename=filename,
            ready_path=ready_path,
            status="ready",
            size_bytes=int(size_bytes or 0),
            created_at=now,
            last_access=now,
            cdn_url=cdn_url or "",
            source=source or "youtube",
            query=query or "",
            lookup_key=lookup_key,
            title=title or "",
            artist=artist or "",
            duration=float(duration or 0),
            thumbnail=thumbnail or "",
            media_type=media_type,
            quality=quality or quality_tier or "",
            format_id=format_id or "",
            public_url=public_url,
            checksum=checksum or "",
            expires_at=float(expires_at or 0),
            local_durable=bool(local_durable),
        )

    def find_ready_by_lookup(self, value: str, *, video: bool) -> AssetRow | None:
        """Find a durable exact title/query hit without contacting a provider."""
        lookup_key = normalize_lookup_text(value)
        if not lookup_key:
            return None
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT media_assets.*
                    FROM media_lookup_aliases
                    JOIN media_assets
                      ON media_assets.key = media_lookup_aliases.asset_key
                    WHERE media_lookup_aliases.video = ?
                      AND media_lookup_aliases.lookup_key = ?
                      AND media_assets.status = 'ready'
                    ORDER BY media_lookup_aliases.last_access DESC,
                             media_assets.last_access DESC
                    LIMIT 1
                    """,
                    (1 if video else 0, lookup_key),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                    """
                    SELECT * FROM media_assets
                    WHERE video = ? AND status = 'ready' AND lookup_key = ?
                    ORDER BY last_access DESC
                    LIMIT 1
                    """,
                    (1 if video else 0, lookup_key),
                    ).fetchone()
        return self._row_to_asset(row) if row else None

    def upsert_status(
        self,
        *,
        key: str,
        media_id: str,
        video: bool = False,
        quality_tier: str = "",
        status: str,
        failure_reason: str = "",
        increment_retry: bool = False,
        source: str = "youtube",
    ) -> None:
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT retry_count FROM media_assets WHERE key = ?", (key,)
                ).fetchone()
                retry = int(row["retry_count"] or 0) if row else 0
                if increment_retry:
                    retry += 1
                if row:
                    conn.execute(
                        """
                        UPDATE media_assets SET
                            status = ?,
                            failure_reason = ?,
                            retry_count = ?,
                            last_access = ?
                        WHERE key = ?
                        """,
                        (status, failure_reason or "", retry, now, key),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO media_assets (
                            key, media_id, video, quality_tier, filename, ready_path,
                            status, size_bytes, created_at, last_access, cdn_url,
                            source, failure_reason, retry_count, media_type
                        ) VALUES (?, ?, ?, ?, '', '', ?, 0, ?, ?, '', ?, ?, ?, ?)
                        """,
                        (
                            key,
                            media_id,
                            1 if video else 0,
                            quality_tier or "",
                            status,
                            now,
                            now,
                            source or "youtube",
                            failure_reason or "",
                            retry,
                            "video" if video else "audio",
                        ),
                    )
                conn.commit()

    def set_status(
        self,
        key: str,
        status: str,
        *,
        failure_reason: str = "",
        increment_retry: bool = False,
    ) -> None:
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                if increment_retry:
                    conn.execute(
                        """
                        UPDATE media_assets SET
                            status = ?,
                            failure_reason = ?,
                            retry_count = retry_count + 1,
                            last_access = ?
                        WHERE key = ?
                        """,
                        (status, failure_reason or "", now, key),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE media_assets SET
                            status = ?,
                            failure_reason = CASE
                                WHEN ? = '' THEN failure_reason ELSE ?
                            END,
                            last_access = ?
                        WHERE key = ?
                        """,
                        (status, failure_reason or "", failure_reason or "", now, key),
                    )
                conn.commit()

    def set_refcount(self, key: str, refcount: int) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE media_assets SET refcount = ? WHERE key = ?",
                    (max(0, int(refcount)), key),
                )
                conn.commit()

    def touch(self, key: str, *, extend_ttl_hours: float | None = None) -> None:
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                if extend_ttl_hours:
                    expires = now + max(1.0, float(extend_ttl_hours)) * 3600.0
                    conn.execute(
                        """
                        UPDATE media_assets
                        SET last_access = ?,
                            expires_at = CASE
                                WHEN local_durable THEN 0
                                ELSE MAX(expires_at, ?)
                            END
                        WHERE key = ?
                        """,
                        (now, expires, key),
                    )
                else:
                    conn.execute(
                        "UPDATE media_assets SET last_access = ? WHERE key = ?",
                        (now, key),
                    )
                conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM media_lookup_aliases WHERE asset_key = ?", (key,)
                )
                conn.execute("DELETE FROM media_assets WHERE key = ?", (key,))
                conn.commit()

    def expired(self, ttl_hours: float) -> list[AssetRow]:
        now = time.time()
        cutoff = now - max(1.0, float(ttl_hours)) * 3600.0
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM media_assets
                    WHERE local_durable = 0
                      AND (
                           (expires_at > 0 AND expires_at < ?)
                           OR (expires_at <= 0 AND last_access < ?)
                      )
                    """,
                    (now, cutoff),
                ).fetchall()
        return [self._row_to_asset(r) for r in rows]

    def list_by_status(self, status: str) -> list[AssetRow]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM media_assets WHERE status = ?",
                    (status,),
                ).fetchall()
        return [self._row_to_asset(r) for r in rows]

    def lru_candidates(self, limit: int = 50) -> list[AssetRow]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM media_assets
                    WHERE status = 'ready' AND refcount <= 0
                    ORDER BY last_access ASC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
        return [self._row_to_asset(r) for r in rows]

    @staticmethod
    def _row_get(row: sqlite3.Row, name: str, default=None):
        try:
            val = row[name]
            return default if val is None else val
        except (IndexError, KeyError):
            return default

    @classmethod
    def _row_to_asset(cls, row: sqlite3.Row) -> AssetRow:
        video = bool(cls._row_get(row, "video", 0))
        return AssetRow(
            key=row["key"],
            media_id=row["media_id"],
            video=video,
            quality_tier=cls._row_get(row, "quality_tier", "") or "",
            filename=cls._row_get(row, "filename", "") or "",
            ready_path=cls._row_get(row, "ready_path", "") or "",
            status=cls._row_get(row, "status", "ready") or "ready",
            size_bytes=int(cls._row_get(row, "size_bytes", 0) or 0),
            created_at=float(cls._row_get(row, "created_at", 0) or 0),
            last_access=float(cls._row_get(row, "last_access", 0) or 0),
            cdn_url=cls._row_get(row, "cdn_url", "") or "",
            source=cls._row_get(row, "source", "youtube") or "youtube",
            query=cls._row_get(row, "query", "") or "",
            lookup_key=cls._row_get(row, "lookup_key", "") or "",
            title=cls._row_get(row, "title", "") or "",
            artist=cls._row_get(row, "artist", "") or "",
            duration=float(cls._row_get(row, "duration", 0) or 0),
            thumbnail=cls._row_get(row, "thumbnail", "") or "",
            media_type=cls._row_get(row, "media_type", "")
            or ("video" if video else "audio"),
            quality=cls._row_get(row, "quality", "") or "",
            format_id=cls._row_get(row, "format_id", "") or "",
            public_url=cls._row_get(row, "public_url", "")
            or cls._row_get(row, "cdn_url", "")
            or "",
            checksum=cls._row_get(row, "checksum", "") or "",
            expires_at=float(cls._row_get(row, "expires_at", 0) or 0),
            failure_reason=cls._row_get(row, "failure_reason", "") or "",
            retry_count=int(cls._row_get(row, "retry_count", 0) or 0),
            refcount=int(cls._row_get(row, "refcount", 0) or 0),
            local_durable=bool(cls._row_get(row, "local_durable", 0)),
        )
