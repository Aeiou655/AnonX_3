# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Direct-stream watchdog: early/mid-stream direct→local failover signals.

True seamless A/V handoff is often impossible; we restart from local
(with seek when media.time is known) within DIRECT_FAILOVER_WINDOW_SEC.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from AnonX_3 import config, logger


@dataclass
class WatchEntry:
    chat_id: int
    media_id: str
    source: str  # youtube_remote | tiktok_remote | ...
    started_at: float = field(default_factory=time.time)
    local_path: str | None = None
    video: bool = False
    seek_hint: int = 0
    armed: bool = True


class DirectWatchdog:
    """Per-chat arm after successful direct start; handle early stream death."""

    def __init__(self) -> None:
        self._watches: dict[int, WatchEntry] = {}

    def window_sec(self) -> float:
        try:
            return max(
                5.0, float(getattr(config, "DIRECT_FAILOVER_WINDOW_SEC", 12) or 12)
            )
        except Exception:
            return 12.0

    def enabled(self) -> bool:
        return bool(getattr(config, "DIRECT_MIDSTREAM_FAILOVER", True))

    def arm(
        self,
        chat_id: int,
        media: Any,
        *,
        source: str = "youtube_remote",
        local_path: str | None = None,
    ) -> None:
        if not self.enabled():
            return
        mid = str(getattr(media, "id", "") or "")
        path = local_path or getattr(media, "local_path", None) or getattr(
            media, "file_path", None
        )
        if path and str(path).startswith("http"):
            path = None
        entry = WatchEntry(
            chat_id=chat_id,
            media_id=mid,
            source=source,
            local_path=str(path) if path else None,
            video=bool(getattr(media, "video", False)),
            seek_hint=int(getattr(media, "time", 0) or 0),
        )
        self._watches[chat_id] = entry
        logger.info(
            "direct_watchdog armed chat_id=%s media_id=%s window=%.0fs local=%s",
            chat_id,
            mid,
            self.window_sec(),
            bool(entry.local_path),
        )

    def update_local(self, chat_id: int, local_path: str | None) -> None:
        w = self._watches.get(chat_id)
        if w and local_path and not str(local_path).startswith("http"):
            w.local_path = str(local_path)

    def disarm(self, chat_id: int) -> None:
        self._watches.pop(chat_id, None)

    def get(self, chat_id: int) -> WatchEntry | None:
        return self._watches.get(chat_id)

    def should_failover_on_stream_end(self, chat_id: int, media: Any = None) -> bool:
        """True only for *early* StreamEnded on remote direct (not natural song end).

        Natural completion must advance queue / leave VC (AnonX behavior).
        Short tracks that finish inside DIRECT_FAILOVER_WINDOW_SEC used to be
        mis-handled as midstream death and never called play_next/stop.
        """
        if not self.enabled():
            return False
        w = self._watches.get(chat_id)
        if not w or not w.armed:
            return False
        elapsed = time.time() - w.started_at
        if elapsed > self.window_sec():
            self.disarm(chat_id)
            return False
        # Natural end: played most of known duration → advance/leave, do not failover.
        if media is not None:
            try:
                dur = float(
                    getattr(media, "duration_sec", 0)
                    or getattr(media, "duration", 0)
                    or 0
                )
            except Exception:
                dur = 0.0
            if dur > 0 and elapsed >= max(8.0, dur * 0.85):
                self.disarm(chat_id)
                return False
            url = getattr(media, "stream_url", None) or ""
            src = getattr(media, "source", None) or w.source
            # Local filesystem path is not remote direct
            if url and not str(url).startswith(("http://", "https://")):
                self.disarm(chat_id)
                return False
            if str(url).startswith(("http://", "https://")) or str(src).endswith(
                "remote"
            ):
                return True
            return False
        return bool(w.local_path)

    def consume_failover(self, chat_id: int) -> WatchEntry | None:
        """Return watch entry once and disarm (single failover attempt)."""
        w = self._watches.pop(chat_id, None)
        if w:
            w.armed = False
        return w


direct_watchdog = DirectWatchdog()
