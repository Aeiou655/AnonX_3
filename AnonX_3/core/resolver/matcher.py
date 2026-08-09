# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Fallback candidate scoring: title / artist / duration / version."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


_VERSION_HINTS = (
    "remix",
    "live",
    "cover",
    "karaoke",
    "instrumental",
    "acoustic",
    "official",
    "edit",
    "extended",
    "radio",
    "slowed",
    "sped",
    "nightcore",
    "explicit",
    "clean",
    "bootleg",
    "mix",
)


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    t = text.casefold()
    t = re.sub(r"[​-‍⁠﻿]", "", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _tokens(text: str | None) -> set[str]:
    raw = _normalize(text)
    if not raw:
        return set()
    return {tok for tok in raw.split() if len(tok) > 1 or not tok.isascii()}


def title_similarity(a: str | None, b: str | None) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return seq
    jacc = len(ta & tb) / max(1, len(ta | tb))
    return max(seq, 0.55 * seq + 0.45 * jacc)


def artist_similarity(a: str | None, b: str | None) -> float:
    return title_similarity(a, b)


def duration_similarity(a_sec: float | int | None, b_sec: float | int | None) -> float:
    try:
        da = float(a_sec or 0)
        db = float(b_sec or 0)
    except Exception:
        return 0.0
    if da <= 0 or db <= 0:
        return 0.5  # unknown — neutral
    diff = abs(da - db)
    # Full score within 3s; linear decay to 0 at 60s
    if diff <= 3:
        return 1.0
    if diff >= 60:
        return 0.0
    return max(0.0, 1.0 - (diff - 3) / 57.0)


def _version_tags(text: str | None) -> set[str]:
    norm = _normalize(text)
    return {h for h in _VERSION_HINTS if h in norm}


def _rescue_normalize(text: str | None) -> str:
    """Normalize while preserving Unicode letters, combining marks, and numbers."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    output: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        if category[:1] in {"L", "M", "N"}:
            output.append(char)
        else:
            output.append(" ")
    return re.sub(r"\s+", " ", "".join(output)).strip()


def is_safe_query_title_rescue(
    *,
    query: str | None,
    candidate_title: str | None,
    seed_duration_sec: float | int | None,
    candidate_duration_sec: float | int | None,
) -> bool:
    """
    Validate the narrow cross-source rescue used after normal scoring rejects.

    The complete query must be the candidate's title prefix. Any suffix must
    contain a recognized version hint, and both durations must be within ten
    seconds and five percent. This deliberately preserves Myanmar marks.
    """
    normalized_query = _rescue_normalize(query)
    normalized_title = _rescue_normalize(candidate_title)
    base_chars = sum(
        unicodedata.category(char)[:1] in {"L", "N"}
        for char in normalized_query
    )
    if not normalized_query or base_chars < 6:
        return False
    if normalized_title == normalized_query:
        title_ok = True
    elif normalized_title.startswith(f"{normalized_query} "):
        suffix = normalized_title[len(normalized_query) :].strip()
        suffix_tokens = set(suffix.split())
        title_ok = bool(suffix_tokens.intersection(_VERSION_HINTS))
    else:
        title_ok = False
    if not title_ok:
        return False

    try:
        seed_duration = float(seed_duration_sec or 0)
        candidate_duration = float(candidate_duration_sec or 0)
    except (TypeError, ValueError):
        return False
    if (
        not math.isfinite(seed_duration)
        or not math.isfinite(candidate_duration)
        or seed_duration <= 0
        or candidate_duration <= 0
    ):
        return False
    duration_delta = abs(seed_duration - candidate_duration)
    return (
        duration_delta <= 10.0
        and duration_delta / max(seed_duration, candidate_duration) <= 0.05
    )


def version_similarity(seed_title: str | None, cand_title: str | None) -> float:
    sa, sb = _version_tags(seed_title), _version_tags(cand_title)
    if not sa and not sb:
        return 1.0  # both clean
    if not sa or not sb:
        return 0.35  # one has version tags, other doesn't
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / max(1, union)


@dataclass
class MatchScore:
    total: float
    title: float
    artist: float
    duration: float
    version: float

    def as_dict(self) -> dict[str, float]:
        return {
            "total": self.total,
            "title": self.title,
            "artist": self.artist,
            "duration": self.duration,
            "version": self.version,
        }


def score_candidate(
    *,
    seed_title: str | None,
    seed_artist: str | None,
    seed_duration_sec: float | int | None,
    cand_title: str | None,
    cand_artist: str | None,
    cand_duration_sec: float | int | None,
) -> MatchScore:
    """Weighted score per prompt: title 45%, artist 30%, duration 20%, version 5%."""
    t = title_similarity(seed_title, cand_title)
    a = artist_similarity(seed_artist, cand_artist)
    d = duration_similarity(seed_duration_sec, cand_duration_sec)
    v = version_similarity(seed_title, cand_title)
    total = 0.45 * t + 0.30 * a + 0.20 * d + 0.05 * v
    return MatchScore(
        total=round(total, 4),
        title=round(t, 4),
        artist=round(a, 4),
        duration=round(d, 4),
        version=round(v, 4),
    )


def pick_best(
    candidates: list[Any],
    *,
    seed_title: str | None,
    seed_artist: str | None,
    seed_duration_sec: float | int | None,
    min_score: float = 0.85,
    soft_min: float = 0.70,
    auto_soft: bool = True,
) -> tuple[Any | None, MatchScore | None]:
    """
    score >= min_score: auto
    soft_min..min_score: only if auto_soft and best available
    < soft_min: reject
    """
    best = None
    best_score: MatchScore | None = None
    for cand in candidates:
        sc = score_candidate(
            seed_title=seed_title,
            seed_artist=seed_artist,
            seed_duration_sec=seed_duration_sec,
            cand_title=getattr(cand, "title", None),
            cand_artist=getattr(cand, "channel_name", None)
            or getattr(cand, "artist", None),
            cand_duration_sec=getattr(cand, "duration_sec", None)
            or getattr(cand, "duration", None),
        )
        if best_score is None or sc.total > best_score.total:
            best = cand
            best_score = sc

    if best is None or best_score is None:
        return None, None
    if best_score.total >= min_score:
        return best, best_score
    if auto_soft and best_score.total >= soft_min:
        return best, best_score
    return None, best_score
