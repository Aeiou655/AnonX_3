# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Alternate-source fallback after YouTube permanent failure."""

from __future__ import annotations

import re
import time
from typing import Any

from AnonX_3 import config, logger
from AnonX_3.core.resolver.matcher import (
    MatchScore,
    is_safe_query_title_rescue,
    pick_best,
    score_candidate,
)
from AnonX_3.core.resolver.soundcloud import (
    SoundCloudTransportError,
    is_soundcloud_url,
    soundcloud,
)
from AnonX_3.core.source_health import source_health
from AnonX_3.helpers import Track


_WEB_URL_RE = re.compile(r"^https?://", re.I)


def _query_candidates(media: Any = None, query: str | None = None) -> list[str]:
    """Build a bounded, stable query ladder without searching YouTube URLs."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return
        if _WEB_URL_RE.match(text) and not is_soundcloud_url(text):
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        candidates.append(text)

    _add(query)
    if media is None:
        return candidates
    _add(getattr(media, "original_query", None))
    _add(getattr(media, "normalized_query", None))
    title = (getattr(media, "title", None) or "").strip()
    artist = (
        getattr(media, "channel_name", None)
        or getattr(media, "artist", None)
        or ""
    ).strip()
    if title and artist:
        _add(f"{artist} {title}")
    _add(title)
    _add(artist)
    if not candidates:
        _add(getattr(media, "id", ""))
    return candidates


def _seed_query(media: Any = None, query: str | None = None) -> str:
    candidates = _query_candidates(media, query)
    return candidates[0] if candidates else ""


def _plain_user_query(media: Any = None, query: str | None = None) -> str:
    """Return only an explicit/original plain-text query, never a derived title."""
    values = [query]
    if media is not None:
        values.extend(
            [
                getattr(media, "original_query", None),
                getattr(media, "normalized_query", None),
            ]
        )
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text and not _WEB_URL_RE.match(text):
            return text
    return ""


def _pick_strict_query_rescue(
    candidates: list[Track],
    *,
    query: str,
    seed_artist: str | None,
    seed_duration_sec: float | int | None,
) -> tuple[Track | None, MatchScore | None]:
    """
    Rescue a user-query match that the cross-source artist/version score rejects.

    Cross-platform uploader names are often unrelated. This exception remains
    narrow: the normalized user query must be the complete title prefix, any
    suffix must be version-labelled, durations must be within ten seconds and
    five percent, and exactly one distinct candidate may qualify.
    """
    try:
        seed_duration = float(seed_duration_sec or 0)
    except (TypeError, ValueError):
        return None, None
    if not query or _WEB_URL_RE.match(query) or seed_duration <= 0:
        return None, None

    matches: dict[str, tuple[Track, MatchScore]] = {}
    for candidate in candidates:
        candidate_title = getattr(candidate, "title", None)
        candidate_duration = getattr(candidate, "duration_sec", None)
        if not is_safe_query_title_rescue(
            query=query,
            candidate_title=candidate_title,
            seed_duration_sec=seed_duration,
            candidate_duration_sec=candidate_duration,
        ):
            continue
        score = score_candidate(
            seed_title=query,
            seed_artist=seed_artist,
            seed_duration_sec=seed_duration,
            cand_title=candidate_title,
            cand_artist=getattr(candidate, "channel_name", None)
            or getattr(candidate, "artist", None),
            cand_duration_sec=candidate_duration,
        )
        identity = str(
            getattr(candidate, "url", None)
            or getattr(candidate, "id", None)
            or candidate_title
            or ""
        ).casefold()
        matches.setdefault(identity, (candidate, score))
    if len(matches) != 1:
        return None, None
    return next(iter(matches.values()))


def fallback_enabled() -> bool:
    return bool(getattr(config, "FALLBACK_SOUNDCLOUD", True))


def min_score() -> float:
    try:
        return float(getattr(config, "FALLBACK_MIN_SCORE", 0.85) or 0.85)
    except Exception:
        return 0.85


def soft_min_score() -> float:
    try:
        return float(getattr(config, "FALLBACK_SOFT_MIN_SCORE", 0.70) or 0.70)
    except Exception:
        return 0.70


async def find_fallback_track(
    *,
    media: Any = None,
    query: str | None = None,
    message_id: int = 0,
    video: bool = False,
    user: str | None = None,
) -> tuple[Track | None, dict | None]:
    """
    Search SoundCloud (and future sources) and return best scored match.

    Returns (track, meta) where meta includes score breakdown.
    """
    if not fallback_enabled():
        return None, {"reason": "fallback_disabled"}
    if video:
        return None, {
            "reason": "video_unsupported",
            "source": "soundcloud",
        }
    if not source_health.allow("soundcloud"):
        logger.info("fallback source skipped circuit=open source=soundcloud")
        return None, {"reason": "source_circuit_open", "source": "soundcloud"}

    queries = _query_candidates(media, query)
    if not queries:
        return None, {"reason": "empty_query"}
    try:
        query_attempts = max(
            1, int(getattr(config, "FALLBACK_QUERY_ATTEMPTS", 2) or 2)
        )
    except Exception:
        query_attempts = 2
    queries = queries[:query_attempts]

    seed_title = queries[0]
    user_query = _plain_user_query(media, query)
    seed_artist = None
    seed_dur = None
    if media is not None:
        seed_artist = getattr(media, "channel_name", None) or getattr(
            media, "artist", None
        )
        seed_dur = getattr(media, "duration_sec", None)

    started = time.monotonic()
    candidates: list[Track] = []
    q = queries[0]
    try:
        for candidate_query in queries:
            q = candidate_query
            candidates = await soundcloud.search(
                q, message_id=message_id, video=video
            )
            if candidates:
                break
    except SoundCloudTransportError:
        source_health.failure("soundcloud", reason="transport")
        return None, {
            "reason": "source_unavailable",
            "source": "soundcloud",
        }
    except Exception as ex:
        source_health.failure("soundcloud", reason=type(ex).__name__)
        raise
    if not candidates:
        # A completed empty search is a valid provider response, not a
        # transport outage. The resolver raises SoundCloudTransportError for
        # the latter so the query ladder cannot repeat a dead provider.
        source_health.success(
            "soundcloud", latency_sec=time.monotonic() - started
        )
        logger.info(
            "fallback no candidates query=%r attempts=%s",
            queries[0][:80],
            len(queries),
        )
        meta = {"reason": "no_candidates", "query": queries[0]}
        if len(queries) > 1:
            meta["attempted_queries"] = queries
        return None, meta

    soft_auto = bool(getattr(config, "FALLBACK_SOFT_AUTO", False))

    def _select(
        pool: list[Track],
    ) -> tuple[Track | None, MatchScore | None, bool]:
        # Hard threshold auto-use; soft band only when explicitly enabled.
        selected, selected_score = pick_best(
            pool,
            seed_title=seed_title or q,
            seed_artist=seed_artist,
            seed_duration_sec=seed_dur,
            min_score=min_score(),
            soft_min=soft_min_score(),
            auto_soft=soft_auto,
        )
        rescued = False
        if not selected and q.casefold() == user_query.casefold():
            selected, rescue_score = _pick_strict_query_rescue(
                pool,
                query=user_query,
                seed_artist=seed_artist,
                seed_duration_sec=seed_dur,
            )
            if selected and rescue_score:
                selected_score = rescue_score
                rescued = True
        return selected, selected_score, rescued

    try:
        probe_limit = max(
            1,
            int(
                getattr(config, "SOUNDCLOUD_CANDIDATE_PROBE_LIMIT", 3)
                or 3
            ),
        )
    except Exception:
        probe_limit = 3
    remaining = list(candidates)
    best: Track | None = None
    score: MatchScore | None = None
    query_rescue = False
    rejected_unplayable = 0
    while remaining and rejected_unplayable < probe_limit:
        best, score, query_rescue = _select(remaining)
        if not best or not score:
            break
        resolved, resolution = await soundcloud.resolve_url_status(
            best.url or "",
            message_id=message_id,
            video=video,
        )
        if resolved and resolution == "ok":
            best = resolved
            break
        if resolution == "transport":
            source_health.failure("soundcloud", reason="transport")
            return None, {
                "reason": "source_unavailable",
                "source": "soundcloud",
            }
        logger.info(
            "fallback rejected unplayable SoundCloud candidate reason=%s "
            "title=%r",
            resolution,
            best.title,
        )
        remaining = [candidate for candidate in remaining if candidate is not best]
        rejected_unplayable += 1
        best = None

    if not best or not score:
        source_health.success(
            "soundcloud", latency_sec=time.monotonic() - started
        )
        if rejected_unplayable:
            return None, {
                "reason": "no_playable_candidates",
                "source": "soundcloud",
                "rejected": rejected_unplayable,
            }
        return None, {
            "reason": "no_match",
            "query": q,
            "best_score": score.total if score else 0,
        }

    if score.total < min_score() and not query_rescue:
        # Soft band: only accept when soft_auto and above soft_min
        if not (soft_auto and score.total >= soft_min_score()):
            return None, {
                "reason": "score_too_low",
                "query": q,
                "score": score.as_dict(),
                "need": min_score(),
            }

    source_health.success(
        "soundcloud", latency_sec=time.monotonic() - started
    )
    best.user = user or getattr(media, "user", None)
    best.message_id = message_id or getattr(media, "message_id", 0) or 0
    best.video = bool(video)
    best.source = "soundcloud"  # type: ignore[attr-defined]
    logger.info(
        "fallback match source=soundcloud match_score=%.3f source_score=%.3f "
        "query_rescue=%s title=%r url=%s",
        score.total,
        source_health.score("soundcloud", compatible=not video, quality=1.0),
        query_rescue,
        best.title,
        (best.url or "")[:80],
    )
    try:
        from AnonX_3.core.metrics import mark_fallback_used

        mark_fallback_used()
    except Exception:
        pass
    return best, {
        "reason": "ok",
        "source": "soundcloud",
        "score": score.as_dict(),
        "auto": score.total >= min_score() or query_rescue,
        "query_rescue": query_rescue,
        "match_mode": (
            "normalized_query_containment" if query_rescue else "weighted_score"
        ),
        "query": q,
    }
