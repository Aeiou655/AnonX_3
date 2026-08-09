"""Small additive request-context layer shared by every queue item.

Media/Track remain the canonical queue objects.  This module enriches them
instead of introducing a second request or queue implementation.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from AnonX_3.core.cache.keys import detect_source


INTENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("myanmar", ("myanmar", "burmese", "မြန်မာ")),
    ("romantic", ("love", "romantic", "အချစ်")),
    ("study", ("study", "studying", "lofi", "lo-fi", "စာကျက်")),
    ("workout", ("workout", "gym", "energetic")),
    ("party", ("party", "dance")),
    ("chill", ("chill", "calm", "rainy", "relax")),
)


def normalize_query(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_music_intent(value: str | None) -> str | None:
    normalized = normalize_query(value).casefold()
    if not normalized:
        return None
    for intent, markers in INTENT_PATTERNS:
        if any(marker in normalized for marker in markers):
            return intent
    return None


def enrich_request(
    media: Any,
    *,
    chat_id: int,
    user_id: int = 0,
    query: str | None = None,
    request_source: str = "command",
    priority: int = 50,
) -> Any:
    """Attach normalized, observable request metadata to a Media/Track."""
    if media is None:
        return None
    normalized = normalize_query(query or getattr(media, "title", None))
    media.chat_id = int(chat_id)
    media.user_id = int(user_id or 0)
    media.original_query = query
    media.normalized_query = normalized
    media.request_source = str(request_source or "command")
    media.priority = int(priority)
    source = getattr(media, "source", None) or detect_source(media)
    media.candidate_sources = list(
        dict.fromkeys(
            [
                source,
                "cache",
                "direct",
                "local",
                "soundcloud",
            ]
        )
    )
    media.selected_source = source
    media.backup_source = "local" if source != "local" else "direct"
    media.feature_flags = dict(getattr(media, "feature_flags", {}) or {})
    intent = detect_music_intent(normalized)
    if intent:
        media.feature_flags["music_intent"] = intent
    return media
