"""Stable cache-key and exact text-normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

_WS_RE = re.compile(r"\s+")
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def normalize_lookup_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _WS_RE.sub(" ", text).strip().casefold()


def _component(value: object, default: str = "") -> str:
    text = str(value or default).strip()
    # Cache keys are colon-delimited. Source IDs used by this project are
    # normally URL-safe; escape literal colons without changing existing IDs.
    return text.replace("%", "%25").replace(":", "%3A")


def _uncomponent(value: str) -> str:
    return str(value).replace("%3A", ":").replace("%25", "%")


def make_cache_key(
    *,
    source: str,
    source_id: str,
    video: bool = False,
    quality: str | int | None = None,
    quality_tier: str | None = None,
) -> str:
    src = _component((source or "youtube").strip().lower(), "youtube")
    media_id = _component(source_id)
    mode = "video" if bool(video) else "audio"
    selected = _component(
        quality if quality not in (None, "") else quality_tier or "best",
        "best",
    )
    return f"source:{src}:{media_id}:{mode}:{selected}"


def parse_cache_key(key: str) -> dict[str, object]:
    parts = str(key or "").split(":")
    if len(parts) != 5 or parts[0] != "source" or parts[3] not in {"audio", "video"}:
        raise ValueError(f"invalid cache key: {key!r}")
    return {
        "source": _uncomponent(parts[1]),
        "source_id": _uncomponent(parts[2]),
        "media_id": _uncomponent(parts[2]),
        "media_type": parts[3],
        "video": parts[3] == "video",
        "quality": _uncomponent(parts[4]),
    }


def legacy_asset_key(
    media_id: str,
    *,
    video: bool = False,
    quality_tier: str | None = None,
) -> str:
    """Return the pre-canonical CDN key used by older deployments."""
    mode = "video" if bool(video) else "audio"
    tier = str(quality_tier or "best").strip() or "best"
    return f"{media_id}:{mode}:{tier}"


def detect_source(media: object) -> str:
    explicit = str(getattr(media, "source", "") or "").strip().lower()
    if explicit:
        return explicit

    for attr in ("url", "link", "file_path"):
        raw = str(getattr(media, attr, "") or "").strip()
        if not raw.startswith(("http://", "https://")):
            continue
        host = (urlparse(raw).hostname or "").lower()
        if "youtu" in host:
            return "youtube"
        if "tiktok" in host:
            return "tiktok"
        if "facebook" in host or "fb.watch" in host:
            return "facebook"
        if "soundcloud" in host:
            return "soundcloud"
        if "telegram" in host or host.endswith("t.me"):
            return "telegram"

    media_id = str(getattr(media, "id", "") or "")
    if _YT_ID_RE.fullmatch(media_id):
        return "youtube"
    return "unknown"
